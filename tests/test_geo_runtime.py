from __future__ import annotations

import csv
import json
import shutil

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    geo_artifact_sha256,
    geo_run_config,
    geo_scenario_json,
    register_conn,
)
from _no_network import block_network
from csr_bench import geo_reader
from csr_bench.config import load_run_config
from csr_bench.db import connect
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.geo_artifact import canonical_artifact_sha256
from csr_bench.run_init import initialize_run, open_run_db

GEO_ARTIFACT_SHA256 = geo_artifact_sha256()


def _tampered_copy(tmp_path):
    dst = tmp_path / "tampered-artifact"
    shutil.copytree(GEO_ARTIFACT_DIR, dst)
    target = dst / "addresses" / "catalog.csv"
    data = bytearray(target.read_bytes())
    data[100] ^= 0xFF
    target.write_bytes(bytes(data))
    return dst


def _catalog_dataset_version_tampered_copy(tmp_path):
    dst = tmp_path / "dataset-version-tampered"
    shutil.copytree(GEO_ARTIFACT_DIR, dst)
    catalog_path = dst / "addresses" / "catalog.csv"
    with open(catalog_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    dv_index = header.index("dataset_version")
    rows[0][dv_index] = "not-the-real-dataset-version"
    with open(catalog_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    new_sha256 = canonical_artifact_sha256(dst)
    manifest_path = dst / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifact_sha256"] = new_sha256
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")))
    return dst, new_sha256


def _manifest_tampered_copy(tmp_path, mutate):
    dst = tmp_path / "manifest-tampered"
    shutil.copytree(GEO_ARTIFACT_DIR, dst)
    manifest_path = dst / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")))
    return dst


def test_unmodified_artifact_starts_the_run(tmp_path):
    run_id = initialize_run(
        tmp_path / "run.db",
        master_seed=1,
        config=geo_run_config(),
        artifact_dir=GEO_ARTIFACT_DIR,
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run WHERE run_id = ?", (run_id,)).fetchone()[
        "status"
    ] == "active"


def test_a_tampered_artifact_byte_fails_initialization_and_marks_run_failed(tmp_path):
    tampered = _tampered_copy(tmp_path)
    path = tmp_path / "run.db"
    with pytest.raises(EngineError) as ei:
        initialize_run(
            path, master_seed=1, config=geo_run_config(), artifact_dir=tampered
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH
    conn = connect(path)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"


def test_missing_artifact_dir_fails_initialization(tmp_path):
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=None
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_empty_artifact_sha256_fails_initialization_even_with_a_real_dir(tmp_path):
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db",
            master_seed=1,
            config=load_run_config(),
            artifact_dir=GEO_ARTIFACT_DIR,
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_no_network_guard_holds_during_initialize_run(tmp_path):
    with block_network():
        run_id = initialize_run(
            tmp_path / "run.db",
            master_seed=1,
            config=geo_run_config(),
            artifact_dir=GEO_ARTIFACT_DIR,
        )
    assert run_id


def test_no_network_guard_holds_during_artifact_load():
    with block_network():
        artifact = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
    assert artifact.catalog


def test_open_run_db_reverifies_the_artifact_hash_on_subsequent_open(tmp_path):
    path = tmp_path / "run.db"
    initialize_run(
        path, master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    ok = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(ok)
    ok.close()

    tampered = _tampered_copy(tmp_path)
    with pytest.raises(EngineError) as ei:
        open_run_db(path, artifact_dir=tampered)
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_dataset_version_override_must_match_the_manifest_artifact_id(tmp_path):
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db",
            master_seed=1,
            config=geo_run_config(),
            artifact_dir=GEO_ARTIFACT_DIR,
            dataset_version="not-the-real-artifact-id",
        )
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED


def test_configured_artifact_id_must_match_the_manifest_artifact_id(tmp_path):
    bad_config = load_run_config(
        scenario_json=geo_scenario_json(artifact_id="some-other-artifact")
    )
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=bad_config, artifact_dir=GEO_ARTIFACT_DIR
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def _catalog_rows_from_csv():
    with open(GEO_ARTIFACT_DIR / "addresses" / "catalog.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _anchor_rows_from_csv():
    with open(GEO_ARTIFACT_DIR / "routing" / "anchors.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_dataset_versions_populated_from_the_artifact_manifest(tmp_path):
    manifest = json.loads((GEO_ARTIFACT_DIR / "manifest.json").read_text())
    initialize_run(
        tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    rows = conn.execute(
        "SELECT kind, version, snapshot_date, checksum, attribution FROM _dataset_versions"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "osm_geography"
    assert row["version"] == GEO_ARTIFACT_ID == manifest["artifact_id"]
    assert row["checksum"] == manifest["source"]["sha256"]
    assert row["attribution"] == manifest["license"]["attribution"]
    assert row["snapshot_date"] == manifest["source"]["published_at"][:10]


def test_zone_geometry_populated_for_all_ten_zones(tmp_path):
    initialize_run(
        tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    rows = conn.execute(
        "SELECT zone_id, centroid_lat, centroid_lon, boundary_ref FROM _zone_geometry "
        "ORDER BY zone_id"
    ).fetchall()
    assert [r["zone_id"] for r in rows] == list(range(1, 11))
    for r in rows:
        assert 32.0 < r["centroid_lat"] < 34.5
        assert -113.0 < r["centroid_lon"] < -111.0
        assert r["boundary_ref"]
    tempe = rows[3]
    assert 33.3 < tempe["centroid_lat"] < 33.45
    assert -111.95 < tempe["centroid_lon"] < -111.9


def test_address_catalog_populated_matches_the_artifact_catalog(tmp_path):
    csv_rows = _catalog_rows_from_csv()
    initialize_run(
        tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    count = conn.execute("SELECT COUNT(*) c FROM _address_catalog").fetchone()["c"]
    assert count == len(csv_rows) == 723
    row = conn.execute(
        "SELECT * FROM _address_catalog WHERE canonical_key = ?",
        (csv_rows[0]["canonical_key"],),
    ).fetchone()
    assert row is not None
    assert row["dataset_version"] == csv_rows[0]["dataset_version"] == GEO_ARTIFACT_ID
    assert row["status"] == "usable"
    assert row["anchor_id"] == csv_rows[0]["anchor_id"]


def test_routing_anchors_populated_matches_the_artifact_anchors(tmp_path):
    csv_rows = _anchor_rows_from_csv()
    initialize_run(
        tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    count = conn.execute("SELECT COUNT(*) c FROM _routing_anchors").fetchone()["c"]
    assert count == len(csv_rows) == 457
    first = conn.execute(
        "SELECT latitude, longitude, selection_cell FROM _routing_anchors WHERE anchor_id = 1"
    ).fetchone()
    assert first["latitude"] == float(csv_rows[0]["latitude"])
    assert first["longitude"] == float(csv_rows[0]["longitude"])
    assert first["selection_cell"] == csv_rows[0]["selection_cell"]


def test_zones_cities_and_postal_codes_reconciled_only_where_the_artifact_has_evidence(tmp_path):
    initialize_run(
        tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    tempe = conn.execute(
        "SELECT cities_json, postal_codes_json FROM _zones WHERE code = 'tempe'"
    ).fetchone()
    assert json.loads(tempe["cities_json"]) == ["Tempe"]
    assert json.loads(tempe["postal_codes_json"]) == ["85281", "85282", "85283"]

    central_phoenix = conn.execute(
        "SELECT cities_json, postal_codes_json FROM _zones WHERE code = 'central_phoenix'"
    ).fetchone()
    assert json.loads(central_phoenix["cities_json"]) == ["Central Phoenix", "Phoenix"]
    assert json.loads(central_phoenix["postal_codes_json"]) == [
        "85001", "85003", "85004", "85006", "85007", "85013", "85016",
    ]


def test_geo_reader_load_artifact_round_trip():
    artifact = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
    assert len(artifact.catalog) == 723
    assert len(artifact.anchors) == 457
    assert len(artifact.anchor_matrix) == 457 * 457
    assert len(artifact.zone_centroids) == 10
    assert artifact.manifest["artifact_id"] == GEO_ARTIFACT_ID


def test_geo_reader_verify_artifact_sha256_matches_the_manifest():
    manifest = json.loads((GEO_ARTIFACT_DIR / "manifest.json").read_text())
    assert (
        geo_reader.verify_artifact_sha256(GEO_ARTIFACT_DIR, manifest["artifact_sha256"])
        == GEO_ARTIFACT_SHA256
        == manifest["artifact_sha256"]
    )


def test_open_run_db_without_artifact_dir_fails_loudly_once_geography_is_pinned(tmp_path):
    path = tmp_path / "run.db"
    initialize_run(
        path, master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    with pytest.raises(EngineError) as ei:
        open_run_db(path)
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_catalog_row_dataset_version_mismatch_fails_initialization(tmp_path):
    tampered, sha256 = _catalog_dataset_version_tampered_copy(tmp_path)
    cfg = load_run_config(scenario_json=geo_scenario_json(artifact_sha256=sha256))
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=cfg, artifact_dir=tampered
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_manifest_artifact_sha256_field_mismatch_fails_initialization(tmp_path):
    tampered = _manifest_tampered_copy(
        tmp_path, lambda m: m.__setitem__("artifact_sha256", "0" * 64)
    )
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=tampered
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_manifest_catalog_row_count_mismatch_fails_initialization(tmp_path):
    def _mutate(m):
        m["counts"]["catalog_rows"] = m["counts"]["catalog_rows"] + 1

    tampered = _manifest_tampered_copy(tmp_path, _mutate)
    with pytest.raises(EngineError) as ei:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=geo_run_config(), artifact_dir=tampered
        )
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH
