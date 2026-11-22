#!/usr/bin/env python3
"""Import scraped MOHURD company JSON into PostgreSQL.

Each company and child record keeps one current row. When a later file shows
different content for the same business key, the old row is closed and a new
current row is inserted. Running the same file again only refreshes last_seen_at.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "db.conf"
DEFAULT_JSON = Path(__file__).resolve().parent / "mohurd_shandong_level1.json"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"

CHILD_TABLES = (
    "qualification",
    "registered_person",
    "project",
    "performance",
    "misconduct",
    "blacklist",
    "punishment",
    "change_log",
)


def load_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    required = ("host", "port", "dbname", "user", "password")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise SystemExit(f"{path} is missing: {', '.join(missing)}")
    return values


def connect(config: dict[str, str], host: str | None = None, port: int | None = None) -> psycopg.Connection:
    return psycopg.connect(
        host=host or config["host"],
        port=port or int(config["port"]),
        dbname=config["dbname"],
        user=config["user"],
        password=config["password"],
        connect_timeout=15,
    )


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class SshTunnel:
    """Reach Postgres on Altare when the intranet address is not routed here."""

    def __init__(self, ssh_host: str, remote_port: int) -> None:
        self.ssh_host = ssh_host
        self.remote_port = remote_port
        self.local_port = 15432
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> int:
        while port_open(self.local_port):
            self.local_port += 1
        self.process = subprocess.Popen(
            [
                "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
                self.ssh_host,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                error = self.process.stderr.read().decode("utf-8", errors="replace") if self.process.stderr else ""
                raise SystemExit(f"SSH tunnel to {self.ssh_host} failed: {error.strip()}")
            if port_open(self.local_port):
                return self.local_port
            time.sleep(0.2)
        raise SystemExit(f"SSH tunnel to {self.ssh_host} did not open")

    def __exit__(self, *args: object) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def clean(value: Any) -> str:
    return str(value or "").strip()


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def rows_of(record: dict[str, Any], section: str) -> list[dict[str, str]]:
    section_value = record.get(section) or {}
    rows = section_value.get("rows") if isinstance(section_value, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def qualification_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "企业资质资格"):
        cert_no = clean(row.get("资质证书号"))
        name = clean(row.get("资质名称"))
        if not cert_no or not name:
            continue
        body = {
            "category": clean(row.get("资质类别")),
            "cert_no": cert_no,
            "name": name,
            "issued_on": clean(row.get("发证日期")),
            "expires_on": clean(row.get("发证有效期")),
            "issuer": clean(row.get("发证机关")),
        }
        items.append({"business_key": f"{cert_no}|{name}", "payload": row, **body, "content_hash": digest(body)})
    return items


def person_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "注册人员"):
        registration_no = clean(row.get("注册号(执业印章号)"))
        if not registration_no:
            continue
        body = {
            "person_name": clean(row.get("姓名")),
            "id_number": clean(row.get("身份证号")),
            "category": clean(row.get("注册类别")),
            "registration_no": registration_no,
            "specialty": clean(row.get("注册专业")),
        }
        key = f"{registration_no}|{body['specialty']}|{body['person_name']}"
        items.append({"business_key": key, "payload": row, **body, "content_hash": digest(body)})
    return items


def project_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "工程项目"):
        project_code = clean(row.get("项目编码"))
        if not project_code:
            continue
        body = {
            "project_code": project_code,
            "project_name": clean(row.get("项目名称")),
            "region": clean(row.get("项目属地")),
            "category": clean(row.get("项目类别")),
            "owner": clean(row.get("建设单位")),
        }
        items.append({"business_key": project_code, "payload": row, **body, "content_hash": digest(body)})
    return items


def performance_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "业绩技术指标"):
        record_no = clean(row.get("业绩记录编号"))
        if not record_no:
            continue
        detail = row.get("详情") if isinstance(row.get("详情"), dict) else {}
        body = {
            "data_level": clean(row.get("数据等级 ?") or row.get("数据等级")),
            "project_name": clean(row.get("项目名称")),
            "record_no": record_no,
            "project_code": clean(row.get("项目编号")),
            "performance_type": clean(row.get("业绩类型")),
            "detail_text": clean(detail.get("text")),
        }
        items.append({"business_key": record_no, "payload": row, **body, "content_hash": digest(body)})
    return items


def misconduct_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "不良行为"):
        if clean(row.get("资质证书号")) and not clean(row.get("诚信记录主体及编号")):
            continue
        body = {
            "subject_no": clean(row.get("诚信记录主体及编号")),
            "decision": clean(row.get("决定内容")),
            "department": clean(row.get("实施部门")),
            "valid_period": clean(row.get("决定日期与有效期")),
        }
        if not any(body.values()):
            continue
        items.append({"business_key": digest(body), "payload": row, **body, "content_hash": digest(body)})
    return items


def blacklist_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "黑名单记录"):
        body = {
            "subject_no": clean(row.get("黑名单记录主体及编号")),
            "basis": clean(row.get("黑名单认定依据")),
            "department": clean(row.get("认定部门")),
            "valid_period": clean(row.get("决定日期与有效期")),
        }
        if not any(body.values()):
            continue
        key = body["subject_no"] or digest(body)
        items.append({"business_key": key, "payload": row, **body, "content_hash": digest(body)})
    return items


def punishment_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "失信联合惩戒记录"):
        if clean(row.get("资质证书号")) and not clean(row.get("失信记录编号")):
            continue
        record_no = clean(row.get("失信记录编号"))
        body = {
            "record_no": record_no,
            "subject": clean(row.get("失信联合惩戒记录主体")),
            "legal_person": clean(row.get("法人姓名")),
            "reason": clean(row.get("列入名单事由")),
            "department": clean(row.get("认定部门")),
            "listed_on": clean(row.get("列入日期")),
        }
        if not any(body.values()):
            continue
        items.append({
            "business_key": record_no or digest(body),
            "payload": row,
            **body,
            "content_hash": digest(body),
        })
    return items


def change_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for row in rows_of(record, "变更记录"):
        if not any(clean(value) for value in row.values()):
            continue
        body = {key: clean(value) for key, value in row.items() if key != "序号"}
        content_hash = digest(body)
        items.append({
            "business_key": content_hash,
            "payload": row,
            "content_hash": content_hash,
        })
    return items


SECTION_BUILDERS = {
    "qualification": qualification_items,
    "registered_person": person_items,
    "project": project_items,
    "performance": performance_items,
    "misconduct": misconduct_items,
    "blacklist": blacklist_items,
    "punishment": punishment_items,
    "change_log": change_items,
}

INSERT_COLUMNS = {
    "qualification": ("category", "cert_no", "name", "issued_on", "expires_on", "issuer"),
    "registered_person": ("person_name", "id_number", "category", "registration_no", "specialty"),
    "project": ("project_code", "project_name", "region", "category", "owner"),
    "performance": ("data_level", "project_name", "record_no", "project_code", "performance_type", "detail_text"),
    "misconduct": ("subject_no", "decision", "department", "valid_period"),
    "blacklist": ("subject_no", "basis", "department", "valid_period"),
    "punishment": ("record_no", "subject", "legal_person", "reason", "department", "listed_on"),
    "change_log": (),
}


def company_body(record: dict[str, Any]) -> dict[str, str]:
    info = record.get("企业基础信息") if isinstance(record.get("企业基础信息"), dict) else {}
    return {
        "credit_code": clean(info.get("统一社会信用代码")),
        "name": clean(info.get("企业名称") or record.get("company_name")),
        "legal_person": clean(info.get("企业法定代表人")),
        "registration_type": clean(info.get("企业登记注册类型")),
        "region": clean(info.get("企业注册属地")),
        "address": clean(info.get("企业经营地址")),
        "detail_url": clean(record.get("detail_url")),
    }


def sync_company(connection: psycopg.Connection, body: dict[str, str], seen_at: datetime) -> None:
    content_hash = digest(body)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, content_hash, first_seen_at
            FROM company
            WHERE credit_code = %s AND is_current
            """,
            (body["credit_code"],),
        )
        current = cursor.fetchone()
        if current and current[1] == content_hash:
            cursor.execute(
                "UPDATE company SET last_seen_at = %s, detail_url = %s WHERE id = %s",
                (seen_at, body["detail_url"], current[0]),
            )
            return
        first_seen = current[2] if current else seen_at
        if current:
            cursor.execute(
                "UPDATE company SET is_current = false, valid_to = %s, last_seen_at = %s WHERE id = %s",
                (seen_at, seen_at, current[0]),
            )
        cursor.execute(
            """
            INSERT INTO company (
                credit_code, name, legal_person, registration_type, region, address,
                detail_url, content_hash, valid_from, is_current, first_seen_at, last_seen_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)
            """,
            (
                body["credit_code"], body["name"], body["legal_person"], body["registration_type"],
                body["region"], body["address"], body["detail_url"], content_hash,
                seen_at, first_seen, seen_at,
            ),
        )


