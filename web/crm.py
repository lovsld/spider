#!/usr/bin/env python3
"""Leads (retrieval) system web app for the calling team.

Separate from the public search site (app.py): login-required, with the
public-sea pool, claiming, per-company calling pages, follow-up
timeline, and an admin page. Business rules live in crm_core.py.

Run:  python web/crm.py            (uvicorn on 0.0.0.0:8001)
"""
from __future__ import annotations

import secrets
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import crm_core  # noqa: E402
from app import open_pool  # noqa: E402
from shared import load_config  # noqa: E402

TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
TZ = ZoneInfo("Asia/Shanghai")
SESSION_COOKIE = "crm_session"
SESSION_DAYS = 7

URGENCY_LABELS = {"expired": "已过期", "d30": "30天内到期", "d90": "90天内到期"}
OUTCOME_LABELS = {
    "no_answer": "未接通",
    "invalid_number": "号码无效",
    "refused_soft": "拒绝（软）",
    "refused_hard": "拒绝（勿扰）",
    "hesitant": "犹豫",
    "won": "成交",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(ROOT / "db.conf")
    pool, tunnel = open_pool(config)
    app.state.pool = pool
    app.state.tunnel = tunnel
    yield
    pool.close()
    if tunnel is not None:
        tunnel.__exit__(None, None, None)


app = FastAPI(title="资质到期线索系统", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


# --- auth ------------------------------------------------------------------

def current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.* FROM app_session s JOIN app_user u ON u.id = s.user_id
                WHERE s.token = %s AND s.expires_at > now() AND u.active
                """,
                (token,),
            )
            return cursor.fetchone()


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/crm/login"})
    return user


def require_role(request: Request, *roles: str) -> dict[str, Any]:
    user = require_user(request)
    if user["role"] not in roles:
        raise HTTPException(status_code=403, detail="无权限")
    return user


@app.get("/crm/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "crm_login.html", {"error": error})


@app.post("/crm/login")
async def login_submit(request: Request) -> RedirectResponse:
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM app_user WHERE username = %s AND active", (username,))
            user = cursor.fetchone()
        if user is None or not crm_core.verify_password(password, user["password_hash"]):
            return RedirectResponse("/crm/login?error=用户名或密码不对", status_code=303)
        token = secrets.token_urlsafe(32)
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app_session (token, user_id, expires_at) VALUES (%s, %s, %s)",
                (token, user["id"], datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)),
            )
        conn.commit()
    response = RedirectResponse("/crm/today", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True)
    return response


@app.get("/crm/logout")
def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with request.app.state.pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM app_session WHERE token = %s", (token,))
            conn.commit()
    response = RedirectResponse("/crm/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- shared queries ----------------------------------------------------------

def pool_filter_sql(filters: dict[str, str], params: dict[str, Any]) -> str:
    clauses = ["l.status = 'pool'"]
    if filters.get("city"):
        params["city"] = f"%{filters['city']}%"
        clauses.append("c.region LIKE %(city)s")
    if filters.get("category"):
        params["category"] = filters["category"]
        clauses.append("l.category = %(category)s")
    if filters.get("urgency"):
        params["urgency"] = filters["urgency"]
        clauses.append("l.urgency = %(urgency)s")
    if filters.get("keyword"):
        params["keyword"] = f"%{filters['keyword']}%"
        clauses.append("c.name LIKE %(keyword)s")
    if filters.get("has_phone") == "1":
        clauses.append(
            "EXISTS (SELECT 1 FROM company_phone p WHERE p.credit_code = l.credit_code "
            "AND p.validity <> 'invalid')"
        )
    if filters.get("has_phone") == "0":
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM company_phone p WHERE p.credit_code = l.credit_code "
            "AND p.validity <> 'invalid')"
        )
    return " AND ".join(clauses)


POOL_SELECT = """
    SELECT l.*, c.name AS company_name, c.region, c.legal_person,
           (SELECT count(*) FROM company_phone p
             WHERE p.credit_code = l.credit_code AND p.validity <> 'invalid') AS phone_count
    FROM lead l JOIN company c ON c.credit_code = l.credit_code AND c.is_current
"""


# --- pages -------------------------------------------------------------------

@app.get("/crm", response_class=HTMLResponse)
def crm_root(request: Request) -> RedirectResponse:
    return RedirectResponse("/crm/today", status_code=303)


@app.get("/crm/today", response_class=HTMLResponse)
def today_page(request: Request) -> HTMLResponse:
    user = require_user(request)
    today = today_local()
    day_start = datetime.combine(today, time.min, TZ)
    day_end = day_start + timedelta(days=1)
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT l.*, c.name AS company_name, c.region
                FROM lead l JOIN company c ON c.credit_code = l.credit_code AND c.is_current
                WHERE l.owner_id = %(uid)s AND l.status IN ('claimed', 'following')
                ORDER BY l.expires_on NULLS LAST, l.claimed_at
                """,
                {"uid": user["id"]},
            )
            mine = cursor.fetchall()
    overdue = [r for r in mine if r["next_follow_at"] and r["next_follow_at"] < day_start]
    due = [r for r in mine if r["next_follow_at"] and day_start <= r["next_follow_at"] < day_end]
    fresh = [r for r in mine if r["status"] == "claimed" and not r["last_contact_at"]]
    rest = [r for r in mine if r not in overdue and r not in due and r not in fresh]
    return TEMPLATES.TemplateResponse(
        request, "crm_today.html",
        {"user": user, "overdue": overdue, "due": due, "fresh": fresh, "rest": rest,
         "urgency_labels": URGENCY_LABELS},
    )


@app.get("/crm/pool", response_class=HTMLResponse)
def pool_page(request: Request) -> HTMLResponse:
    user = require_user(request)
    filters = {k: request.query_params.get(k, "") for k in
               ("city", "category", "urgency", "keyword", "has_phone")}
    params: dict[str, Any] = {}
    where = pool_filter_sql(filters, params)
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT DISTINCT category FROM lead WHERE category IS NOT NULL ORDER BY 1")
            categories = [row["category"] for row in cursor.fetchall()]
            cursor.execute(
                POOL_SELECT + f" WHERE {where} ORDER BY l.expires_on NULLS LAST LIMIT 200",
                params,
            )
            leads = cursor.fetchall()
            cursor.execute(f"SELECT count(*) AS n FROM lead l JOIN company c "
                           f"ON c.credit_code = l.credit_code AND c.is_current WHERE {where}", params)
            total = (cursor.fetchone() or {})["n"]
    return TEMPLATES.TemplateResponse(
        request, "crm_pool.html",
        {"user": user, "leads": leads, "filters": filters, "categories": categories,
         "total": total, "urgency_labels": URGENCY_LABELS,
         "msg": request.query_params.get("msg", "")},
    )


@app.post("/crm/claim")
async def claim_submit(request: Request) -> RedirectResponse:
    user = require_user(request)
    form = await request.form()
    ids = [int(v) for v in form.getlist("lead_ids") if str(v).isdigit()]
    back = str(form.get("back", "/crm/pool"))
    if not ids:
        return RedirectResponse(f"{back}?msg=没有选中线索", status_code=303)
    with request.app.state.pool.connection() as conn:
        result = crm_core.claim_leads(conn, user["id"], ids, now=datetime.now(timezone.utc))
    msg = f"接单 {len(result['claimed'])} 条"
    if result["errors"]:
        msg += "；未成功：" + "；".join(result["errors"][:5])
    return RedirectResponse(f"{back}?msg={msg}", status_code=303)


@app.get("/crm/mine", response_class=HTMLResponse)
def mine_page(request: Request) -> HTMLResponse:
    user = require_user(request)
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT l.*, c.name AS company_name, c.region
                FROM lead l JOIN company c ON c.credit_code = l.credit_code AND c.is_current
                WHERE l.owner_id = %(uid)s AND l.status IN ('claimed', 'following')
                ORDER BY c.name, l.expires_on
                """,
                {"uid": user["id"]},
            )
            leads = cursor.fetchall()
    return TEMPLATES.TemplateResponse(
        request, "crm_mine.html",
        {"user": user, "leads": leads, "urgency_labels": URGENCY_LABELS},
    )


@app.get("/crm/company/{credit_code}", response_class=HTMLResponse)
def company_page(request: Request, credit_code: str) -> HTMLResponse:
    user = require_user(request)
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM company WHERE credit_code = %s AND is_current", (credit_code,))
            company = cursor.fetchone()
            if company is None:
                raise HTTPException(status_code=404, detail="企业不存在")
            cursor.execute(
                "SELECT * FROM company_phone WHERE credit_code = %s ORDER BY validity DESC, id",
                (credit_code,),
            )
            phones = cursor.fetchall()
            cursor.execute(
                "SELECT * FROM lead WHERE credit_code = %s ORDER BY expires_on", (credit_code,)
            )
            all_leads = cursor.fetchall()
            cursor.execute(
                """
                SELECT r.*, u.display_name AS user_name FROM contact_record r
                JOIN app_user u ON u.id = r.user_id
                WHERE r.credit_code = %s ORDER BY r.created_at DESC LIMIT 100
                """,
                (credit_code,),
            )
            records = cursor.fetchall()
            cursor.execute("SELECT 1 FROM do_not_call WHERE credit_code = %s", (credit_code,))
            is_dnc = cursor.fetchone() is not None
    my_leads = [r for r in all_leads if r["owner_id"] == user["id"] and r["status"] in ("claimed", "following")]
    pool_leads = [r for r in all_leads if r["status"] == "pool"]
    others = [r for r in all_leads if r["status"] in ("claimed", "following") and r["owner_id"] != user["id"]]
    return TEMPLATES.TemplateResponse(
        request, "crm_company.html",
        {"user": user, "company": company, "phones": phones, "my_leads": my_leads,
         "pool_leads": pool_leads, "others_active": others, "records": records,
         "is_dnc": is_dnc, "urgency_labels": URGENCY_LABELS, "outcome_labels": OUTCOME_LABELS,
         "msg": request.query_params.get("msg", "")},
    )


@app.post("/crm/company/{credit_code}/phone")
async def phone_add(request: Request, credit_code: str) -> RedirectResponse:
    user = require_user(request)
    form = await request.form()
    normalized = crm_core.normalize_phone(str(form.get("phone", "")))
    back = f"/crm/company/{credit_code}"
    if normalized is None:
        return RedirectResponse(f"{back}?msg=号码格式不对", status_code=303)
    phone, kind = normalized
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO company_phone (credit_code, phone, kind, source, marked_by)
                VALUES (%s, %s, %s, 'manual', %s)
                ON CONFLICT (credit_code, phone) DO NOTHING
                """,
                (credit_code, phone, kind, user["id"]),
            )
        conn.commit()
    return RedirectResponse(f"{back}?msg=号码已添加", status_code=303)


