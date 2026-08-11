from __future__ import annotations

import json
from decimal import Decimal

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from _geo_fixture import FIXTURE  # noqa: E402
from _geo_fixture import run_fixture_pipeline as _run
from csr_bench.address import load_default_abbreviation_table  # noqa: E402
from csr_bench.geo_artifact import canonical_artifact_sha256  # noqa: E402
from geo_build import crop, zones  # noqa: E402
from geo_build.errors import GateFailure, GeoBuildError  # noqa: E402
from geo_build.osm_reader import read_elements  # noqa: E402
from geo_build.pipeline import BuildParameters, write_artifact  # noqa: E402
from geo_build.routing import FakeBackend  # noqa: E402


def _count_addr_tagged_elements_in_crop(osm_path: str, buffer_km: Decimal = Decimal("15.0")) -> int:
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
    result = _run()
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
    result = _run()
    reasons = {r.reason for r in result.rejected_rows}
    assert reasons == {
        "incomplete_address",
        "state_mismatch",
        "duplicate_key",
        "duplicate_coordinate",
        "normalization_invalid",
    }


def test_pipeline_never_sees_the_element_outside_the_crop():
    result = _run()
    all_ids = {row["formatted_address"] for row in result.catalog_rows} | {
        (r.source_type, r.source_id) for r in result.rejected_rows
    }
    assert ("node", 1009) not in all_ids


def test_funnel_counts_are_internally_balanced():
    result = _run()
    funnel = result.funnel
    assert funnel.is_balanced()
    usable_rows = [row for row in result.catalog_rows if row["status"] == "usable"]
    assert funnel.accepted == len(usable_rows)
    assert funnel.total_rejected() == len(result.rejected_rows)
    assert len(result.catalog_rows) == len(usable_rows)


def test_funnel_candidates_total_counts_only_addr_tagged_elements():
    result = _run()
    expected = _count_addr_tagged_elements_in_crop(FIXTURE)
    assert result.funnel.candidates_total == expected
    assert expected == 12

    boundary_way_ids = {101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111}
    boundary_relation_ids = {201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211}
    rejected_ways = {r.source_id for r in result.rejected_rows if r.source_type == "way"}
    rejected_relations = {r.source_id for r in result.rejected_rows if r.source_type == "relation"}
    assert not (rejected_ways & boundary_way_ids)
    assert not (rejected_relations & boundary_relation_ids)


def test_funnel_provenance_breakdown_matches_expected_derivation_paths():
    result = _run()
    funnel_dict = result.funnel.to_dict()
    assert funnel_dict["city_source_counts"] == {"admin_relation": 8, "tag": 1, "zone": 1}
    assert funnel_dict["state_source_counts"] == {"crop": 8, "tag": 2}


def test_pipeline_zone_assignment_on_real_catalog_rows():
    result = _run()
    by_address = {row["formatted_address"]: row for row in result.catalog_rows}

    outlying = by_address["600 Second Ave, Tempe, AZ 85251"]
    assert outlying["zone_id"] is None
    assert outlying["effective_zone_id"] == 4

    in_zone = by_address["100 Main St, Scottsdale, AZ 85251"]
    assert in_zone["zone_id"] == 3
    assert in_zone["effective_zone_id"] == 3


def _ambiguous_table() -> dict:
    base_table = load_default_abbreviation_table()
    return {
        **base_table,
        "suffixes": {**base_table["suffixes"], "Street": "St", "STREET": "Ave"},
    }


def test_an_ambiguous_abbreviation_table_fails_the_build():
    with pytest.raises(GateFailure, match="geography.min_address_catalog_size"):
        _run(abbreviation_table=_ambiguous_table())


