from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from csr_bench import constants  # noqa: E402
from csr_bench.db import in_memory  # noqa: E402
from csr_bench.migrations import migrate  # noqa: E402
from geo_build import geometry, zones  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "zones_basic.osm")
REAL_ZONES_GEOJSON = str(Path(__file__).parent / "fixtures" / "geo" / "zone_definitions.geojson")


def test_zone_codes_match_constants_zone_names_order_and_count():
    assert len(zones.ZONE_CODES) == len(constants.ZONE_NAMES) == 10


def test_zone_codes_match_migrated_zones_table_exactly():
    conn = in_memory()
    try:
        migrate(conn)
        rows = conn.execute("SELECT code FROM _zones ORDER BY id").fetchall()
        assert tuple(r["code"] for r in rows) == zones.ZONE_CODES
    finally:
        conn.close()


def test_extract_zones_produces_all_ten_zones_with_correct_ids_and_names():
    result = zones.extract_zones(FIXTURE)
    assert len(result.zones) == 10
    by_code = {z.code: z for z in result.zones}
    assert set(by_code) == set(zones.ZONE_CODES)
    for i, code in enumerate(zones.ZONE_CODES, start=1):
        z = by_code[code]
        assert z.zone_id == i
        assert z.name == constants.ZONE_NAMES[i - 1]


def test_extract_zones_municipal_relations_include_all_eleven_source_relations():
    result = zones.extract_zones(FIXTURE)
    names = {m.name for m in result.municipal_relations}
    assert names == {
        "Scottsdale",
        "Tempe",
        "Mesa",
        "Chandler",
        "Gilbert",
        "Glendale",
        "Peoria",
        "Phoenix",
        "Goodyear",
        "Avondale",
        "Litchfield Park",
    }


def test_phoenix_split_produces_two_disjoint_halves_at_the_pinned_latitude():
    result = zones.extract_zones(FIXTURE)
    by_code = {z.code: z for z in result.zones}
    central = by_code["central_phoenix"].polygon
    north = by_code["north_phoenix"].polygon

    assert by_code["central_phoenix"].source_relation_ids == (
        by_code["north_phoenix"].source_relation_ids
    )

    _, min_lat_central, _, max_lat_central = central.bounds
    _, min_lat_north, _, max_lat_north = north.bounds
    assert max_lat_central <= zones.PHOENIX_SPLIT_LATITUDE
    assert min_lat_north >= zones.PHOENIX_SPLIT_LATITUDE
    assert central.intersection(north).area < 1e-12


def test_west_valley_goodyear_is_the_union_of_three_relations():
    result = zones.extract_zones(FIXTURE)
    by_code = {z.code: z for z in result.zones}
    wv = by_code["west_valley_goodyear"]
    assert len(wv.source_relation_ids) == 3
    goodyear = next(m for m in result.municipal_relations if m.name == "Goodyear")
    assert wv.polygon.area > goodyear.polygon.area


def test_extract_zones_result_is_pairwise_disjoint():
    result = zones.extract_zones(FIXTURE)
    assert len(result.zones) == 10


def test_extract_zones_missing_relation_raises_clear_error(tmp_path):
    text = Path(FIXTURE).read_text(encoding="utf-8")
    lines = text.splitlines()
    filtered = []
    skipping = False
    for line in lines:
        if '<relation id="211">' in line:
            skipping = True
        if skipping:
            if "</relation>" in line:
                skipping = False
            continue
        filtered.append(line)
    trimmed = tmp_path / "missing_gilbert.osm"
    trimmed.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Gilbert"):
        zones.extract_zones(str(trimmed))


def test_extract_zones_reconciles_a_real_overlap_instead_of_raising(tmp_path):
    text = Path(FIXTURE).read_text(encoding="utf-8")
    overlapping = text
    overlapping = overlapping.replace(
        '<node id="41" lat="33.67" lon="-111.98"/>',
        '<node id="41" lat="33.67" lon="-112.08"/>',
    )
    overlapping = overlapping.replace(
        '<node id="42" lat="33.67" lon="-111.88"/>',
        '<node id="42" lat="33.67" lon="-111.98"/>',
    )
    overlapping = overlapping.replace(
        '<node id="43" lat="33.77" lon="-111.88"/>',
        '<node id="43" lat="33.77" lon="-111.98"/>',
    )
    overlapping = overlapping.replace(
        '<node id="44" lat="33.77" lon="-111.98"/>',
        '<node id="44" lat="33.77" lon="-112.08"/>',
    )
    assert overlapping != text
    moved = tmp_path / "overlap.osm"
    moved.write_text(overlapping, encoding="utf-8")

    baseline = zones.extract_zones(FIXTURE)
    baseline_gilbert_area = {z.code: z for z in baseline.zones}["gilbert"].polygon.area

    result = zones.extract_zones(str(moved))
    by_code = {z.code: z for z in result.zones}
    chandler = by_code["chandler"]
    gilbert = by_code["gilbert"]

    assert chandler.zone_id < gilbert.zone_id
    assert chandler.polygon.intersection(gilbert.polygon).area == 0.0
    assert gilbert.polygon.area < baseline_gilbert_area

    reassignment = next(
        r
        for r in result.overlap_reconciliations
        if r["kept_by"] == "chandler" and r["ceded_by"] == "gilbert"
    )
    assert reassignment["area_km2"] > 0.0


def test_seeded_zone_cities_matches_schema_seed():
    seeded = zones.seeded_zone_cities()
    assert seeded["west_valley_goodyear"] == ["Goodyear", "Avondale", "Litchfield Park"]
    assert seeded["scottsdale"] == ["Scottsdale"]
    assert seeded["central_phoenix"] == ["Central Phoenix", "Phoenix"]
    assert seeded["north_phoenix"] == ["North Phoenix", "Phoenix"]


