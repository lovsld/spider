#!/usr/bin/env python3
"""Scheduled / manual jobs for the leads system (cron-friendly).

Usage:
  python web/leads_jobs.py generate        # (re)build pool leads from expiries
  python web/leads_jobs.py recycle         # return stale claims to the pool
  python web/leads_jobs.py fetch-phones --limit 200   # Qichacha phone backfill
  python web/leads_jobs.py create-user zhangsan --role member
  python web/leads_jobs.py all             # generate + recycle

Cron example (every morning 06:30):
  30 6 * * *  cd /path/to/spider && python web/leads_jobs.py all
  0 7 * * *   cd /path/to/spider && QICHACHA_KEY=... QICHACHA_SECRET=... \
              python web/leads_jobs.py fetch-phones --limit 200

Database access uses the repo-root db.conf (same as import_db / web app).
Qichacha credentials come from the environment only.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WEB = Path(__file__).resolve().parent
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

import crm_core  # noqa: E402
from shared import load_config  # noqa: E402


def _connect():
    config = load_config(ROOT / "db.conf")
    return crm_core.connect_from_config(config)


def fetch_phones(connection, client, limit: int, sleep_seconds: float = 1.0) -> dict[str, int]:
    """Backfill company_phone from Qichacha for companies that have no
    usable number yet (companies with active leads first)."""
    counts = {"companies": 0, "phones_added": 0, "no_phone_found": 0, "errors": 0}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.credit_code, c.name
            FROM company c
            WHERE c.is_current
              AND NOT EXISTS (
                  SELECT 1 FROM company_phone p
                  WHERE p.credit_code = c.credit_code AND p.validity <> 'invalid'
              )
            ORDER BY EXISTS (
                SELECT 1 FROM lead l WHERE l.credit_code = c.credit_code
                AND l.status IN ('pool', 'claimed', 'following')
            ) DESC, c.name
            LIMIT %s
            """,
            (limit,),
        )
        companies = cursor.fetchall()
    for company in companies:
        counts["companies"] += 1
        try:
            raw_phones = client.get_company_phones(company["credit_code"])
        except Exception as exc:
            counts["errors"] += 1
            print(f"[qichacha] {company['name']}: {exc}")
            if counts["errors"] >= 5:
                print("[qichacha] 连续错误过多，停止本轮（检查 Key/额度/订阅接口）。")
                break
            continue
        added = 0
        with connection.cursor() as cursor:
            for raw in raw_phones:
                normalized = crm_core.normalize_phone(raw)
                if normalized is None:
                    continue
                phone, kind = normalized
                cursor.execute(
                    """
                    INSERT INTO company_phone (credit_code, phone, kind, source)
                    VALUES (%s, %s, %s, 'qichacha')
                    ON CONFLICT (credit_code, phone) DO NOTHING
                    """,
                    (company["credit_code"], phone, kind),
                )
                added += cursor.rowcount
        connection.commit()
        counts["phones_added"] += added
        if added == 0:
            counts["no_phone_found"] += 1
        else:
            print(f"[qichacha] {company['name']}: +{added} 个号码")
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate")
    sub.add_parser("recycle")
    sub.add_parser("all")
    phones = sub.add_parser("fetch-phones")
    phones.add_argument("--limit", type=int, default=200)
    phones.add_argument("--sleep", type=float, default=1.0)
    user = sub.add_parser("create-user")
    user.add_argument("username")
    user.add_argument("--role", default="member", choices=("member", "leader", "admin"))
    user.add_argument("--display-name", default="")
    args = parser.parse_args()

    connection, tunnel = _connect()
    try:
        if args.command in ("generate", "all"):
            print("[generate]", crm_core.generate_leads(connection))
        if args.command in ("recycle", "all"):
            print("[recycle]", crm_core.recycle_leads(connection))
        if args.command == "fetch-phones":
            from qichacha import QichachaClient

            client = QichachaClient.from_env()
            print("[fetch-phones]", fetch_phones(connection, client, args.limit, args.sleep))
        if args.command == "create-user":
            password = getpass.getpass("设置密码: ")
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO app_user (username, password_hash, display_name, role) "
                    "VALUES (%s, %s, %s, %s)",
                    (args.username, crm_core.hash_password(password),
                     args.display_name or args.username, args.role),
                )
            connection.commit()
            print(f"[user] 已创建 {args.username}（{args.role})")
    finally:
        connection.close()
        if tunnel is not None:
            tunnel.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