def test_ambiguous_table_rejections_reach_the_funnel_with_the_right_reason():
    from geo_build import extract, normalize_stage
    from geo_build.funnel import FunnelCounts
    from geo_build.osm_reader import read_elements

    table = _ambiguous_table()
    zone_result = zones.extract_zones(FIXTURE)
    area = crop.build_area(zone_result.zone_polygons_in_order(), Decimal("15.0"))
    cropped = crop.filter_to_build_area(read_elements(FIXTURE), area)
    candidates, rejected_extract = extract.extract_candidates(
        cropped, extract.build_extraction_context(zone_result)
    )

    funnel = FunnelCounts()
    funnel.record_extraction(candidates, rejected_extract)
    rejections = [normalize_stage.normalize_candidate(c, table) for c in candidates]
    rejections = [r for r in rejections if r is not None]
    funnel.record_rejections(rejections)

    assert len(rejections) == len(candidates)
    assert funnel.rejected_by_reason["normalization_ambiguous"] == 9
    by_source = {r.source_id: r.reason for r in rejections}
    assert by_source[1014] == "normalization_invalid"


def test_pipeline_rerun_on_the_same_input_is_byte_identical(tmp_path):
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    manifest1 = write_artifact(_run(), out1, built_at="2020-01-01T00:00:00Z")
    manifest2 = write_artifact(_run(), out2, built_at="2999-12-31T23:59:59Z")

    files1 = sorted(p.relative_to(out1) for p in out1.rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(out2) for p in out2.rglob("*") if p.is_file())
    assert files1 == files2
    for rel in files1:
        if rel.as_posix() == "manifest.json":
            continue
        assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes(), f"{rel} differs"

    assert manifest1["artifact_sha256"] == manifest2["artifact_sha256"]
    assert canonical_artifact_sha256(out1) == canonical_artifact_sha256(out2)
    assert {k: v for k, v in manifest1.items() if k != "built_at"} == {
        k: v for k, v in manifest2.items() if k != "built_at"
    }


