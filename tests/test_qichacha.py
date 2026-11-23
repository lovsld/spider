"""Unit tests for web/qichacha.py (fake transport, no network)."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import qichacha  # noqa: E402


def test_make_token_matches_md5_scheme():
    expected = hashlib.md5("KEY1234567890SECRET".encode()).hexdigest().upper()
    assert qichacha.make_token("KEY", "1234567890", "SECRET") == expected


def test_extract_phones_tolerant():
    payload = {"Result": {"Name": "某公司", "Tel": "0531-88886666；13800138000",
                          "Contact": {"Phone": "400-800-1234"}, "Other": "x"}}
    phones = qichacha.extract_phones(payload)
    assert "0531-88886666" in phones and "13800138000" in phones and "400-800-1234" in phones
    assert qichacha.extract_phones({"Result": None}) == []
    assert qichacha.extract_phones({}) == []


def test_client_signs_and_parses():
    seen = {}

    def fake_transport(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {"Status": "200", "Result": {"Tel": "13800138000"}}

    client = qichacha.QichachaClient("KEY", "SECRET", transport=fake_transport,
                                     clock=lambda: 1700000000.0)
    assert client.get_company_phones("91370100MA3C12345X") == ["13800138000"]
    assert seen["headers"]["Timespan"] == "1700000000"
    assert seen["headers"]["Token"] == qichacha.make_token("KEY", "1700000000", "SECRET")
    assert "keyword=91370100MA3C12345X" in seen["url"]


def test_client_raises_on_error_status():
    client = qichacha.QichachaClient(
        "KEY", "SECRET",
        transport=lambda url, headers: {"Status": "400", "Message": "bad key"},
        clock=lambda: 1.0,
    )
    try:
        client.get_company_phones("x")
        raise AssertionError("must raise")
    except RuntimeError as exc:
        assert "400" in str(exc)
