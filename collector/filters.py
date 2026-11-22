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
dictionaries (see docs/COLLECTOR.md). They are loaded from JSON files
next to this module when present:

  qualifications_tree.json  the platform qualification dictionary, verbatim
  qualifications.json       flat [{"name", "code", "category", "group"}]
                            flattened from the tree (599 entries, 2026-11-22)
  regions_shandong.json     the platform region dictionary for Shandong,
                            verbatim (17 cities / 137 counties)
  shandong_counties.json    {"济南": ["历下区", ...], ...} derived from it

Both dictionaries were supplied by Hu from the platform on 2026-11-22.
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

# Shandong's cities AS LISTED IN THE PLATFORM'S OWN REGION DICTIONARY
# (regions_shandong.json): 17 entries. Note the dictionary still lists
# 莱芜 (371200) even though it was merged into Jinan administratively in
# 2019 — the platform dictionary governs what its selectors offer, so
# traversal follows the dictionary, not the current administrative map.
SHANDONG_CITIES: tuple[str, ...] = (
    "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁",
    "泰安", "威海", "日照", "莱芜", "临沂", "德州", "聊城", "滨州", "菏泽",
)

# The only qualification verified on the platform so far (2026-11-21 run).
KNOWN_QUALIFICATIONS: tuple[str, ...] = ("建筑工程施工总承包一级",)

_HERE = Path(__file__).resolve().parent


# Dictionary category -> the category label the platform filter UI shows.
# "建筑业企业" is verified from the original collector; the other three use
# the dictionary's own top-level names (verify once on the first real run).
CATEGORY_UI_LABELS: dict[str, str] = {
    "工程设计": "工程设计",
    "工程勘察": "工程勘察",
    "建筑业企业资质": "建筑业企业",
    "工程监理": "工程监理",
}

# Category can also be addressed by its dictionary code prefix.
CATEGORY_CODES: dict[str, str] = {
    "A": "工程设计",
    "B": "工程勘察",
    "D": "建筑业企业资质",
    "E": "工程监理",
}


@dataclass(frozen=True)
class QueryCombo:
    qualification: str
    province: str = PROVINCE
    city: str = ""      # "" = whole province (legacy single-combo mode)
    county: str = ""    # "" = whole city
    code: str = ""      # platform dictionary code, e.g. "D101A"
    category: str = ""  # dictionary top-level category, e.g. "建筑业企业资质"

    @property
    def ui_category(self) -> str:
        """Category label to click in the platform filter UI."""
        return CATEGORY_UI_LABELS.get(self.category, "建筑业企业")

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
        # The legacy province-level combo keeps its historic output name.
        if self.level == "province" and self.qualification == KNOWN_QUALIFICATIONS[0]:
            return "mohurd_shandong_level1.json"
        return f"mohurd_{self.slug()}.json"

    def as_dict(self) -> dict[str, str]:
        out = {
            "qualification": self.qualification,
            "province": self.province,
            "city": self.city,
            "county": self.county,
        }
        if self.code:
            out["code"] = self.code
        if self.category:
            out["category"] = self.category
        return out


def load_qualification_entries(path: Path | None = None) -> list[dict[str, str]]:
    """Flat qualification entries: [{"name", "code", "category", "group"}].

    Accepts the object form above and the legacy plain-name list form.
    """
    path = path or (_HERE / "qualifications.json")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list) and value and all(isinstance(v, dict) and v.get("name") for v in value):
            return [
                {
                    "name": str(v["name"]),
                    "code": str(v.get("code", "")).strip(),
                    "category": str(v.get("category", "")),
                    "group": str(v.get("group", "")),
                }
                for v in value
            ]
        if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
            return [{"name": v, "code": "", "category": "", "group": ""} for v in value]
        raise ValueError(f"{path} must be a JSON array of qualification entries or names")
    return [{"name": q, "code": "", "category": "", "group": ""} for q in KNOWN_QUALIFICATIONS]


def load_qualifications(path: Path | None = None) -> list[str]:
    return [entry["name"] for entry in load_qualification_entries(path)]


def load_counties(path: Path | None = None) -> dict[str, list[str]]:
    path = path or (_HERE / "shandong_counties.json")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return {str(k): [str(v) for v in vs] for k, vs in value.items()}
        raise ValueError(f"{path} must be a JSON object of city -> [counties]")
    return {}


def entry_combos(
    entries: list[dict[str, str]],
    cities: tuple[str, ...] = SHANDONG_CITIES,
    category: str = "",
) -> list[QueryCombo]:
    """Level-1 plan from dictionary entries: qualification x city.

    `category` limits the plan to one dictionary category, given either by
    its Chinese name ("工程设计") or its code prefix ("A"/"B"/"D"/"E").
    """
    wanted = CATEGORY_CODES.get(category.strip().upper(), category.strip())
    combos: list[QueryCombo] = []
    for entry in entries:
        if wanted and entry.get("category") != wanted:
            continue
        for city in cities:
            combos.append(
                QueryCombo(
                    qualification=entry["name"],
                    city=city,
                    code=entry.get("code", ""),
                    category=entry.get("category", ""),
                )
            )
    return combos


def city_combos(qualifications: list[str], cities: tuple[str, ...] = SHANDONG_CITIES) -> list[QueryCombo]:
    """Level-1 plan: every qualification x every Shandong city."""
    return [QueryCombo(qualification=q, city=c) for q in qualifications for c in cities]


def load_region_tree(path: Path | None = None) -> dict:
    path = path or (_HERE / "regions_shandong.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def region_fullname(combo: QueryCombo, tree: dict | None = None) -> str:
    """Full region label as result rows show it, e.g. 山东省-济南市-历下区.

    Built from the platform region dictionary's own region_fullname
    values, so county names that repeat across cities (市中区 etc.) and
    names whose short form lacks a suffix (章丘 vs 章丘市) validate
    correctly. Falls back to the combo's region_label when the
    dictionary has no matching entry.
    """
    tree = tree if tree is not None else load_region_tree()
    if not tree:
        return combo.region_label
    if not combo.city:
        return str(tree.get("region_fullname") or combo.province)
    for city in tree.get("children", []):
        if city.get("region_name") == combo.city:
            if not combo.county:
                return str(city.get("region_fullname") or combo.city)
            for county in city.get("children", []):
                if county.get("region_name") == combo.county or county.get("region_fullname", "").endswith(combo.county):
                    return str(county.get("region_fullname") or combo.county)
            return str(city.get("region_fullname") or combo.city)
    return combo.region_label


def drill_down(combo: QueryCombo, counties_by_city: dict[str, list[str]]) -> list[QueryCombo]:
    """County combos for an OVERFLOW city combo. Empty list = county data
    has not been enumerated from the platform yet; caller must report that
    instead of treating the city result as complete."""
    if combo.level != "city":
        return []
    return [
        QueryCombo(
            qualification=combo.qualification,
            city=combo.city,
            county=county,
            code=combo.code,
            category=combo.category,
        )
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
