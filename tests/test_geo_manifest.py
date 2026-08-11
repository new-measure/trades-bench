from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from _geo_fixture import run_fixture_pipeline  # noqa: E402
from geo_build.manifest import (  # noqa: E402
    BUILDER_VERSION,
    PLACEHOLDER,
    REQUIRED_MANIFEST_FIELDS,
    SourceSnapshot,
    build_manifest,
    missing_required_fields,
    tool_records,
    write_manifest,
)
from geo_build.osrm import OSRM_IMAGE_DIGEST, OSRM_VERSION  # noqa: E402
from geo_build.pipeline import write_artifact  # noqa: E402


def _manifest(**overrides):
    payload = {
        "artifact_id": "az-test",
        "artifact_sha256": "0" * 64,
        "source": SourceSnapshot(),
        "crop_polygon_sha256": "1" * 64,
        "crop_buffer_km": 15.0,
        "tools": tool_records("osrm", OSRM_VERSION, OSRM_IMAGE_DIGEST),
        "zone_definitions_sha256": "2" * 64,
        "counts": {
            "candidates": 1,
            "accepted": 1,
            "rejected_by_reason": {},
            "usable_by_zone": {},
            "usable_by_zone_segment": {},
            "outlying": 0,
        },
        "anchor_count": 3,
        "anchor_selection_procedure": {"kind": "fixed_grid_cell_centroid_snap"},
        "anchor_matrix_sha256": "3" * 64,
        "accuracy_gate": {
            "sample_size": 10,
            "seed": "s",
            "median_pct": 1.0,
            "p95_pct": 2.0,
            "thresholds": {"median_pct": 10.0, "p95_pct": 25.0},
            "pass": True,
            "evaluated_pairs": 10,
            "skipped_pairs": 0,
            "min_evaluated_fraction": 0.9,
            "min_evaluated_pairs": 9,
        },
        "routing_backend": {"name": "osrm", "version": OSRM_VERSION, "image_digest": None},
        "packageable": True,
    }
    payload.update(overrides)
    return build_manifest(**payload)


def test_manifest_carries_every_required_field():
    assert missing_required_fields(_manifest()) == []


def test_missing_required_fields_actually_detects_a_gap():
    manifest = _manifest()
    del manifest["anchor_matrix_sha256"]
    manifest["accuracy_gate"].pop("seed")
    manifest["accuracy_gate"].pop("evaluated_pairs")
    assert set(missing_required_fields(manifest)) == {
        "anchor_matrix_sha256",
        "accuracy_gate.seed",
        "accuracy_gate.evaluated_pairs",
    }


def test_the_gate_verdict_carries_its_own_sample_sufficiency():
    assert {
        "accuracy_gate.evaluated_pairs",
        "accuracy_gate.skipped_pairs",
        "accuracy_gate.min_evaluated_fraction",
        "accuracy_gate.min_evaluated_pairs",
    } <= set(REQUIRED_MANIFEST_FIELDS)


def test_required_field_list_matches_section_7():
    assert set(REQUIRED_MANIFEST_FIELDS) >= {
        "artifact_id",
        "artifact_sha256",
        "built_at",
        "builder_version",
        "source.url",
        "source.published_at",
        "source.bytes",
        "source.sha256",
        "crop.polygon_sha256",
        "crop.buffer_km",
        "tools",
        "zone_definitions_sha256",
        "counts.candidates",
        "counts.accepted",
        "counts.rejected_by_reason",
        "counts.usable_by_zone",
        "counts.usable_by_zone_segment",
        "counts.outlying",
        "anchors.count",
        "anchors.selection_procedure",
        "anchor_matrix_sha256",
        "accuracy_gate.sample_size",
        "accuracy_gate.seed",
        "accuracy_gate.median_pct",
        "accuracy_gate.p95_pct",
        "accuracy_gate.thresholds",
        "accuracy_gate.pass",
        "license",
    }


def test_source_placeholder_is_explicit_for_a_fixture_build():
    source = SourceSnapshot()
    assert source.is_placeholder
    payload = source.to_dict()
    assert payload["url"] == PLACEHOLDER
    assert payload["sha256"] == PLACEHOLDER
    assert payload["bytes"] == 0
    assert payload["placeholder"] is True


def test_a_fully_supplied_source_is_not_a_placeholder():
    source = SourceSnapshot(
        url="https://download.geofabrik.de/north-america/us/arizona.osm.pbf",
        published_at="2026-08-01",
        bytes=1234,
        sha256="f" * 64,
    )
    assert not source.is_placeholder
    assert source.to_dict()["placeholder"] is False


def test_builder_version_names_both_halves():
    assert BUILDER_VERSION.startswith("csr_bench/")
    assert "+geo_build/" in BUILDER_VERSION


def test_tool_records_pin_the_container_digest():
    tools = {t["name"]: t for t in tool_records("osrm", OSRM_VERSION, OSRM_IMAGE_DIGEST)}
    assert tools["osrm"]["image_digest"] == OSRM_IMAGE_DIGEST
    assert tools["osrm"]["image_digest"].startswith("sha256:")
    assert tools["osrm"]["version"] == OSRM_VERSION
    assert {"python", "pyosmium", "shapely"} <= set(tools)
    assert tools["python"]["image_digest"] is None


def test_license_block_records_odbl_and_the_attribution_line():
    manifest = _manifest()
    assert manifest["license"]["id"] == "ODbL-1.0"
    assert "OpenStreetMap contributors" in manifest["license"]["attribution"]


def test_built_at_is_the_only_wall_clock_value_and_is_caller_overridable():
    manifest = _manifest(built_at="2020-01-01T00:00:00Z")
    assert manifest["built_at"] == "2020-01-01T00:00:00Z"
    assert _manifest()["built_at"].endswith("Z")


def test_manifest_json_is_sorted_and_newline_terminated(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, _manifest(built_at="2020-01-01T00:00:00Z"))
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    keys = list(json.loads(text))
    assert keys == sorted(keys)


def test_a_real_build_manifest_is_complete_and_marked_non_packageable(tmp_path):
    out = tmp_path / "geography-dev"
    result = run_fixture_pipeline()
    manifest = write_artifact(result, out)
    assert missing_required_fields(manifest) == []
    assert manifest["packageable"] is False
    assert manifest["counts"]["candidates"] == 12
    assert manifest["counts"]["accepted"] == 7
    assert manifest["counts"]["outlying"] == 1
    assert manifest["anchors"]["count"] == len(result.routing.anchors)
    assert manifest["anchors"]["selection_procedure"]["cell_km"] == 0.5
    assert manifest["accuracy_gate"]["pass"] is True
    assert manifest["crop"]["buffer_km"] == 15.0
    assert manifest["zone_definitions_sha256"] != manifest["anchor_matrix_sha256"]

    on_disk = json.loads(Path(out / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
