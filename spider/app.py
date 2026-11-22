#!/usr/bin/env python3
"""Search the imported MOHURD company records."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from import_db import SshTunnel, load_config

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
PAGE_SIZE = 20


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def open_pool(config: dict[str, str]) -> tuple[ConnectionPool, SshTunnel | None]:
    tunnel: SshTunnel | None = None
    if config.get("ssh_host"):
        tunnel = SshTunnel(config["ssh_host"], int(config["port"]))
        port = tunnel.__enter__()
        host = "127.0.0.1"
    else:
        host = config["host"]
        port = int(config["port"])
    conninfo = psycopg.conninfo.make_conninfo(
        host=host,
        port=port,
        dbname=config["dbname"],
        user=config["user"],
        password=config["password"],
        connect_timeout=15,
    )
    pool = ConnectionPool(conninfo, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
    return pool, tunnel


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(ROOT / "db.conf")
    pool, tunnel = open_pool(config)
    app.state.pool = pool
    app.state.tunnel = tunnel
    try:
        yield
    finally:
        pool.close()
        if tunnel is not None:
            tunnel.__exit__(None, None, None)


app = FastAPI(title="建筑企业查询", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


def query_all(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with app.state.pool.connection() as connection:
        return connection.execute(sql, params).fetchall()


def query_one(sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    with app.state.pool.connection() as connection:
        return connection.execute(sql, params).fetchone()


def parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def qualification_match(filters: dict[str, str], params: dict[str, Any]) -> str:
    """Predicate for one current qualification row aliased as q."""
    parts = ["q.is_current", "q.credit_code = c.credit_code"]
    qualification = filters.get("qualification", "").strip()
    if qualification:
        params["qualification"] = like_pattern(qualification)
        parts.append("(q.name ILIKE %(qualification)s ESCAPE '\\' OR q.cert_no ILIKE %(qualification)s ESCAPE '\\')")
    if filters.get("abnormal") == "1":
        parts.append("q.name LIKE '%%资质异常%%'")
    start = parse_day(filters.get("expires_from", ""))
    end = parse_day(filters.get("expires_to", ""))
    if start or end:
        parts.append("q.expires_on ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'")
    if start:
        params["expires_from"] = start
        parts.append("q.expires_on::date >= %(expires_from)s")
    if end:
        params["expires_to"] = end
        parts.append("q.expires_on::date <= %(expires_to)s")
    return " AND ".join(parts)


def search_companies(filters: dict[str, str], page: int) -> tuple[list[dict[str, Any]], int]:
    clauses = ["c.is_current"]
    params: dict[str, Any] = {}
    text_filters = {
        "name": "c.name",
        "credit_code": "c.credit_code",
        "legal_person": "c.legal_person",
        "region": "c.region",
    }
    for key, column in text_filters.items():
        value = filters.get(key, "").strip()
        if not value:
            continue
        params[key] = like_pattern(value)
        clauses.append(f"{column} ILIKE %({key})s ESCAPE '\\'")
    qual_sql = qualification_match(filters, params)
    qual_filtered = qual_sql != "q.is_current AND q.credit_code = c.credit_code"
    if qual_filtered:
        clauses.append(f"EXISTS (SELECT 1 FROM qualification q WHERE {qual_sql})")
    for key, table, predicate in (
        ("person", "registered_person", "p.person_name ILIKE %(person)s ESCAPE '\\'"),
        ("project", "project", "(p.project_name ILIKE %(project)s ESCAPE '\\' OR p.project_code ILIKE %(project)s ESCAPE '\\')"),
    ):
        value = filters.get(key, "").strip()
        if not value:
            continue
        params[key] = like_pattern(value)
        clauses.append(
            f"""EXISTS (
                SELECT 1 FROM {table} p
                WHERE p.credit_code = c.credit_code AND p.is_current AND {predicate}
            )"""
        )
    where = " AND ".join(clauses)
    total = query_one(f"SELECT count(*) AS count FROM company c WHERE {where}", params)
    params["limit"] = PAGE_SIZE
    params["offset"] = (page - 1) * PAGE_SIZE
    matched_expires = "NULL::text"
    matched_name = "NULL::text"
    order = "c.name"
    if qual_filtered:
        matched_expires = f"""(
            SELECT q.expires_on FROM qualification q
            WHERE {qual_sql}
            ORDER BY CASE WHEN q.expires_on ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' THEN q.expires_on::date END
            LIMIT 1
        )"""
        matched_name = f"""(
            SELECT q.name FROM qualification q
            WHERE {qual_sql}
            ORDER BY CASE WHEN q.expires_on ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' THEN q.expires_on::date END
            LIMIT 1
        )"""
        order = "matched_expires NULLS LAST, c.name"
    rows = query_all(
        f"""
        SELECT c.credit_code, c.name, c.legal_person, c.registration_type, c.region, c.address,
               (SELECT count(*) FROM qualification q
                WHERE q.credit_code = c.credit_code AND q.is_current) AS qualification_count,
               (SELECT count(*) FROM registered_person p
                WHERE p.credit_code = c.credit_code AND p.is_current) AS person_count,
               (SELECT count(*) FROM project p
                WHERE p.credit_code = c.credit_code AND p.is_current) AS project_count,
               {matched_expires} AS matched_expires,
               {matched_name} AS matched_name
        FROM company c
        WHERE {where}
        ORDER BY {order}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )
    return rows, int(total["count"] if total else 0)


