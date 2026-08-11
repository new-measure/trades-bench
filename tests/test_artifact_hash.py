from __future__ import annotations

import hashlib

import pytest

from csr_bench.errors import EngineError, ErrorCode
from csr_bench.geo_artifact import (
    MANIFEST_FILENAME,
    artifact_file_paths,
    canonical_artifact_sha256,
    file_sha256,
)


def _artifact(root, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_hash_is_stable_across_calls_and_directories(tmp_path):
    files = {
        "addresses/catalog.csv": b"a,b\n1,2\n",
        "routing/anchor_distance.bin": bytes(range(16)),
        "zones/zone_definitions.geojson": b'{"type":"FeatureCollection"}\n',
    }
    one, two = tmp_path / "one", tmp_path / "two"
    _artifact(one, files)
    _artifact(two, files)
    assert canonical_artifact_sha256(one) == canonical_artifact_sha256(one)
    assert canonical_artifact_sha256(one) == canonical_artifact_sha256(two)


def test_manifest_is_excluded_but_every_other_file_is_hashed(tmp_path):
    root = tmp_path / "artifact"
    _artifact(root, {"addresses/catalog.csv": b"x\n", MANIFEST_FILENAME: b"{}\n"})
    baseline = canonical_artifact_sha256(root)

    (root / MANIFEST_FILENAME).write_bytes(b'{"artifact_sha256": "whatever"}\n')
    assert canonical_artifact_sha256(root) == baseline

    (root / "addresses" / "catalog.csv").write_bytes(b"y\n")
    assert canonical_artifact_sha256(root) != baseline


def test_only_the_top_level_manifest_is_excluded(tmp_path):
    root = tmp_path / "artifact"
    _artifact(root, {"reports/manifest.json": b"{}\n"})
    baseline = canonical_artifact_sha256(root)
    (root / "reports" / "manifest.json").write_bytes(b'{"changed": true}\n')
    assert canonical_artifact_sha256(root) != baseline
    assert artifact_file_paths(root) == ["reports/manifest.json"]


def test_file_order_is_sorted_relative_path_not_filesystem_order(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    ordered = {"a.txt": b"1", "b/c.txt": b"2", "z.txt": b"3"}
    _artifact(one, ordered)
    _artifact(two, dict(reversed(list(ordered.items()))))
    assert artifact_file_paths(one) == ["a.txt", "b/c.txt", "z.txt"]
    assert canonical_artifact_sha256(one) == canonical_artifact_sha256(two)


def test_path_and_content_cannot_be_confused(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    _artifact(one, {"ab": b"c"})
    _artifact(two, {"a": b"bc"})
    assert canonical_artifact_sha256(one) != canonical_artifact_sha256(two)


def test_empty_directory_hashes_to_the_domain_prefix_alone(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert artifact_file_paths(root) == []
    assert canonical_artifact_sha256(root) != hashlib.sha256().hexdigest()


def test_a_missing_directory_is_a_stable_engine_error(tmp_path):
    with pytest.raises(EngineError) as excinfo:
        canonical_artifact_sha256(tmp_path / "nope")
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_file_sha256_is_a_plain_single_file_digest(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    assert file_sha256(path) == hashlib.sha256(b"hello").hexdigest()