@app.post("/crm/phone/{phone_id}/validity")
async def phone_validity(request: Request, phone_id: int) -> RedirectResponse:
    require_user(request)
    form = await request.form()
    validity = str(form.get("validity", ""))
    credit_code = str(form.get("credit_code", ""))
    if validity not in ("valid", "invalid", "unknown"):
        raise HTTPException(status_code=400, detail="validity 不合法")
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE company_phone SET validity = %s, updated_at = now() WHERE id = %s",
                (validity, phone_id),
            )
        conn.commit()
    return RedirectResponse(f"/crm/company/{credit_code}?msg=号码状态已更新", status_code=303)


@app.post("/crm/lead/{lead_id}/contact")
async def lead_contact(request: Request, lead_id: int) -> RedirectResponse:
    user = require_user(request)
    form = await request.form()
    outcome = str(form.get("outcome", ""))
    if outcome not in crm_core.OUTCOMES:
        raise HTTPException(status_code=400, detail="通话结果不合法")
    note = str(form.get("note", "")).strip()
    phone_used = str(form.get("phone_used", "")).strip()
    next_follow = None
    if outcome == "hesitant":
        raw_date = str(form.get("next_follow_date", "")).strip()
        if not raw_date:
            raise HTTPException(status_code=400, detail="犹豫必须填写下次回访日期")
        next_follow = datetime.combine(date.fromisoformat(raw_date), time(10, 0), TZ)
    product = str(form.get("product", "")).strip() or None
    amount_raw = str(form.get("amount", "")).strip()
    amount = float(amount_raw) if amount_raw else None
    now = datetime.now(timezone.utc)
    with request.app.state.pool.connection() as conn:
        config = crm_core.get_config(conn)
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM lead WHERE id = %s FOR UPDATE", (lead_id,))
            lead = cursor.fetchone()
            if lead is None:
                raise HTTPException(status_code=404, detail="线索不存在")
            if lead["owner_id"] != user["id"] and user["role"] not in ("leader", "admin"):
                raise HTTPException(status_code=403, detail="不是你的线索")
            credit_code = lead["credit_code"]
            result = crm_core.apply_outcome(lead, outcome, now, config, next_follow=next_follow)
            updates = result["updates"]
            assignments = ", ".join(f"{key} = %({key})s" for key in updates)
            cursor.execute(f"UPDATE lead SET {assignments} WHERE id = %(id)s",
                           {**updates, "id": lead_id})
            cursor.execute(
                """
                INSERT INTO contact_record (lead_id, credit_code, user_id, outcome, note,
                                            product, amount, next_follow_at, phone_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (lead_id, credit_code, user["id"], outcome, note, product, amount,
                 next_follow, phone_used or None),
            )
            if outcome == "invalid_number" and phone_used:
                cursor.execute(
                    "UPDATE company_phone SET validity = 'invalid', updated_at = now() "
                    "WHERE credit_code = %s AND phone = %s",
                    (credit_code, phone_used),
                )
            effects = result["effects"]
            if effects.get("add_do_not_call"):
                cursor.execute(
                    "INSERT INTO do_not_call (credit_code, reason, created_by) VALUES (%s, %s, %s) "
                    "ON CONFLICT (credit_code) DO NOTHING",
                    (credit_code, note or "硬拒绝", user["id"]),
                )
            if effects.get("close_all_company_leads"):
                cursor.execute(
                    "UPDATE lead SET status = 'closed', close_reason = 'refused_hard', "
                    "owner_id = NULL, updated_at = now() "
                    "WHERE credit_code = %s AND status IN ('pool', 'claimed', 'following')",
                    (credit_code,),
                )
        conn.commit()
    return RedirectResponse(f"/crm/company/{credit_code}?msg=已记录", status_code=303)


@app.post("/crm/lead/{lead_id}/release")
async def lead_release(request: Request, lead_id: int) -> RedirectResponse:
    user = require_user(request)
    form = await request.form()
    back = str(form.get("back", "/crm/today"))
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE lead SET status = 'pool', owner_id = NULL, claimed_at = NULL, "
                "next_follow_at = NULL, no_answer_count = 0, updated_at = now() "
                "WHERE id = %s AND owner_id = %s",
                (lead_id, user["id"]),
            )
        conn.commit()
    return RedirectResponse(f"{back}?msg=已释放回公海", status_code=303)


# --- team & admin --------------------------------------------------------------

@app.get("/crm/team", response_class=HTMLResponse)
def team_page(request: Request) -> HTMLResponse:
    require_role(request, "leader", "admin")
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.display_name, u.username,
                  (SELECT count(*) FROM lead l WHERE l.owner_id = u.id
                     AND l.status IN ('claimed', 'following')) AS held,
                  (SELECT count(*) FROM lead l WHERE l.owner_id = u.id
                     AND l.status = 'won') AS won,
                  (SELECT count(*) FROM contact_record r WHERE r.user_id = u.id
                     AND r.created_at >= now() - interval '30 days') AS contacts30
                FROM app_user u WHERE u.active ORDER BY u.id
                """
            )
            members = cursor.fetchall()
            cursor.execute(
                """
                SELECT outcome, count(*) AS n FROM contact_record
                WHERE created_at >= now() - interval '30 days' GROUP BY outcome ORDER BY outcome
                """
            )
            outcomes = cursor.fetchall()
            cursor.execute("SELECT status, count(*) AS n FROM lead GROUP BY status ORDER BY status")
            lead_stats = cursor.fetchall()
    return TEMPLATES.TemplateResponse(
        request, "crm_team.html",
        {"user": require_user(request), "members": members, "outcomes": outcomes,
         "lead_stats": lead_stats, "outcome_labels": OUTCOME_LABELS},
    )