def company_detail(credit_code: str) -> dict[str, Any] | None:
    company = query_one(
        """
        SELECT credit_code, name, legal_person, registration_type, region, address, detail_url, last_seen_at
        FROM company
        WHERE credit_code = %(credit_code)s AND is_current
        """,
        {"credit_code": credit_code},
    )
    if company is None:
        return None
    sections = {
        "qualifications": query_all(
            """
            SELECT category, cert_no, name, issued_on, expires_on, issuer
            FROM qualification
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY cert_no, name
            """,
            {"credit_code": credit_code},
        ),
        "people": query_all(
            """
            SELECT person_name, id_number, category, registration_no, specialty
            FROM registered_person
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY person_name, registration_no
            """,
            {"credit_code": credit_code},
        ),
        "projects": query_all(
            """
            SELECT project_code, project_name, region, category, owner
            FROM project
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY project_code
            """,
            {"credit_code": credit_code},
        ),
        "performances": query_all(
            """
            SELECT data_level, project_name, record_no, project_code, performance_type, detail_text
            FROM performance
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY record_no
            """,
            {"credit_code": credit_code},
        ),
        "misconducts": query_all(
            """
            SELECT subject_no, decision, department, valid_period
            FROM misconduct
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY subject_no
            """,
            {"credit_code": credit_code},
        ),
        "blacklists": query_all(
            """
            SELECT subject_no, basis, department, valid_period
            FROM blacklist
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY subject_no
            """,
            {"credit_code": credit_code},
        ),
        "punishments": query_all(
            """
            SELECT record_no, subject, legal_person, reason, department, listed_on
            FROM punishment
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY record_no
            """,
            {"credit_code": credit_code},
        ),
        "changes": query_all(
            """
            SELECT payload
            FROM change_log
            WHERE credit_code = %(credit_code)s AND is_current
            ORDER BY id
            """,
            {"credit_code": credit_code},
        ),
    }
    return {"company": company, **sections}


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    name: str = "",
    credit_code: str = "",
    legal_person: str = "",
    region: str = "",
    qualification: str = "",
    person: str = "",
    project: str = "",
    abnormal: str = "",
    expires_from: str = "",
    expires_to: str = "",
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    filters = {
        "name": name,
        "credit_code": credit_code,
        "legal_person": legal_person,
        "region": region,
        "qualification": qualification,
        "person": person,
        "project": project,
        "abnormal": "1" if abnormal == "1" else "",
        "expires_from": expires_from if parse_day(expires_from) else "",
        "expires_to": expires_to if parse_day(expires_to) else "",
    }
    rows, total = search_companies(filters, page)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        page = pages
        rows, total = search_companies(filters, page)
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "filters": filters,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "show_expiry": bool(filters["abnormal"] or filters["expires_from"] or filters["expires_to"] or filters["qualification"]),
            "page_link": lambda target: "/?" + urlencode({**{key: value for key, value in filters.items() if value}, "page": target}),
        },
    )


@app.get("/company/{credit_code}", response_class=HTMLResponse)
def detail(request: Request, credit_code: str) -> HTMLResponse:
    record = company_detail(credit_code)
    if record is None:
        raise HTTPException(status_code=404, detail="未找到这家企业的当前记录")
    return TEMPLATES.TemplateResponse(request, "detail.html", record)


def main() -> None:
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
