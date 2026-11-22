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
