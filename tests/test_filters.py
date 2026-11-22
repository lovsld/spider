"""Unit tests for collector/filters.py (pure logic, no browser/network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from filters import (  # noqa: E402
    DISPLAY_LIMIT,
    SHANDONG_CITIES,
    QueryCombo,
    city_combos,
    drill_down,
    is_overflow,
    load_counties,
    load_qualification_entries,
    load_qualifications,
    entry_combos,
    county_combos,
    region_fullname,
)


def test_display_limit_is_450():
    assert DISPLAY_LIMIT == 450


def test_shandong_has_17_cities_per_platform_dictionary():
    assert len(SHANDONG_CITIES) == 17
    assert "济南" in SHANDONG_CITIES and "青岛" in SHANDONG_CITIES


def test_city_combos_cover_every_qualification_and_city():
    combos = city_combos(["甲资质", "乙资质"])
    assert len(combos) == 2 * len(SHANDONG_CITIES)
    assert all(c.level == "city" for c in combos)


def test_legacy_combo_keeps_historic_filename():
    combo = QueryCombo(qualification="建筑工程施工总承包一级")
    assert combo.output_name() == "mohurd_shandong_level1.json"


def test_other_combos_get_unique_filenames():
    a = QueryCombo(qualification="建筑工程施工总承包一级", city="济南")
    b = QueryCombo(qualification="建筑工程施工总承包一级", city="青岛")
    assert a.output_name() != b.output_name()
    assert a.output_name().endswith(".json")


def test_drill_down_only_from_city_and_needs_county_data():
    combo = QueryCombo(qualification="甲资质", city="济南")
    assert drill_down(combo, {}) == []
    children = drill_down(combo, {"济南": ["历下区", "市中区"]})
    assert [c.county for c in children] == ["历下区", "市中区"]
    assert all(c.level == "county" for c in children)


def test_is_overflow():
    assert is_overflow(30, 450)
    assert is_overflow(30, 445)
    assert not is_overflow(29, 435)
    assert not is_overflow(30, 100)


def test_loaders_fall_back_until_scouting_files_exist():
    quals = load_qualifications(Path("/nonexistent/qualifications.json"))
    assert quals == ["建筑工程施工总承包一级"]
    assert load_counties(Path("/nonexistent/counties.json")) == {}


def test_source_query_dict():
    combo = QueryCombo(qualification="甲", city="济南", county="历下区")
    assert combo.as_dict() == {
        "qualification": "甲", "province": "山东", "city": "济南", "county": "历下区",
    }


def test_platform_dictionary_files():
    entries = load_qualification_entries()
    assert len(entries) == 599
    by_code = {e["code"]: e["name"] for e in entries if e["code"]}
    assert by_code["D101A"] == "建筑工程施工总承包一级"
    assert {e["category"] for e in entries} == {"工程设计", "工程勘察", "建筑业企业资质", "工程监理"}
    counties = load_counties()
    assert len(counties) == 17 and "莱芜" in counties  # platform dictionary still lists 莱芜
    assert sum(len(v) for v in counties.values()) == 137
    assert counties["济南"][0] == "历下区"
    assert len(city_combos(load_qualifications())) == 599 * 17


def test_ui_category_mapping():
    assert QueryCombo(qualification="x", category="建筑业企业资质").ui_category == "建筑业企业"
    assert QueryCombo(qualification="x", category="工程设计").ui_category == "工程设计"
    assert QueryCombo(qualification="x").ui_category == "建筑业企业"  # legacy default


def test_entry_combos_carry_code_and_category_and_filter():
    entries = load_qualification_entries()
    d = entry_combos(entries, category="D")
    assert len(d) == 143 * 17
    assert all(c.category == "建筑业企业资质" and c.code.startswith("D") for c in d)
    a = entry_combos(entries, category="工程设计")
    assert len(a) == 396 * 17
    one = [c for c in d if c.code == "D101A" and c.city == "济南"][0]
    assert one.qualification == "建筑工程施工总承包一级"
    assert one.as_dict()["code"] == "D101A"


def test_region_fullname_from_platform_dictionary():
    assert region_fullname(QueryCombo(qualification="x", city="济南")) == "山东省-济南市"
    assert region_fullname(QueryCombo(qualification="x", city="济南", county="历下区")) == "山东省-济南市-历下区"
    assert region_fullname(QueryCombo(qualification="x", city="莱芜", county="钢城区")) == "山东省-莱芜市-钢城区"
    assert region_fullname(QueryCombo(qualification="x")) == "山东省"


def test_county_combos_are_disjoint_and_complete():
    entries = load_qualification_entries()
    counties = load_counties()
    combos = county_combos(entries, counties)
    assert len(combos) == 599 * 137
    pairs = {(c.city, c.county) for c in combos if c.code == "D101A"}
    assert len(pairs) == 137  # every county exactly once per qualification -> no region overlap
    d = county_combos(entries, counties, category="D")
    assert len(d) == 143 * 137
    one = [c for c in combos if c.code == "D101A" and c.county == "历下区"][0]
    assert one.city == "济南" and one.level == "county"


def test_dictionary_diffs():
    from filters import diff_counties, diff_qualifications

    old = [{"name": "甲级", "code": "X1"}, {"name": "乙级", "code": "X2"}]
    new = [{"name": "甲级", "code": "X1"}, {"name": "乙级改", "code": "X2"}, {"name": "丙级", "code": "X3"}]
    d = diff_qualifications(old, new)
    assert d["added"] == ["X3 丙级"] and d["removed"] == [] and d["renamed"] == ["X2 乙级 -> 乙级改"]
    dc = diff_counties({"济南": ["历下区"]}, {"济南": ["历下区", "市中区"]})
    assert dc == {"added": ["济南/市中区"], "removed": []}
