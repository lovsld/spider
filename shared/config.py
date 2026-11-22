"""Read the repo-root db.conf (key=value lines). See db.conf.example."""
from __future__ import annotations

from pathlib import Path


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