def test_write_artifact_emits_the_full_section_6_layout(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(_run(), out)
    expected = {
        "ATTRIBUTION.md",
        "LICENSE",
        "addresses/catalog.csv",
        "addresses/rejected.csv",
        "manifest.json",
        "normalization/usps_abbreviations.json",
        "reports/data_quality.md",
        "reports/funnel_counts.csv",
        "reports/funnel_counts.json",
        "routing/anchor_distance.bin",
        "routing/anchors.csv",
        "zones/zone_definitions.geojson",
        "zones/zone_provenance.json",
    }
    actual = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert actual == expected


def test_manifest_records_the_canonical_hash_of_everything_but_itself(tmp_path):
    out = tmp_path / "geography-dev"
    manifest = write_artifact(_run(), out)
    assert manifest["artifact_sha256"] == canonical_artifact_sha256(out)

    before = canonical_artifact_sha256(out)
    (out / "manifest.json").write_text('{"tampered": true}\n', encoding="utf-8")
    assert canonical_artifact_sha256(out) == before
    (out / "addresses" / "catalog.csv").write_text("tampered\n", encoding="utf-8")
    assert canonical_artifact_sha256(out) != before


def test_packaged_catalog_has_no_pending_row_and_a_closed_status_domain(tmp_path):
    result = _run()
    statuses = {row["status"] for row in result.catalog_rows}
    assert statuses <= {"usable", "unusable"}
    assert "pending" not in statuses
    for row in result.catalog_rows:
        if row["status"] == "usable":
            assert row["anchor_id"] is not None
            assert row["access_distance_km"] not in (None, "")
            assert row["unusable_reason"] is None
        else:
            assert row["unusable_reason"] in {"unsnappable", "unroutable"}


def test_write_artifact_refuses_a_tree_that_never_went_through_routing(tmp_path):
    result = _run()
    result.catalog_rows[0]["status"] = "pending"
    with pytest.raises(GeoBuildError, match="pending"):
        write_artifact(result, tmp_path / "out")


def test_unsnappable_rows_are_retained_as_unusable_catalog_rows(tmp_path):
    backend = FakeBackend(unsnappable=[(-111.79, 33.55)])
    result = _run(backend=backend)
    by_address = {row["formatted_address"]: row for row in result.catalog_rows}
    row = by_address["100 Main St, Scottsdale, AZ 85251"]
    assert row["status"] == "unusable"
    assert row["unusable_reason"] == "unsnappable"
    assert row["anchor_id"] is None
    assert row["access_distance_km"] is None

    assert result.funnel.rejected_by_reason["unsnappable"] == 1
    assert result.funnel.accepted == 6
    assert result.funnel.is_balanced()
    assert all(r.reason != "unsnappable" for r in result.rejected_rows)


def test_unroutable_rows_are_retained_as_unusable_catalog_rows():
    backend = FakeBackend(unroutable=[(-111.79, 33.55)])
    result = _run(backend=backend)
    by_address = {row["formatted_address"]: row for row in result.catalog_rows}
    row = by_address["100 Main St, Scottsdale, AZ 85251"]
    assert row["status"] == "unusable"
    assert row["unusable_reason"] == "unroutable"
    assert result.funnel.rejected_by_reason["unroutable"] == 1
    assert result.funnel.is_balanced()


def test_accuracy_gate_fails_on_a_too_coarse_anchor_grid():
    with pytest.raises(GateFailure, match="accuracy gate failed"):
        _run(parameters=BuildParameters(anchor_grid_km=6.0, accuracy_sample_size=200))


def test_data_quality_report_has_all_eight_required_sections(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(_run(), out)
    text = (out / "reports" / "data_quality.md").read_text(encoding="utf-8")
    for heading in (
        "## 1. Source snapshot identity and checksums",
        "## 2. Funnel counts",
        "## 3. Usable addresses by zone, segment, and zone x segment",
        "## 4. Coordinate and ZIP sanity checks",
        "## 5. Anchor statistics",
        "## 6. Accuracy gate",
        "## 7. Known limitations",
        "## 8. Attribution and license",
    ):
        assert heading in text
    assert "Map data © OpenStreetMap contributors" in text


def test_packaged_abbreviation_table_is_the_one_the_build_normalized_with(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(_run(), out)
    packaged = json.loads(
        (out / "normalization" / "usps_abbreviations.json").read_text(encoding="utf-8")
    )
    assert packaged == load_default_abbreviation_table()


def test_license_bundle_is_the_full_odbl_text(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(_run(), out)
    license_text = (out / "LICENSE").read_text(encoding="utf-8")
    assert "Open Data Commons Open Database License (ODbL)" in license_text
    assert len(license_text) > 20_000
    attribution = (out / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "Map data © OpenStreetMap contributors" in attribution
    assert "Geofabrik" in attribution


def test_zone_geojson_has_ten_features_with_rounded_coordinates(tmp_path):
    out = tmp_path / "geography-dev"
    write_artifact(_run(), out)
    payload = json.loads((out / "zones" / "zone_definitions.geojson").read_text())
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 10
    codes = {f["properties"]["code"] for f in payload["features"]}
    from geo_build.zones import ZONE_CODES

    assert codes == set(ZONE_CODES)


def test_scenario_json_overrides_the_duplicate_coordinate_radius(tmp_path):
    result = _run(overrides={"geography.duplicate_coordinate_meters": 1.0})
    house_numbers = {row["house_number"] for row in result.catalog_rows}
    assert "102" in house_numbers


def test_scenario_json_overrides_the_outlying_buffer(tmp_path):
    result = _run(
        overrides={
            "geography.outlying_buffer_km": 0.5,
            "geography.min_outlying_addresses": 0,
        }
    )
    formatted = {row["formatted_address"] for row in result.catalog_rows}
    assert "600 Second Ave, Tempe, AZ 85251" not in formatted


def test_default_config_values_are_read_from_the_registry_not_hardcoded():
    from csr_bench.config import assemble_registry

    registry = assemble_registry()
    assert registry["geography.duplicate_coordinate_meters"].default == Decimal("10.0")
    assert registry["geography.outlying_buffer_km"].default == Decimal("15.0")


def test_zones_from_file_kwarg_runs_end_to_end_with_a_fake_backend():
    from _geo_real_fixture import TEMPE_ZONE_ID, run_tempe_fixture_pipeline

    result = run_tempe_fixture_pipeline(backend=FakeBackend())
    usable = [row for row in result.catalog_rows if row["status"] == "usable"]
    assert usable
    assert {row["zone_id"] for row in usable} == {TEMPE_ZONE_ID}
    assert {row["effective_zone_id"] for row in usable} == {TEMPE_ZONE_ID}
