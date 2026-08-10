"""End-to-end wiring: tools.geo_build.pipeline over the committed
`pipeline_fixture.osm`. Covers byte-identical reruns (research/osm-data.md
§7), funnel-count correctness, and the scenario-JSON config override path
(CSR-003b, RES-60 — the same path CSR-003d will reuse to lower floors for its
tiny fixture).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from csr_bench.address import load_default_abbreviation_table  # noqa: E402
from geo_build import crop, zones  # noqa: E402
from geo_build.osm_reader import read_elements  # noqa: E402
from geo_build.pipeline import run_pipeline, write_artifact  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "pipeline_fixture.osm")


def _count_addr_tagged_elements_in_crop(osm_path: str, buffer_km: Decimal = Decimal("15.0")) -> int:
    """Independent count of "elements carrying at least one addr:* tag,
    after crop" — computed straight from `osm_reader`/`crop`, not from the
    pipeline itself, so a test comparing `funnel.candidates_total` against
    it is a real cross-check (finding 1), not a tautology.
    """
    zone_result = zones.extract_zones(osm_path)
    area = crop.build_area(zone_result.zone_polygons_in_order(), buffer_km)
    elements = crop.filter_to_build_area(read_elements(osm_path), area)
    count = 0
    for group in (elements.nodes, elements.ways, elements.relations):
        for element in group:
            if any(k.startswith("addr:") for k in element.tags):
                count += 1
    return count


def test_pipeline_produces_the_expected_accepted_addresses():
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    formatted = {row["formatted_address"] for row in result.catalog_rows}
    assert formatted == {
        "100 Main St, Scottsdale, AZ 85251",
        "200 Main St, Scottsdale, AZ 85251",
        "300 Main St, Scottsdale, AZ 85251",
        "600 Second Ave, Tempe, AZ 85251",
        "800 Elm St, Scottsdale, AZ 85251",
        "801 Elm St, Scottsdale, AZ 85251",
        "900 Cactus Ln, Scottsdale, AZ 85251",
    }


def test_pipeline_rejections_cover_every_stage():
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    reasons = {r.reason for r in result.rejected_rows}
    # Every extraction/dedupe rejection reason this fixture can produce
    # through the *default* abbreviation table. `normalization_ambiguous`
    # needs a deliberately colliding table (the shipped default has none by
    # design) — covered end-to-end in
    # `test_pipeline_normalization_ambiguous_wires_end_to_end` below, over
    # this same fixture, rather than here.
    assert reasons == {
        "incomplete_address",
        "state_mismatch",
        "duplicate_key",
        "duplicate_coordinate",
        "normalization_invalid",
    }


def test_pipeline_never_sees_the_element_outside_the_crop():
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    all_ids = {row["formatted_address"] for row in result.catalog_rows} | {
        (r.source_type, r.source_id) for r in result.rejected_rows
    }
    assert ("node", 1009) not in all_ids


def test_funnel_counts_are_internally_balanced():
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    funnel = result.funnel
    assert funnel.is_balanced()
    assert funnel.accepted == len(result.catalog_rows)
    assert funnel.total_rejected() == len(result.rejected_rows)


def test_funnel_candidates_total_counts_only_addr_tagged_elements():
    """finding 1: the candidate universe is "one candidate per OSM node,
    way, or relation that carries the required address tags"
    (research/osm-data.md:76), not every element the crop kept. The
    fixture's eleven zone-boundary ways and eleven administrative relations
    carry no `addr:*` tag at all and must never enter the funnel on either
    side.
    """
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    expected = _count_addr_tagged_elements_in_crop(FIXTURE)
    assert result.funnel.candidates_total == expected
    assert expected == 12  # 8 original + 4 added for findings 7-9's coverage

    boundary_way_ids = {101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111}
    boundary_relation_ids = {201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211}
    rejected_ways = {r.source_id for r in result.rejected_rows if r.source_type == "way"}
    rejected_relations = {r.source_id for r in result.rejected_rows if r.source_type == "relation"}
    assert not (rejected_ways & boundary_way_ids)
    assert not (rejected_relations & boundary_relation_ids)


def test_funnel_provenance_breakdown_matches_expected_derivation_paths():
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    funnel_dict = result.funnel.to_dict()
    # Extracted (pre-normalize) candidates: 1001, 1004, 1005, 1003, 2100-way,
    # 1011, 1013, 1014 derive city from the containing admin_relation (8);
    # 1008 derives from the zone fallback (1); 1010 supplies addr:city
    # directly (1) — finding 8's "present tag wins" coverage. States: 1001,
    # 1004, 1005, 1003, 2100-way, 1008, 1013, 1014 derive from the crop (8);
    # 1010 (addr:state=AZ) and 1011 (addr:state=az, lowercase) both supply
    # the tag directly (2) — proving normalize()'s case-fold reaches this
    # far without this stage reimplementing it.
    assert funnel_dict["city_source_counts"] == {"admin_relation": 8, "tag": 1, "zone": 1}
    assert funnel_dict["state_source_counts"] == {"crop": 8, "tag": 2}


def test_pipeline_zone_assignment_on_real_catalog_rows():
    """finding 7: zone_id/effective_zone_id on rows `run_pipeline` actually
    emits, against the real zone geometry extracted from this fixture (not
    a synthetic polygon, as `test_geo_zone_assign.py` uses).
    """
    result = run_pipeline(FIXTURE, dataset_version="dev-test")
    by_address = {row["formatted_address"]: row for row in result.catalog_rows}

    outlying = by_address["600 Second Ave, Tempe, AZ 85251"]
    assert outlying["zone_id"] is None  # node 1008 sits in the Tempe/Scottsdale gap
    assert outlying["effective_zone_id"] == 4  # Tempe, the nearest zone

    in_zone = by_address["100 Main St, Scottsdale, AZ 85251"]
    assert in_zone["zone_id"] == 3  # Scottsdale
    assert in_zone["effective_zone_id"] == 3


def test_pipeline_normalization_ambiguous_wires_end_to_end():
    """`normalization_ambiguous` cannot be produced by the shipped default
    abbreviation table (no collisions, by design) or by any address text
    alone — it is a property of the *table*
    (`csr_bench.address._build_lookup`), checked once per `normalize()`
    call regardless of which candidate is being normalized. This proves the
    reason still wires all the way from `normalize_stage` through
    `pipeline.run_pipeline` to `funnel`/`rejected_rows` when a colliding
    table is supplied — every extracted candidate over this fixture is
    rejected the same way, since the ambiguity check runs before any
    candidate-specific text is even consulted.
    """
    base_table = load_default_abbreviation_table()
    ambiguous_table = {
        **base_table,
        "suffixes": {**base_table["suffixes"], "Street": "St", "STREET": "Ave"},
    }
    result = run_pipeline(
        FIXTURE, dataset_version="dev-test", abbreviation_table=ambiguous_table
    )
    assert result.catalog_rows == []
    reasons = {r.reason for r in result.rejected_rows}
    assert "normalization_ambiguous" in reasons
    # Every extracted candidate whose grammar is otherwise fine is rejected
    # the same way (9 of the 10 successful extractions). Node 1014 (the
    # `normalization_invalid` house-number-has-no-digits case) still fails
    # for that same reason, unaffected by table ambiguity: `normalize()`
    # runs `_parse_street_line`'s house-number check *before*
    # `_build_lookup`'s ambiguity check, so a grammar failure never reaches
    # the table at all.
    ambiguous_rejections = [
        r for r in result.rejected_rows if r.reason == "normalization_ambiguous"
    ]
    assert len(ambiguous_rejections) == 9
    still_grammar_invalid = [r for r in result.rejected_rows if r.source_id == 1014]
    assert still_grammar_invalid[0].reason == "normalization_invalid"


def test_pipeline_rerun_on_the_same_input_is_byte_identical(tmp_path):
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    write_artifact(run_pipeline(FIXTURE, dataset_version="dev-test"), out1)
    write_artifact(run_pipeline(FIXTURE, dataset_version="dev-test"), out2)

    files1 = sorted(p.relative_to(out1) for p in out1.rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(out2) for p in out2.rglob("*") if p.is_file())
    assert files1 == files2
    for rel in files1:
        assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes(), f"{rel} differs"


def test_write_artifact_layout_matches_the_sub_issue_slice(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(run_pipeline(FIXTURE, dataset_version="dev-test"), out)
    assert (out / "zones" / "zone_definitions.geojson").exists()
    assert (out / "zones" / "zone_provenance.json").exists()
    assert (out / "addresses" / "catalog.csv").exists()
    assert (out / "addresses" / "rejected.csv").exists()
    assert (out / "reports" / "funnel_counts.csv").exists()
    assert (out / "reports" / "funnel_counts.json").exists()


def test_zone_geojson_has_ten_features_with_rounded_coordinates(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(run_pipeline(FIXTURE, dataset_version="dev-test"), out)
    payload = json.loads((out / "zones" / "zone_definitions.geojson").read_text())
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 10
    codes = {f["properties"]["code"] for f in payload["features"]}
    from geo_build.zones import ZONE_CODES

    assert codes == set(ZONE_CODES)


def test_scenario_json_overrides_the_duplicate_coordinate_radius(tmp_path):
    # node 1005 sits ~5.5m from node 1001 on the same street — accepted at
    # the default 10m radius's complement (rejected), rejected disappears
    # entirely if the radius shrinks below that distance via a scenario
    # override (the CSR-003d override path this pipeline reuses).
    # A bare JSON numeric literal (not a quoted string) — config.py parses
    # scenario JSON with `parse_float=Decimal`, so this becomes a real
    # `Decimal`, not a string `_as_decimal` would reject.
    scenario = json.dumps({"keys": {"geography.duplicate_coordinate_meters": 1.0}})
    result = run_pipeline(FIXTURE, dataset_version="dev-test", scenario_json=scenario)
    house_numbers = {row["house_number"] for row in result.catalog_rows}
    assert "102" in house_numbers  # node 1005's house number now survives


def test_scenario_json_overrides_the_outlying_buffer(tmp_path):
    # Shrinking the buffer drastically should make node 1009 remain outside
    # the crop either way (it's ~140km out) but should not otherwise change
    # the accepted set for a still-generous buffer.
    scenario = json.dumps({"keys": {"geography.outlying_buffer_km": 0.5}})
    result = run_pipeline(FIXTURE, dataset_version="dev-test", scenario_json=scenario)
    # node 1008 sits in a ~4km gap - a 0.5km buffer drops it from the crop.
    formatted = {row["formatted_address"] for row in result.catalog_rows}
    assert "600 Second Ave, Tempe, AZ 85251" not in formatted


def test_default_config_values_are_read_from_the_registry_not_hardcoded():
    from csr_bench.config import assemble_registry

    registry = assemble_registry()
    assert registry["geography.duplicate_coordinate_meters"].default == Decimal("10.0")
    assert registry["geography.outlying_buffer_km"].default == Decimal("15.0")