def sync_children(
    connection: psycopg.Connection,
    table: str,
    credit_code: str,
    items: list[dict[str, Any]],
    seen_at: datetime,
) -> None:
    columns = INSERT_COLUMNS[table]
    column_sql = ", ".join(columns) + ", " if columns else ""
    items = list({item["business_key"]: item for item in items}.values())
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, business_key, content_hash, first_seen_at FROM {table} WHERE credit_code = %s AND is_current",
            (credit_code,),
        )
        current = {row[1]: row for row in cursor.fetchall()}
        seen_keys: set[str] = set()
        unchanged_ids: list[int] = []
        close_ids: list[int] = []
        missing_ids: list[int] = []
        inserts: list[tuple[Any, ...]] = []
        for item in items:
            key = item["business_key"]
            seen_keys.add(key)
            existing = current.get(key)
            if existing and existing[2] == item["content_hash"]:
                unchanged_ids.append(existing[0])
                continue
            first_seen = existing[3] if existing else seen_at
            if existing:
                close_ids.append(existing[0])
            values = [item.get(column, "") for column in columns]
            inserts.append((
                credit_code, key, *values, item["content_hash"], Jsonb(item["payload"]),
                seen_at, True, first_seen, seen_at,
            ))
        for key, existing in current.items():
            if key not in seen_keys:
                missing_ids.append(existing[0])
        if unchanged_ids:
            cursor.execute(
                f"UPDATE {table} SET last_seen_at = %s WHERE id = ANY(%s)",
                (seen_at, unchanged_ids),
            )
        if close_ids:
            cursor.execute(
                f"UPDATE {table} SET is_current = false, valid_to = %s, last_seen_at = %s WHERE id = ANY(%s)",
                (seen_at, seen_at, close_ids),
            )
        if missing_ids:
            cursor.execute(
                f"UPDATE {table} SET is_current = false, valid_to = %s WHERE id = ANY(%s) AND is_current",
                (seen_at, missing_ids),
            )
        if inserts:
            width = len(inserts[0])
            values_sql = ", ".join(["(" + ", ".join(["%s"] * width) + ")"] * len(inserts))
            cursor.execute(
                f"""
                INSERT INTO {table} (
                    credit_code, business_key, {column_sql}
                    content_hash, payload, valid_from, is_current, first_seen_at, last_seen_at
                ) VALUES {values_sql}
                """,
                [value for row in inserts for value in row],
            )


