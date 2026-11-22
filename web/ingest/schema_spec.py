"""Expected database schema, derived from schema.sql (single source).

Parses CREATE TABLE / CREATE UNIQUE INDEX statements out of schema.sql
so the expectation can never drift from the DDL by hand-editing a second
copy. `compare_schema` is pure: it takes the actual structure fetched
from information_schema / pg_indexes and returns human-readable diffs.
"""
from __future__ import annotations

import re
from pathlib import Path

SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = 1

# Map information_schema type names back to the DDL spellings we compare.
_TYPE_ALIASES = {
    "character varying": "varchar",
    "timestamp with time zone": "timestamptz",
    "bigint": "bigint",
    "boolean": "boolean",
    "text": "text",
    "jsonb": "jsonb",
    "integer": "int",
}


def parse_expected(sql_text: str) -> dict:
    """{"tables": {name: {column: normalized_type}}, "indexes": {name}}"""
    tables: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\);", sql_text, re.S
    ):
        name, body = match.group(1), match.group(2)
        columns: dict[str, str] = {}
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            col = re.match(r"^(\w+)\s+(.+)$", line)
            if not col or col.group(1).upper() in {"UNIQUE", "PRIMARY", "CONSTRAINT"}:
                continue
            raw_type = col.group(2).strip().lower()
            raw_type = re.sub(r"\s+", " ", raw_type)
            # Keep the base type incl. length for varchar(n).
            base = re.match(r"^(varchar\(\d+\)|[\w ]+?)(?:\s+.*)?$", raw_type)
            columns[col.group(1)] = (base.group(1).strip() if base else raw_type)
        tables[name] = columns
    indexes = set(re.findall(r"CREATE UNIQUE INDEX IF NOT EXISTS (\w+)", sql_text))
    return {"tables": tables, "indexes": indexes}


def load_expected() -> dict:
    return parse_expected(SCHEMA_SQL.read_text(encoding="utf-8"))


def normalize_actual_type(data_type: str, char_max_len: int | None) -> str:
    base = _TYPE_ALIASES.get(data_type, data_type)
    if base == "varchar" and char_max_len:
        return f"varchar({char_max_len})"
    return base


def compare_schema(expected: dict, actual: dict) -> list[str]:
    """actual: {"tables": {name: {column: normalized_type}}, "indexes": set}"""
    diffs: list[str] = []
    for table, columns in expected["tables"].items():
        if table not in actual["tables"]:
            diffs.append(f"missing table: {table}")
            continue
        for column, want_type in columns.items():
            got = actual["tables"][table].get(column)
            if got is None:
                diffs.append(f"missing column: {table}.{column}")
            elif got != want_type:
                diffs.append(f"type mismatch: {table}.{column} expected {want_type}, got {got}")
    for index in sorted(expected["indexes"]):
        if index not in actual["indexes"]:
            diffs.append(f"missing unique index: {index}")
    return diffs


# --- Record shape validation (collector JSON -> ingest) -------------------

RECORD_SECTIONS = (
    "企业资质资格",
    "注册人员",
    "工程项目",
    "业绩技术指标",
    "不良行为",
    "黑名单记录",
    "失信联合惩戒记录",
)

_CREDIT_RE = re.compile(r"^[0-9A-Z]{15,18}$")


def validate_record(record: dict) -> list[str]:
    """Structural check for one collected record; [] means valid."""
    problems: list[str] = []
    info = record.get("企业基础信息")
    if not isinstance(info, dict):
        return ["missing 企业基础信息"]
    code = str(info.get("统一社会信用代码") or "")
    if not _CREDIT_RE.match(code):
        problems.append(f"bad 统一社会信用代码: {code!r}")
    if not str(info.get("企业名称") or "").strip():
        problems.append("missing 企业名称")
    for section in RECORD_SECTIONS:
        value = record.get(section)
        if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
            problems.append(f"bad section: {section}")
    return problems
