"""Unit tests for web/crm_core.py pure logic (no database)."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import crm_core as core  # noqa: E402

CFG = dict(core.DEFAULT_CONFIG)
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


def test_parse_expiry_formats():
    assert core.parse_expiry("2027-05-01") == date(2027, 5, 1)
    assert core.parse_expiry("2027/5/1") == date(2027, 5, 1)
    assert core.parse_expiry("2027年5月1日") == date(2027, 5, 1)
    assert core.parse_expiry("20270501") == date(2027, 5, 1)
    assert core.parse_expiry("") is None
    assert core.parse_expiry(None) is None
    assert core.parse_expiry("长期") is None


def test_classify_urgency_windows():
    today = date(2026, 11, 23)
    assert core.classify_urgency(date(2026, 11, 1), today, CFG) == "expired"
    assert core.classify_urgency(date(2026, 12, 20), today, CFG) == "d30"
    assert core.classify_urgency(date(2027, 1, 30), today, CFG) == "d90"
    assert core.classify_urgency(date(2027, 6, 1), today, CFG) is None
    assert core.classify_urgency(date(2024, 1, 1), today, CFG) is None  # beyond lookback


def test_password_hash_roundtrip():
    stored = core.hash_password("s3cret")
    assert core.verify_password("s3cret", stored)
    assert not core.verify_password("wrong", stored)
    assert not core.verify_password("s3cret", "garbage")


def test_normalize_phone():
    assert core.normalize_phone("138 0013 8000") == ("13800138000", "mobile")
    assert core.normalize_phone("+86-138-0013-8000") == ("13800138000", "mobile")
    assert core.normalize_phone("0531-88886666") == ("053188886666", "landline")
    assert core.normalize_phone("12345") is None
    assert core.normalize_phone(None) is None


def _lead(**overrides):
    base = {"status": "claimed", "no_answer_count": 0, "claimed_at": NOW,
            "next_follow_at": None}
    base.update(overrides)
    return base


def test_apply_outcome_transitions():
    # no-answer increments and schedules a retry; at the cap it releases.
    out = core.apply_outcome(_lead(), "no_answer", NOW, CFG)
    assert out["updates"]["no_answer_count"] == 1
    assert out["updates"]["next_follow_at"] == NOW + timedelta(days=1)
    out = core.apply_outcome(_lead(no_answer_count=4), "no_answer", NOW, CFG)
    assert out["updates"]["status"] == "pool" and out["effects"]["released"] == "no_answer_max"
    # hesitant requires a follow-up time and moves to following.
    try:
        core.apply_outcome(_lead(), "hesitant", NOW, CFG)
        raise AssertionError("hesitant without follow-up must raise")
    except ValueError:
        pass
    follow = NOW + timedelta(days=3)
    out = core.apply_outcome(_lead(), "hesitant", NOW, CFG, next_follow=follow)
    assert out["updates"]["status"] == "following" and out["updates"]["next_follow_at"] == follow
    # soft refusal closes with a cooldown; hard refusal flags do-not-call.
    out = core.apply_outcome(_lead(), "refused_soft", NOW, CFG)
    assert out["updates"]["status"] == "closed"
    assert out["updates"]["cooldown_until"] == NOW + timedelta(days=90)
    out = core.apply_outcome(_lead(), "refused_hard", NOW, CFG)
    assert out["effects"]["add_do_not_call"] and out["effects"]["close_all_company_leads"]
    # won closes as won.
    out = core.apply_outcome(_lead(status="following"), "won", NOW, CFG)
    assert out["updates"]["status"] == "won"


def test_decide_recycle_rules():
    stale_claim = _lead(claimed_at=NOW - timedelta(days=3))
    assert core.decide_recycle(stale_claim, False, NOW, CFG) == "uncontacted_expired"
    assert core.decide_recycle(stale_claim, True, NOW, CFG) is None  # contacted: protected
    fresh_claim = _lead(claimed_at=NOW - timedelta(days=1))
    assert core.decide_recycle(fresh_claim, False, NOW, CFG) is None
    overdue = _lead(status="following", next_follow_at=NOW - timedelta(days=15))
    assert core.decide_recycle(overdue, True, NOW, CFG) == "follow_overdue"
    pool_lead = _lead(status="pool")
    assert core.decide_recycle(pool_lead, False, NOW, CFG) is None