def import_record(connection: psycopg.Connection, record: dict[str, Any]) -> str:
    body = company_body(record)
    if not body["credit_code"] or not body["name"]:
        return "skipped"
    seen_at = parse_time(clean(record.get("collected_at_utc")) or None)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO crawl_snapshot (credit_code, crawled_at, detail_url, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (credit_code, crawled_at) DO NOTHING
            RETURNING id
            """,
            (body["credit_code"], seen_at, body["detail_url"], Jsonb(record)),
        )
        inserted = cursor.fetchone()
    sync_company(connection, body, seen_at)
    for table, builder in SECTION_BUILDERS.items():
        sync_children(connection, table, body["credit_code"], builder(record), seen_at)
    connection.commit()
    return "inserted" if inserted else "refreshed"


def apply_schema(connection: psycopg.Connection) -> None:
    connection.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args()
    records = json.loads(args.json.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit(f"{args.json} must contain a JSON array")
    counts = {"inserted": 0, "refreshed": 0, "skipped": 0}
    config = load_config(args.config)
    tunnel = SshTunnel(config["ssh_host"], int(config["port"])) if config.get("ssh_host") else None
    if tunnel is None:
        connection_context = connect(config)
        local_port = None
    else:
        local_port = tunnel.__enter__()
        connection_context = connect(config, host="127.0.0.1", port=local_port)
    try:
        with connection_context as connection:
            apply_schema(connection)
            for index, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    counts["skipped"] += 1
                    continue
                result = import_record(connection, record)
                counts[result] += 1
                if index % 25 == 0 or index == len(records):
                    print(f"[import] {index}/{len(records)} {counts}", flush=True)
    finally:
        if tunnel is not None:
            tunnel.__exit__(None, None, None)
    print(f"Done. {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
