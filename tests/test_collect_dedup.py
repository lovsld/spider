"""Unit tests for collection-time dedup helpers in collector/collect.py."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

import collect as collect_mod  # noqa: E402


def _record(code: str, complete: bool) -> dict:
    rec = {
        "company_name": "测试公司",
        "企业基础信息": {"统一社会信用代码": code, "企业名称": "测试公司"},
    }
    if complete:
        for section in collect_mod.SECTIONS:
            rec[section] = {"rows": []}
    return rec


def test_completed_keys_from_only_complete_records(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "city.json"
        path.write_text(
            json.dumps(
                [
                    _record("91370100MA3C12345X", complete=True),
                    _record("91370100MA3C99999Y", complete=False),
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        keys = collect_mod.completed_keys_from(path)
        assert keys == {"91370100MA3C12345X"}
        assert collect_mod.completed_keys_from(Path(td) / "missing.json") == set()


def test_list_fingerprint_order_insensitive_and_change_sensitive():
    rows_a = [
        {"统一社会信用代码": "AAA", "企业名称": "甲", "企业法定代表人": "张", "企业注册属地": "山东省-济南市"},
        {"统一社会信用代码": "BBB", "企业名称": "乙", "企业法定代表人": "李", "企业注册属地": "山东省-青岛市"},
    ]
    rows_b = list(reversed(rows_a))
    assert collect_mod.list_fingerprint(rows_a) == collect_mod.list_fingerprint(rows_b)
    rows_c = [dict(rows_a[0]), dict(rows_a[1], 企业法定代表人="王")]
    assert collect_mod.list_fingerprint(rows_a) != collect_mod.list_fingerprint(rows_c)