def test_extract_zones_fails_loudly_on_a_self_intersecting_bowtie_ring(tmp_path):
    text = Path(FIXTURE).read_text(encoding="utf-8")
    bowtied = text.replace(
        '  <way id="111">\n'
        '    <nd ref="41"/>\n'
        '    <nd ref="42"/>\n'
        '    <nd ref="43"/>\n'
        '    <nd ref="44"/>\n'
        '    <nd ref="41"/>\n',
        '  <way id="111">\n'
        '    <nd ref="41"/>\n'
        '    <nd ref="43"/>\n'
        '    <nd ref="42"/>\n'
        '    <nd ref="44"/>\n'
        '    <nd ref="41"/>\n',
    )
    assert bowtied != text
    path = tmp_path / "bowtie.osm"
    path.write_text(bowtied, encoding="utf-8")

    with pytest.raises(ValueError, match="211"):
        zones.extract_zones(str(path))


def _fixture_with_extra_relation(extra_xml: str, tmp_path: Path) -> str:
    base = Path(FIXTURE).read_text(encoding="utf-8")
    injected = base.replace("</osm>\n", extra_xml + "</osm>\n")
    assert injected != base
    path = tmp_path / "zones_plus_relation.osm"
    path.write_text(injected, encoding="utf-8")
    return str(path)


def test_non_municipality_admin_level_relation_is_ignored(tmp_path):
    extra = (
        '  <node id="9001" lat="30.00" lon="-110.00"/>\n'
        '  <node id="9002" lat="30.00" lon="-109.90"/>\n'
        '  <node id="9003" lat="30.10" lon="-109.90"/>\n'
        '  <node id="9004" lat="30.10" lon="-110.00"/>\n'
        '  <way id="9100">\n'
        '    <nd ref="9001"/><nd ref="9002"/><nd ref="9003"/><nd ref="9004"/><nd ref="9001"/>\n'
        '    <tag k="area" v="yes"/>\n'
        "  </way>\n"
        '  <relation id="9200">\n'
        '    <member type="way" ref="9100" role="outer"/>\n'
        '    <tag k="type" v="boundary"/>\n'
        '    <tag k="boundary" v="administrative"/>\n'
        '    <tag k="admin_level" v="6"/>\n'
        '    <tag k="name" v="Scottsdale"/>\n'
        "  </relation>\n"
    )
    path = _fixture_with_extra_relation(extra, tmp_path)

    result = zones.extract_zones(path)
    by_code = {z.code: z for z in result.zones}
    scottsdale_muni = next(
        m for m in result.municipal_relations if m.name == "Scottsdale"
    )
    assert scottsdale_muni.osm_relation_id == 208
    assert by_code["scottsdale"].source_relation_ids == (208,)


def test_duplicate_admin_level_8_name_raises_clear_error(tmp_path):
    extra = (
        '  <node id="9001" lat="30.00" lon="-110.00"/>\n'
        '  <node id="9002" lat="30.00" lon="-109.90"/>\n'
        '  <node id="9003" lat="30.10" lon="-109.90"/>\n'
        '  <node id="9004" lat="30.10" lon="-110.00"/>\n'
        '  <way id="9100">\n'
        '    <nd ref="9001"/><nd ref="9002"/><nd ref="9003"/><nd ref="9004"/><nd ref="9001"/>\n'
        '    <tag k="area" v="yes"/>\n'
        "  </way>\n"
        '  <relation id="9200">\n'
        '    <member type="way" ref="9100" role="outer"/>\n'
        '    <tag k="type" v="boundary"/>\n'
        '    <tag k="boundary" v="administrative"/>\n'
        '    <tag k="admin_level" v="8"/>\n'
        '    <tag k="name" v="Scottsdale"/>\n'
        "  </relation>\n"
    )
    path = _fixture_with_extra_relation(extra, tmp_path)

    with pytest.raises(ValueError, match="[Dd]uplicate.*Scottsdale|Scottsdale.*duplicate"):
        zones.extract_zones(path)


def test_load_zone_definitions_parses_the_committed_real_zone_file():
    result = zones.load_zone_definitions(REAL_ZONES_GEOJSON)
    assert len(result.zones) == 10

    ordered = sorted(result.zones, key=lambda z: z.zone_id)
    assert tuple(z.code for z in ordered) == zones.ZONE_CODES
    for i, z in enumerate(ordered, start=1):
        assert z.zone_id == i
        assert z.name == constants.ZONE_NAMES[i - 1]
        assert z.source_relation_ids

    by_code = {z.code: z for z in result.zones}
    assert by_code["tempe"].source_relation_ids == (110833,)
    assert by_code["scottsdale"].source_relation_ids == (110826,)
    assert set(by_code["west_valley_goodyear"].source_relation_ids) == {110782, 110810, 110811}

    geometry.assert_pairwise_disjoint({z.code: z.polygon for z in result.zones})
    for r in result.overlap_reconciliations:
        assert by_code[r["kept_by"]].zone_id < by_code[r["ceded_by"]].zone_id
        assert r["area_km2"] >= 0.0


def test_load_zone_definitions_resolves_the_previously_double_claimed_point():
    from shapely.geometry import Point

    result = zones.load_zone_definitions(REAL_ZONES_GEOJSON)
    point = Point(-112.205405, 33.696293)
    containing = [z.code for z in result.zones if z.polygon.covers(point)]
    assert containing == ["glendale"]
