"""Unit tests for combo claim/lock/stop/status helpers in collect.py."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

import collect as collect_mod  # noqa: E402


def test_claim_freshness_and_takeover_after_ttl():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "combo.state.json"
        now = time.time()
        assert collect_mod.try_claim(state, "w1", now_ts=now)
        assert not collect_mod.try_claim(state, "w2", now_ts=now + 10)  # fresh claim blocks
        assert collect_mod.try_claim(state, "w1", now_ts=now + 10)  # owner re-claims
        later = now + 10 + collect_mod.CLAIM_TTL_SECONDS + 1
        assert collect_mod.try_claim(state, "w2", now_ts=later)  # stale claim taken over
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["claimed_by"] == "w2"


def test_combo_lock_exclusive_and_stale_steal():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "combo.state.json"
        lock = collect_mod.acquire_combo_lock(state, ttl=60)
        assert lock is not None and lock.exists()
        assert collect_mod.acquire_combo_lock(state, ttl=60) is None
        collect_mod.release_combo_lock(lock)
        assert not lock.exists()
        lock2 = collect_mod.acquire_combo_lock(state, ttl=0)  # ttl=0: any existing lock is stale
        assert lock2 is not None
        collect_mod.release_combo_lock(lock2)


def test_stop_requested_and_status_summary():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        assert not collect_mod.stop_requested(data, "w1")
        (data / "STOP.w1").write_text("", encoding="utf-8")
        assert collect_mod.stop_requested(data, "w1")
        assert not collect_mod.stop_requested(data, "w2")
        now = time.time()
        (data / "a.state.json").write_text(json.dumps(
            {"status": "complete", "records": 10, "dedup_skipped": 2}), encoding="utf-8")
        (data / "b.state.json").write_text(json.dumps(
            {"status": "OVERFLOW", "records": 450}), encoding="utf-8")
        (data / "c.state.json").write_text(json.dumps(
            {"claimed_by": "w9", "heartbeat_ts": now}), encoding="utf-8")
        (data / "d.state.json").write_text(json.dumps({}), encoding="utf-8")
        s = collect_mod.summarize_states(data, now_ts=now)
        assert s == {"combos": 4, "complete": 1, "overflow": 1, "running": 1,
                     "pending": 1, "records": 460, "dedup_skipped": 2}


def test_worker_isolation_paths():
    import argparse

    args = argparse.Namespace(
        worker="jinan-1",
        profile=collect_mod.DEFAULT_PROFILE,
        ready_flag=Path("start_collect.flag"),
    )
    out = collect_mod.apply_worker_isolation(args)
    assert out.profile.name == f"playwright_profile_jinan-1"
    assert out.ready_flag == Path("start_collect.jinan-1.flag")
    args2 = argparse.Namespace(
        worker="jinan-1",
        profile=Path("/tmp/custom-profile"),
        ready_flag=Path("/tmp/custom.flag"),
    )
    out2 = collect_mod.apply_worker_isolation(args2)
    assert out2.profile == Path("/tmp/custom-profile")  # explicit wins
    assert out2.ready_flag == Path("/tmp/custom.flag")
