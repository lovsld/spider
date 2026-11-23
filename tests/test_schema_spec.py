"""Unit tests for web/ingest/schema_spec.py (pure logic, no database)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web" / "ingest"))

from schema_spec import compare_schema, load_expected, validate_record  # noqa: E402


def test_expected_spec_covers_all_tables():
    expected = load_expected()
    assert set(expected["tables"]) == {
        "crawl_snapshot", "company", "qualification", "registered_person",
        "project", "performance", "misconduct", "blacklist", "punishment",
        "change_log", "schema_meta",
        "app_user", "app_session", "lead", "contact_record",
        "company_phone", "do_not_call", "system_config",
    }
    assert expected["tables"]["company"]["credit_code"] == "varchar(32)"
    assert expected["tables"]["company"]["is_current"] == "boolean"
    assert "company_one_current" in expected["indexes"]


def test_compare_schema_reports_diffs():
    expected = load_expected()
    actual = {"tables": {name: dict(cols) for name, cols in expected["tables"].items()},
              "indexes": set(expected["indexes"])}
    assert compare_schema(expected, actual) == []
    del actual["tables"]["company"]["region"]
    actual["tables"]["company"]["name"] = "varchar(10)"
    actual["indexes"].discard("company_one_current")
    del actual["tables"]["blacklist"]
    diffs = compare_schema(expected, actual)
    assert any("missing column: company.region" in d for d in diffs)
    assert any("type mismatch: company.name" in d for d in diffs)
    assert any("missing unique index: company_one_current" in d for d in diffs)
    assert any("missing table: blacklist" in d for d in diffs)


def _valid_record() -> dict:
    rec = {
        "company_name": "测试公司",
        "企业基础信息": {"统一社会信用代码": "91370100MA3C12345X", "企业名称": "测试公司"},
    }
    for section in (
        "企业资质资格", "注册人员", "工程项目", "业绩技术指标",
        "不良行为", "黑名单记录", "失信联合惩戒记录",
    ):
        rec[section] = {"rows": []}
    return rec


def test_validate_record():
    assert validate_record(_valid_record()) == []
    bad = _valid_record()
    bad["企业基础信息"]["统一社会信用代码"] = "123"
    assert any("bad 统一社会信用代码" in p for p in validate_record(bad))
    bad2 = _valid_record()
    del bad2["工程项目"]
    assert any("bad section: 工程项目" in p for p in validate_record(bad2))
