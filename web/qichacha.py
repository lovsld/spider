"""Qichacha （企查查） open-API client: fetch company phone numbers.

Auth scheme (Qichacha open platform): request headers carry
  Timespan: unix seconds
  Token:    MD5(AppKey + Timespan + SecretKey), upper case
Credentials come from the environment (QICHACHA_KEY / QICHACHA_SECRET),
never from source code. The default endpoint is the basic-info API
(ECIInfoVerify/GetInfo); Hu's membership may subscribe to a dedicated
contact-info product instead — set QICHACHA_API_PATH to its path
without code changes. Phone extraction is deliberately tolerant: it
collects every plausible phone field from the Result payload.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE_URL = "https://api.qichacha.com/"
DEFAULT_API_PATH = "ECIInfoVerify/GetInfo"
_PHONE_FIELDS = ("Tel", "Phone", "PhoneNumber", "Mobile", "ContactPhone",
                 "TelePhone", "PhoneNum", "ContactTel")


def make_token(app_key: str, timespan: str, secret_key: str) -> str:
    return hashlib.md5(f"{app_key}{timespan}{secret_key}".encode("utf-8")).hexdigest().upper()


def _default_transport(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_phones(payload: dict[str, Any]) -> list[str]:
    """All raw phone strings found in a Qichacha Result payload."""
    found: list[str] = []

    def walk(node: Any, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, key)
        elif isinstance(node, list):
            for item in node:
                walk(item, key_hint)
        elif isinstance(node, str) and key_hint in _PHONE_FIELDS:
            for part in node.replace("；", ";").replace("，", ";").split(";"):
                part = part.strip()
                if part:
                    found.append(part)

    result = payload.get("Result") if isinstance(payload, dict) else None
    if result is not None:
        walk(result)
    return found


class QichachaClient:
    def __init__(
        self,
        app_key: str,
        secret_key: str,
        api_path: str = DEFAULT_API_PATH,
        transport: Callable[[str, dict[str, str]], dict[str, Any]] = _default_transport,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not app_key or not secret_key:
            raise ValueError("Qichacha credentials missing (QICHACHA_KEY / QICHACHA_SECRET)")
        self.app_key = app_key
        self.secret_key = secret_key
        self.api_path = api_path
        self.transport = transport
        self.clock = clock

    @classmethod
    def from_env(cls) -> "QichachaClient":
        return cls(
            app_key=os.environ.get("QICHACHA_KEY", ""),
            secret_key=os.environ.get("QICHACHA_SECRET", ""),
            api_path=os.environ.get("QICHACHA_API_PATH", DEFAULT_API_PATH),
        )

    def get_company_phones(self, keyword: str) -> list[str]:
        """Raw phone strings for a company name / credit code keyword."""
        timespan = str(int(self.clock()))
        token = make_token(self.app_key, timespan, self.secret_key)
        query = urllib.parse.urlencode({"key": self.app_key, "keyword": keyword})
        url = f"{BASE_URL}{self.api_path}?{query}"
        payload = self.transport(url, {"Token": token, "Timespan": timespan})
        status = str(payload.get("Status", ""))
        if status != "200":
            raise RuntimeError(f"qichacha status {status}: {payload.get('Message')}")
        return extract_phones(payload)