@app.get("/crm/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> HTMLResponse:
    user = require_role(request, "admin")
    with request.app.state.pool.connection() as conn:
        config = crm_core.get_config(conn)
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, username, display_name, role, active FROM app_user ORDER BY id")
            users = cursor.fetchall()
    return TEMPLATES.TemplateResponse(
        request, "crm_admin.html",
        {"user": user, "config": config, "users": users,
         "msg": request.query_params.get("msg", "")},
    )


@app.post("/crm/admin/config")
async def admin_config(request: Request) -> RedirectResponse:
    require_role(request, "admin")
    form = await request.form()
    with request.app.state.pool.connection() as conn:
        for key in crm_core.DEFAULT_CONFIG:
            value = str(form.get(key, "")).strip()
            if value:
                crm_core.set_config(conn, key, value)
    return RedirectResponse("/crm/admin?msg=配置已保存", status_code=303)


@app.post("/crm/admin/jobs")
async def admin_jobs(request: Request) -> RedirectResponse:
    require_role(request, "admin")
    form = await request.form()
    job = str(form.get("job", ""))
    with request.app.state.pool.connection() as conn:
        if job == "generate":
            result = crm_core.generate_leads(conn)
        elif job == "recycle":
            result = crm_core.recycle_leads(conn)
        else:
            raise HTTPException(status_code=400, detail="未知任务")
    return RedirectResponse(f"/crm/admin?msg={job} 完成：{result}", status_code=303)


@app.post("/crm/admin/users")
async def admin_users(request: Request) -> RedirectResponse:
    require_role(request, "admin")
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    role = str(form.get("role", "member"))
    display = str(form.get("display_name", "")).strip() or username
    if not username or len(password) < 6 or role not in ("member", "leader", "admin"):
        return RedirectResponse("/crm/admin?msg=参数不对（密码至少 6 位）", status_code=303)
    with request.app.state.pool.connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app_user (username, password_hash, display_name, role) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (username, crm_core.hash_password(password), display, role),
            )
            created = cursor.rowcount
        conn.commit()
    return RedirectResponse(f"/crm/admin?msg={'已创建用户' if created else '用户名已存在'}", status_code=303)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
