from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from _geo_fixture import run_fixture_pipeline  # noqa: E402
from geo_build.manifest import (  # noqa: E402
    PLACEHOLDER,
    REQUIRED_MANIFEST_FIELDS,
    SourceSnapshot,
    build_manifest,
    missing_required_fields,
    write_manifest,
)
from geo_build.pipeline import write_artifact  # noqa: E402


def _manifest(**overrides):
    payload = {
        "artifact_id": "az-test",
        "artifact_sha256": "0" * 64,
        "source": SourceSnapshot(),
        "zone_definitions_sha256": "2" * 64,
        "catalog_rows": 9,
        "anchor_count": 3,
    }
    payload.update(overrides)
    return build_manifest(**payload)


def test_manifest_carries_every_required_field():
    assert missing_required_fields(_manifest()) == []


def test_missing_required_fields_actually_detects_a_gap():
    manifest = _manifest()
    del manifest["zone_definitions_sha256"]
    manifest["counts"].pop("catalog_rows")
    assert set(missing_required_fields(manifest)) == {
        "zone_definitions_sha256",
        "counts.catalog_rows",
    }


def test_required_field_list_is_exactly_the_engine_read_surface():
    assert set(REQUIRED_MANIFEST_FIELDS) == {
        "artifact_id",
        "artifact_sha256",
        "source.url",
        "source.published_at",
        "source.bytes",
        "source.sha256",
        "zone_definitions_sha256",
        "counts.catalog_rows",
        "anchors.count",
        "license",
    }


def test_source_placeholder_is_explicit_for_a_fixture_build():
    source = SourceSnapshot()
    assert source.is_placeholder
    payload = source.to_dict()
    assert payload["url"] == PLACEHOLDER
    assert payload["sha256"] == PLACEHOLDER
    assert payload["bytes"] == 0
    assert set(payload) == {"url", "published_at", "bytes", "sha256"}


def test_a_fully_supplied_source_is_not_a_placeholder():
    source = SourceSnapshot(
        url="https://download.geofabrik.de/north-america/us/arizona.osm.pbf",
        published_at="2026-08-01",
        bytes=1234,
        sha256="f" * 64,
    )
    assert not source.is_placeholder


def test_license_block_records_odbl_and_the_attribution_line():
    manifest = _manifest()
    assert manifest["license"]["id"] == "ODbL-1.0"
    assert "OpenStreetMap contributors" in manifest["license"]["attribution"]


def test_manifest_json_is_sorted_and_newline_terminated(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, _manifest())
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    keys = list(json.loads(text))
    assert keys == sorted(keys)


def test_a_real_build_manifest_is_complete_and_minimal(tmp_path):
    out = tmp_path / "geography-dev"
    result = run_fixture_pipeline()
    manifest = write_artifact(result, out)
    assert missing_required_fields(manifest) == []
    assert set(manifest) == {
        "artifact_id",
        "artifact_sha256",
        "source",
        "zone_definitions_sha256",
        "counts",
        "anchors",
        "license",
    }
    assert manifest["counts"] == {"catalog_rows": len(result.catalog_rows)}
    assert manifest["anchors"] == {"count": len(result.routing.anchors)}
    assert manifest["zone_definitions_sha256"]

    on_disk = json.loads(Path(out / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
