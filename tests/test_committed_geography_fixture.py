from __future__ import annotations

import array
import csv
import json
from pathlib import Path

from trades_bench.geo_artifact import canonical_artifact_sha256

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "geo"
ARTIFACT_DIR = FIXTURE_DIR / "geography-tempe-fixture"
SCENARIO_PATH = FIXTURE_DIR / "fixture_scenario.json"
TEMPE_ZONE_ID = "4"

REQUIRED_ARTIFACT_FILES: tuple[str, ...] = (
    "manifest.json",
    "zones/zone_definitions.geojson",
    "zones/zone_provenance.json",
    "addresses/catalog.csv",
    "routing/anchors.csv",
    "routing/anchor_distance.bin",
    "normalization/usps_abbreviations.json",
    "ATTRIBUTION.md",
    "LICENSE",
)


def _manifest() -> dict:
    return json.loads((ARTIFACT_DIR / "manifest.json").read_text(encoding="utf-8"))


def _catalog_rows() -> list[dict[str, str]]:
    with open(ARTIFACT_DIR / "addresses" / "catalog.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _scenario_floors() -> dict[str, int]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))["keys"]


def test_the_committed_fixture_artifact_exists():
    assert ARTIFACT_DIR.is_dir()
    for rel in REQUIRED_ARTIFACT_FILES:
        assert (ARTIFACT_DIR / rel).is_file(), rel


def test_the_manifest_parses_and_is_built_from_the_real_snapshot():
    manifest = _manifest()
    snapshot = json.loads(
        (Path(__file__).parent.parent / "data" / "snapshots" / "arizona.source.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["artifact_id"]
    assert manifest["artifact_sha256"]
    assert manifest["source"]["url"] == snapshot["resolved_url"]
    assert manifest["source"]["sha256"] == snapshot["sha256"]


def test_the_canonical_hash_matches_the_manifests_recorded_hash():
    manifest = _manifest()
    assert canonical_artifact_sha256(ARTIFACT_DIR) == manifest["artifact_sha256"]


def test_the_packed_anchor_matrix_loads_with_stdlib_array_and_struct():
    with open(ARTIFACT_DIR / "routing" / "anchors.csv", newline="", encoding="utf-8") as f:
        n = sum(1 for _ in csv.DictReader(f))
    assert n > 0

    path = ARTIFACT_DIR / "routing" / "anchor_distance.bin"
    assert path.stat().st_size == 4 * n * n

    values = array.array("I")
    with open(path, "rb") as f:
        values.fromfile(f, n * n)
    matrix = [[values[i * n + j] for j in range(n)] for i in range(n)]
    for i in range(n):
        assert matrix[i][i] == 0
        for j in range(n):
            assert matrix[i][j] == matrix[j][i]


def test_catalog_row_counts_satisfy_the_fixture_scenario_floors():
    floors = _scenario_floors()
    rows = _catalog_rows()
    assert {row["status"] for row in rows} <= {"usable", "unusable"}

    usable = [row for row in rows if row["status"] == "usable"]
    assert len(usable) >= floors["geography.min_address_catalog_size"]

    by_zone: dict[str, int] = {}
    by_zone_segment: dict[tuple[str, str], int] = {}
    outlying = 0
    for row in usable:
        zone = row["zone_id"]
        if zone == "":
            outlying += 1
            continue
        by_zone[zone] = by_zone.get(zone, 0) + 1
        for segment in row["segment_eligibility"].split(","):
            if segment:
                key = (zone, segment)
                by_zone_segment[key] = by_zone_segment.get(key, 0) + 1

    assert outlying >= floors["geography.min_outlying_addresses"]
    for count in by_zone.values():
        assert count >= floors["geography.min_addresses_per_zone"]
    for count in by_zone_segment.values():
        assert count >= floors["geography.min_addresses_per_zone_segment"]


def test_every_usable_address_is_in_the_tempe_zone():
    usable = [row for row in _catalog_rows() if row["status"] == "usable"]
    assert usable, "the committed Tempe fixture must contain at least one usable address"
    assert {row["zone_id"] for row in usable} == {TEMPE_ZONE_ID}
    assert {row["effective_zone_id"] for row in usable} == {TEMPE_ZONE_ID}
