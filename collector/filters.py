"""Filter enumeration and traversal planning for the MOHURD collector.

Pure logic only — no browser, no network — so it can be unit-tested.
The platform (https://jzsc.mohurd.gov.cn/data/company) shows at most
30 pages x 15 rows = 450 records for one filter combination, so a single
(qualification, province) query can silently truncate. Traversal rule:

  1. Start at (qualification x city) for every city in Shandong.
  2. A combination that fills all 30 pages is OVERFLOW — never accept it
     as complete; drill down to that city's counties and run each county.
  3. A county combination that still fills 30 pages is OVERFLOW_COUNTY
     and is reported for a human decision, not silently truncated.

Qualification names and county names must come from the platform's own
filter dialogs (see docs/COLLECTOR.md). They are loaded from JSON files
next to this module when present:

  qualifications.json    ["建筑工程施工总承包一级", ...]  (verbatim names)
  shandong_counties.json {"济南": ["历下区", ...], ...}

Until those files are filled in from a scouting pass, only the one
historically verified qualification is known.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROVINCE = "山东"
PAGE_SIZE = 15
MAX_PAGES = 30
DISPLAY_LIMIT = PAGE_SIZE * MAX_PAGES  # 450

# Shandong's 16 prefecture-level cities (public administrative divisions;
# used as the level-1 traversal grid and as a checklist for scouting).
SHANDONG_CITIES: tuple[str, ...] = (
    "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁",
    "泰安", "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽",
)

# The only qualification verified on the platform so far (2026-11-21 run).
KNOWN_QUALIFICATIONS: tuple[str, ...] = ("建筑工程施工总承包一级",)

_HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class QueryCombo:
    qualification: str
    province: str = PROVINCE
    city: str = ""      # "" = whole province (legacy single-combo mode)
    county: str = ""    # "" = whole city

    @property
    def level(self) -> str:
        if self.county:
            return "county"
        if self.city:
            return "city"
        return "province"

    @property
    def region_label(self) -> str:
        return self.county or self.city or self.province

    def slug(self) -> str:
        raw = "_".join(p for p in (self.province, self.city, self.county, self.qualification) if p)
        ascii_part = re.sub(r"[^0-9A-Za-z]+", "-", raw).strip("-")
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"{ascii_part}-{digest}" if ascii_part else digest

    def output_name(self) -> str:
        # The legacy province-level file keeps its historic name so the
        # 356-record resume state stays valid.
        if self.level == "province" and self.qualification == KNOWN_QUALIFICATIONS[0]:
            return "mohurd_shandong_level1.json"
        return f"mohurd_{self.slug()}.json"

    def as_dict(self) -> dict[str, str]:
        return {
            "qualification": self.qualification,
            "province": self.province,
            "city": self.city,
            "county": self.county,
        }


def load_qualifications(path: Path | None = None) -> list[str]:
    path = path or (_HERE / "qualifications.json")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
            return list(value)
        raise ValueError(f"{path} must be a JSON array of qualification names")
    return list(KNOWN_QUALIFICATIONS)


def load_counties(path: Path | None = None) -> dict[str, list[str]]:
    path = path or (_HERE / "shandong_counties.json")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return {str(k): [str(v) for v in vs] for k, vs in value.items()}
        raise ValueError(f"{path} must be a JSON object of city -> [counties]")
    return {}


def city_combos(qualifications: list[str], cities: tuple[str, ...] = SHANDONG_CITIES) -> list[QueryCombo]:
    """Level-1 plan: every qualification x every Shandong city."""
    return [QueryCombo(qualification=q, city=c) for q in qualifications for c in cities]


def drill_down(combo: QueryCombo, counties_by_city: dict[str, list[str]]) -> list[QueryCombo]:
    """County combos for an OVERFLOW city combo. Empty list = county data
    has not been enumerated from the platform yet; caller must report that
    instead of treating the city result as complete."""
    if combo.level != "city":
        return []
    return [
        QueryCombo(qualification=combo.qualification, city=combo.city, county=county)
        for county in counties_by_city.get(combo.city, [])
    ]


def is_overflow(pages_seen: int, records_seen: int, max_pages: int = MAX_PAGES) -> bool:
    """A run hit the display ceiling if it filled every allowed page."""
    return pages_seen >= max_pages and records_seen >= DISPLAY_LIMIT - PAGE_SIZE + 1


@dataclass
class TraversalPlan:
    combos: list[QueryCombo] = field(default_factory=list)
    overflow: list[QueryCombo] = field(default_factory=list)
    overflow_county: list[QueryCombo] = field(default_factory=list)
    needs_county_data: list[QueryCombo] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "combos": len(self.combos),
            "overflow": len(self.overflow),
            "overflow_county": len(self.overflow_county),
            "needs_county_data": len(self.needs_county_data),
        }
