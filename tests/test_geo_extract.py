from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from csr_bench.address import load_default_abbreviation_table  # noqa: E402
from geo_build import crop, extract, normalize_stage, zones  # noqa: E402
from geo_build.osm_reader import read_elements  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "pipeline_fixture.osm")
ZONES_ONLY_FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "zones_basic.osm")
TABLE = load_default_abbreviation_table()


def _extract_from(path: str):
    zone_result = zones.extract_zones(path)
    area = crop.build_area(zone_result.zone_polygons_in_order(), Decimal("15.0"))
    elements = read_elements(path)
    cropped = crop.filter_to_build_area(elements, area)
    ctx = extract.build_extraction_context(zone_result)
    return extract.extract_candidates(cropped, ctx)


def _zones_fixture_with(extra_xml: str, tmp_path: Path) -> str:
    base = Path(ZONES_ONLY_FIXTURE).read_text(encoding="utf-8")
    injected = base.replace("</osm>\n", extra_xml + "</osm>\n")
    assert injected != base
    path = tmp_path / "zones_plus_extra.osm"
    path.write_text(injected, encoding="utf-8")
    return str(path)


def _cropped_elements_and_ctx():
    zone_result = zones.extract_zones(FIXTURE)
    area = crop.build_area(zone_result.zone_polygons_in_order(), Decimal("15.0"))
    elements = read_elements(FIXTURE)
    cropped = crop.filter_to_build_area(elements, area)
    ctx = extract.build_extraction_context(zone_result)
    return cropped, ctx


def _candidates_by_id(candidates):
    return {c.source_id: c for c in candidates}


def _rejected_by_id(rejected):
    return {r.source_id: r for r in rejected}


def test_hard_tag_candidates_accepted():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, rejected = extract.extract_candidates(cropped, ctx)
    accepted_ids = {c.source_id for c in candidates}
    assert {1001, 1003, 2100}.issubset(accepted_ids)


def test_incomplete_address_missing_postcode_is_rejected():
    cropped, ctx = _cropped_elements_and_ctx()
    _candidates, rejected = extract.extract_candidates(cropped, ctx)
    by_id = _rejected_by_id(rejected)
    assert by_id[1006].reason == extract.REASON_INCOMPLETE_ADDRESS
    assert "addr:postcode" in by_id[1006].detail


def test_incomplete_address_non_reducible_zip_is_rejected(tmp_path):
    extra = (
        '  <node id="5001" lat="33.55" lon="-111.79">\n'
        '    <tag k="addr:housenumber" v="1"/>\n'
        '    <tag k="addr:street" v="Bad Zip St"/>\n'
        '    <tag k="addr:postcode" v="ABCDE"/>\n'
        "  </node>\n"
    )
    path = _zones_fixture_with(extra, tmp_path)
    _candidates, rejected = _extract_from(path)
    by_id = _rejected_by_id(rejected)
    assert by_id[5001].reason == extract.REASON_INCOMPLETE_ADDRESS
    assert "postcode" in by_id[5001].detail


def test_state_mismatch_is_a_distinct_rejection_reason():
    cropped, ctx = _cropped_elements_and_ctx()
    _candidates, rejected = extract.extract_candidates(cropped, ctx)
    by_id = _rejected_by_id(rejected)
    assert by_id[1007].reason == extract.REASON_STATE_MISMATCH
    assert "CA" in by_id[1007].detail


def test_city_derived_from_admin_relation_when_tag_absent():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, _rejected = extract.extract_candidates(cropped, ctx)
    by_id = _candidates_by_id(candidates)
    node_1001 = by_id[1001]
    assert node_1001.city == "Scottsdale"
    assert node_1001.city_source == "admin_relation"


def test_city_derived_from_zone_fallback_when_outside_every_relation():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, _rejected = extract.extract_candidates(cropped, ctx)
    by_id = _candidates_by_id(candidates)
    outlying = by_id[1008]
    assert outlying.city == "Tempe"
    assert outlying.city_source == "zone"


def test_state_derived_from_crop_when_tag_absent():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, _rejected = extract.extract_candidates(cropped, ctx)
    by_id = _candidates_by_id(candidates)
    assert by_id[1001].state == "AZ"
    assert by_id[1001].state_source == "crop"


def test_addr_unit_tag_is_dropped_and_does_not_affect_unit_relevant_fields():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, _rejected = extract.extract_candidates(cropped, ctx)
    by_id = _candidates_by_id(candidates)
    node_1013 = by_id[1013]

    assert "addr:unit" not in node_1013.tags
    assert node_1013.unit is None

    rejection = normalize_stage.normalize_candidate(node_1013, TABLE)
    assert rejection is None
    assert node_1013.unit is None
    assert node_1013.formatted_address == "900 Cactus Ln, Scottsdale, AZ 85251"


def test_addr_unit_tag_marks_the_candidate_unit_tag_present_even_after_the_tag_is_dropped():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, _rejected = extract.extract_candidates(cropped, ctx)
    by_id = _candidates_by_id(candidates)
    node_1013 = by_id[1013]

    assert "addr:unit" not in node_1013.tags
    assert node_1013.unit_tag_present is True


def test_addr_flats_tag_also_marks_the_candidate_unit_tag_present(tmp_path):
    extra = (
        '  <node id="5002" lat="33.56" lon="-111.79">\n'
        '    <tag k="addr:housenumber" v="1"/>\n'
        '    <tag k="addr:street" v="Flats St"/>\n'
        '    <tag k="addr:postcode" v="85251"/>\n'
        '    <tag k="addr:flats" v="1-6"/>\n'
        "  </node>\n"
    )
    path = _zones_fixture_with(extra, tmp_path)
    candidates, _rejected = _extract_from(path)
    by_id = _candidates_by_id(candidates)
    node_5002 = by_id[5002]

    assert "addr:flats" not in node_5002.tags
    assert node_5002.unit_tag_present is True


def test_a_candidate_without_addr_unit_or_addr_flats_is_not_unit_tag_present():
    cropped, ctx = _cropped_elements_and_ctx()
    candidates, _rejected = extract.extract_candidates(cropped, ctx)
    by_id = _candidates_by_id(candidates)
    assert by_id[1001].unit_tag_present is False


def test_relation_candidate_uses_member_way_centroid(tmp_path):
    extra = (
        '  <node id="3001" lat="33.56" lon="-111.80"/>\n'
        '  <node id="3002" lat="33.56" lon="-111.79"/>\n'
        '  <way id="3100">\n'
        '    <nd ref="3001"/>\n'
        '    <nd ref="3002"/>\n'
        "  </way>\n"
        '  <relation id="3200">\n'
        '    <member type="way" ref="3100" role=""/>\n'
        '    <tag k="addr:housenumber" v="900"/>\n'
        '    <tag k="addr:street" v="Relation Way"/>\n'
        '    <tag k="addr:postcode" v="85251"/>\n'
        "  </relation>\n"
    )
    path = _zones_fixture_with(extra, tmp_path)
    candidates, _rejected = _extract_from(path)
    by_id = _candidates_by_id(candidates)
    assert 3200 in by_id
    rel_candidate = by_id[3200]
    assert rel_candidate.source_type == "relation"
    assert abs(rel_candidate.longitude - (-111.795)) < 1e-6
    assert abs(rel_candidate.latitude - 33.56) < 1e-6
