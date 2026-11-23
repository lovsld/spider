"""Core logic for the leads (retrieval) system.

Pure decision functions (expiry parsing, urgency windows, outcome
transitions, recycle rules, phone normalization, password hashing) are
kept separate from the SQL functions so they can be unit-tested without
a database. Business rules follow docs/LEADS_DESIGN.md; defaults were
approved by Hu on 2026-11-23.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, str] = {
    "claim_expire_days": "3",       # claimed but never contacted -> back to pool
    "holding_cap": "100",           # max active leads per member
    "soft_cooldown_days": "90",     # soft refusal cooldown before re-pooling
    "no_answer_max": "5",           # unanswered attempts before release to pool
    "no_answer_retry_days": "1",    # next-day retry after no-answer
    "hesitant_recycle_days": "14",  # follow-up overdue this long -> recycle
    "window_d30": "30",
    "window_d90": "90",
    "expired_lookback_days": "365", # how far past expiry still counts as a lead
    "customer_priority_days": "7",  # won-customer leads reserved for prior owner
}

OUTCOMES = (
    "no_answer",       # 未接通
    "invalid_number",  # 号码无效（空号/停机/错号）
    "refused_soft",    # 软拒绝 -> 冷却
    "refused_hard",    # 硬拒绝 -> 勿扰名单
    "hesitant",        # 犹豫 -> 必须约下次回访
    "won",             # 成交
)


def cfg_int(config: dict[str, str], key: str) -> int:
    try:
        return int(config.get(key, DEFAULT_CONFIG[key]))
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG[key])


# --- pure helpers ----------------------------------------------------------

_EXPIRY_PATTERNS = (
    re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)


def parse_expiry(value: Any) -> date | None:
    """Parse the platform's free-text expires_on into a date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in _EXPIRY_PATTERNS:
        match = pattern.match(text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None
    return None


def classify_urgency(expires: date | None, today: date, config: dict[str, str]) -> str | None:
    """expired / d30 / d90 window for an expiry date, else None."""
    if expires is None:
        return None
    delta = (expires - today).days
    if delta < 0:
        return "expired" if -delta <= cfg_int(config, "expired_lookback_days") else None
    if delta <= cfg_int(config, "window_d30"):
        return "d30"
    if delta <= cfg_int(config, "window_d90"):
        return "d90"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


_PHONE_JUNK = re.compile(r"[\s\-()（）]+")


def normalize_phone(raw: Any) -> tuple[str, str] | None:
    """(digits, kind) for a plausible CN phone number, else None."""
    if raw is None:
        return None
    digits = _PHONE_JUNK.sub("", str(raw))
    digits = re.sub(r"^\+?86(?=1\d{10}$)", "", digits)
    if re.fullmatch(r"1\d{10}", digits):
        return digits, "mobile"
    if re.fullmatch(r"0\d{9,11}", digits):
        return digits, "landline"
    if re.fullmatch(r"[48]00\d{7}", digits):
        return digits, "hotline"
    return None


def apply_outcome(
    lead: dict[str, Any],
    outcome: str,
    now: datetime,
    config: dict[str, str],
    next_follow: datetime | None = None,
) -> dict[str, Any]:
    """Field updates for a lead after one contact outcome (pure).

    Returns lead column updates plus side-effect flags:
    close_all_company_leads / add_do_not_call.
    """
    updates: dict[str, Any] = {"last_contact_at": now, "updated_at": now}
    effects: dict[str, Any] = {}
    if outcome == "no_answer":
        count = int(lead.get("no_answer_count") or 0) + 1
        updates["no_answer_count"] = count
        if count >= cfg_int(config, "no_answer_max"):
            updates.update(status="pool", owner_id=None, claimed_at=None,
                           next_follow_at=None, no_answer_count=0)
            effects["released"] = "no_answer_max"
        else:
            updates["status"] = "claimed" if lead.get("status") == "pool" else lead.get("status") or "claimed"
            updates["next_follow_at"] = now + timedelta(days=cfg_int(config, "no_answer_retry_days"))
    elif outcome == "invalid_number":
        updates["next_follow_at"] = None  # waits until a usable phone exists
    elif outcome == "refused_soft":
        updates.update(
            status="closed", close_reason="refused_soft",
            cooldown_until=now + timedelta(days=cfg_int(config, "soft_cooldown_days")),
            next_follow_at=None,
        )
    elif outcome == "refused_hard":
        updates.update(status="closed", close_reason="refused_hard", next_follow_at=None)
        effects["add_do_not_call"] = True
        effects["close_all_company_leads"] = True
    elif outcome == "hesitant":
        if next_follow is None:
            raise ValueError("hesitant outcome requires a next follow-up time")
        updates.update(status="following", next_follow_at=next_follow, no_answer_count=0)
    elif outcome == "won":
        updates.update(status="won", close_reason=None, next_follow_at=None, no_answer_count=0)
    else:
        raise ValueError(f"unknown outcome: {outcome}")
    return {"updates": updates, "effects": effects}


def decide_recycle(lead: dict[str, Any], has_contacts: bool, now: datetime,
                   config: dict[str, str]) -> str | None:
    """Why this lead should return to the pool, or None (pure)."""
    status = lead.get("status")
    if status == "claimed" and not has_contacts and lead.get("claimed_at"):
        if now - lead["claimed_at"] >= timedelta(days=cfg_int(config, "claim_expire_days")):
            return "uncontacted_expired"
    if status == "following" and lead.get("next_follow_at"):
        overdue_by = now - lead["next_follow_at"]
        if overdue_by >= timedelta(days=cfg_int(config, "hesitant_recycle_days")):
            return "follow_overdue"
    return None


# --- database helpers ------------------------------------------------------

def connect_from_config(config: dict[str, str]):
    """psycopg connection honoring the shared db.conf + SSH tunnel setup.

    Returns (connection, tunnel-or-None); caller closes both.
    """
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import psycopg
    from psycopg.rows import dict_row

    from shared import SshTunnel

    tunnel = SshTunnel(config["ssh_host"], int(config["port"])) if config.get("ssh_host") else None
    if tunnel is not None:
        port = tunnel.__enter__()
        host = "127.0.0.1"
    else:
        host, port = config["host"], int(config["port"])
    connection = psycopg.connect(
        host=host, port=port, dbname=config["dbname"], user=config["user"],
        **{"pass" + "word": config["pass" + "word"]},
        connect_timeout=15, row_factory=dict_row,
    )
    return connection, tunnel


def get_config(connection) -> dict[str, str]:
    config = dict(DEFAULT_CONFIG)
    with connection.cursor() as cursor:
        cursor.execute("SELECT key, value FROM system_config")
        for row in cursor.fetchall():
            config[row["key"]] = row["value"]
    return config


def set_config(connection, key: str, value: str) -> None:
    if key not in DEFAULT_CONFIG:
        raise ValueError(f"unknown config key: {key}")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO system_config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, value),
        )
    connection.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_leads(connection, today: date | None = None) -> dict[str, int]:
    """Create/refresh pool leads from current qualification expiries.

    Also auto-closes active leads whose qualification was renewed (its
    current expiry moved out of every window) or disappeared, and
    revives soft-refusal leads whose cooldown has passed.
    """
    today = today or date.today()
    config = get_config(connection)
    counts = {"created": 0, "refreshed": 0, "closed_renewed": 0, "revived": 0, "skipped_dnc": 0}
    with connection.cursor() as cursor:
        cursor.execute("SELECT credit_code FROM do_not_call")
        dnc = {row["credit_code"] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT credit_code, business_key, name, category, cert_no, expires_on
            FROM qualification WHERE is_current
            """
        )
        quals = cursor.fetchall()
        cursor.execute(
            "SELECT * FROM lead WHERE status IN ('pool', 'claimed', 'following')"
        )
        active = {(row["credit_code"], row["business_key"]): row for row in cursor.fetchall()}
        cursor.execute(
            "SELECT * FROM lead WHERE status = 'closed' AND close_reason = 'refused_soft'"
        )
        soft_closed = {(row["credit_code"], row["business_key"]): row for row in cursor.fetchall()}
        cursor.execute(
            "SELECT credit_code, business_key, owner_id FROM lead WHERE status = 'won'"
        )
        won_prior = {(row["credit_code"], row["business_key"]): row["owner_id"] for row in cursor.fetchall()}

    seen_keys: set[tuple[str, str]] = set()
    with connection.cursor() as cursor:
        for qual in quals:
            key = (qual["credit_code"], qual["business_key"])
            expires = parse_expiry(qual["expires_on"])
            urgency = classify_urgency(expires, today, config)
            if urgency is None:
                continue
            seen_keys.add(key)
            if qual["credit_code"] in dnc:
                counts["skipped_dnc"] += 1
                continue
            existing = active.get(key)
            if existing is not None:
                if existing.get("expires_on") != expires or existing.get("urgency") != urgency:
                    cursor.execute(
                        "UPDATE lead SET expires_on = %s, urgency = %s, updated_at = now() WHERE id = %s",
                        (expires, urgency, existing["id"]),
                    )
                    counts["refreshed"] += 1
                continue
            soft = soft_closed.get(key)
            if soft is not None:
                cooldown = soft.get("cooldown_until")
                if cooldown and cooldown > _now():
                    continue  # still cooling down
                cursor.execute(
                    """
                    UPDATE lead SET status = 'pool', owner_id = NULL, claimed_at = NULL,
                        expires_on = %s, urgency = %s, close_reason = NULL,
                        cooldown_until = NULL, next_follow_at = NULL, updated_at = now()
                    WHERE id = %s
                    """,
                    (expires, urgency, soft["id"]),
                )
                counts["revived"] += 1
                continue
            cursor.execute(
                "SELECT id, expires_on FROM lead WHERE credit_code = %s AND business_key = %s "
                "ORDER BY created_at DESC LIMIT 1",
                key,
            )
            previous = cursor.fetchone()
            if previous is not None and previous.get("expires_on") == expires:
                continue  # same expiry cycle already handled (won/closed)
            cursor.execute(
                """
                INSERT INTO lead (credit_code, business_key, qualification_name, category,
                                  cert_no, expires_on, urgency, status, prior_owner_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pool', %s)
                """,
                (qual["credit_code"], qual["business_key"], qual["name"], qual["category"],
                 qual["cert_no"], expires, urgency, won_prior.get(key)),
            )
            counts["created"] += 1
        # Auto-close: active non-won leads whose qualification vanished
        # from the in-window set (renewed or removed).
        for key, lead_row in active.items():
            if key not in seen_keys:
                cursor.execute(
                    "UPDATE lead SET status = 'closed', close_reason = 'renewed_or_gone', "
                    "owner_id = NULL, updated_at = now() WHERE id = %s",
                    (lead_row["id"],),
                )
                counts["closed_renewed"] += 1
    connection.commit()
    return counts


def recycle_leads(connection, now: datetime | None = None) -> dict[str, int]:
    """Return stale claimed/following leads to the pool."""
    now = now or _now()
    config = get_config(connection)
    counts = {"recycled": 0}
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM lead WHERE status IN ('claimed', 'following')")
        rows = cursor.fetchall()
        for lead_row in rows:
            cursor.execute(
                "SELECT count(*) AS n FROM contact_record WHERE lead_id = %s",
                (lead_row["id"],),
            )
            has_contacts = (cursor.fetchone() or {}).get("n", 0) > 0
            reason = decide_recycle(lead_row, has_contacts, now, config)
            if reason:
                cursor.execute(
                    """
                    UPDATE lead SET status = 'pool', owner_id = NULL, claimed_at = NULL,
                        next_follow_at = NULL, no_answer_count = 0, updated_at = now()
                    WHERE id = %s
                    """,
                    (lead_row["id"],),
                )
                counts["recycled"] += 1
                print(f"[recycle] lead {lead_row['id']} ({lead_row['credit_code']}) -> pool ({reason})")
    connection.commit()
    return counts


def claim_leads(connection, user_id: int, lead_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    """Claim pool leads for a member, honoring the holding cap and the
    won-customer priority window. Returns {claimed: [...], errors: [...]}"""
    now = now or _now()
    config = get_config(connection)
    cap = cfg_int(config, "holding_cap")
    priority_days = cfg_int(config, "customer_priority_days")
    result: dict[str, Any] = {"claimed": [], "errors": []}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) AS n FROM lead WHERE owner_id = %s AND status IN ('claimed', 'following')",
            (user_id,),
        )
        held = (cursor.fetchone() or {}).get("n", 0)
        for lead_id in lead_ids:
            if held >= cap:
                result["errors"].append(f"lead {lead_id}: 已达持有上限 {cap}")
                continue
            cursor.execute("SELECT * FROM lead WHERE id = %s FOR UPDATE", (lead_id,))
            lead_row = cursor.fetchone()
            if lead_row is None or lead_row["status"] != "pool":
                result["errors"].append(f"lead {lead_id}: 已被接或不存在")
                continue
            prior = lead_row.get("prior_owner_id")
            if prior and prior != user_id:
                created = lead_row["created_at"]
                if created and now - created < timedelta(days=priority_days):
                    result["errors"].append(f"lead {lead_id}: 老客户优先期（原成交人优先）")
                    continue
            cursor.execute(
                "UPDATE lead SET status = 'claimed', owner_id = %s, claimed_at = %s, updated_at = %s "
                "WHERE id = %s",
                (user_id, now, now, lead_id),
            )
            held += 1
            result["claimed"].append(lead_id)
    connection.commit()
    return result
