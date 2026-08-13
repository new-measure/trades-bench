from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from _geo_fixture import FIXTURE, scenario_json  # noqa: E402
from csr_bench.geo_artifact import canonical_artifact_sha256  # noqa: E402
from geo_build.manifest import missing_required_fields  # noqa: E402
from geo_build.matrix import build_anchor_matrix, symmetrize  # noqa: E402
from geo_build.osrm import (  # noqa: E402
    OSRM_IMAGE,
    OSRM_IMAGE_DIGEST,
    OSRM_VERSION,
    OsrmService,
    docker_available,
    write_pbf,
)
from geo_build.pipeline import BuildParameters, run_pipeline, write_artifact  # noqa: E402

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon is not reachable"),
]

OSRM_PARAMETERS = BuildParameters(anchor_grid_km=0.5, accuracy_sample_size=120)

OSRM_SCENARIO = scenario_json()


@pytest.fixture(scope="module")
def osrm_backend():
    with OsrmService(FIXTURE) as backend:
        yield backend


def test_the_pinned_image_is_referenced_by_digest():
    assert OSRM_IMAGE_DIGEST.startswith("sha256:")
    assert OSRM_IMAGE == f"ghcr.io/project-osrm/osrm-backend@{OSRM_IMAGE_DIGEST}"
    assert OSRM_VERSION.startswith("v")


def test_write_pbf_converts_the_xml_fixture(tmp_path):
    out = write_pbf(FIXTURE, tmp_path / "data.osm.pbf")
    assert out.exists()
    assert out.stat().st_size > 0


def test_backend_info_is_packageable_and_carries_the_digest(osrm_backend):
    info = osrm_backend.info
    assert info.name == "osrm"
    assert info.packageable is True
    assert info.image_digest == OSRM_IMAGE_DIGEST


def test_snapping_lands_on_the_road_lattice(osrm_backend):
    snapped = osrm_backend.snap(-111.79, 33.55)
    assert snapped is not None
    assert snapped.distance_meters < 5.0
    assert snapped.lat == pytest.approx(33.55, abs=1e-4)


def test_snapping_rejects_a_point_beyond_the_configured_tolerance(osrm_backend):
    snapped = osrm_backend.snap(-111.785, 33.545)
    assert snapped is not None
    assert snapped.distance_meters > 150.0


def test_table_returns_genuinely_asymmetric_distances(osrm_backend):
    a, b = (-111.79, 33.55), (-111.77, 33.57)
    table = osrm_backend.table([a, b], [a, b])
    assert table[0][0] == 0.0 and table[1][1] == 0.0
    assert table[0][1] is not None and table[1][0] is not None
    assert table[0][1] != table[1][0]

    mean = (table[0][1] + table[1][0]) / 2.0
    assert symmetrize(table) == [[0.0, mean], [mean, 0.0]]


def test_route_agrees_with_the_table(osrm_backend):
    a, b = (-111.79, 33.55), (-111.77, 33.57)
    assert osrm_backend.route(a, b) == pytest.approx(
        osrm_backend.table([a], [b])[0][0], rel=1e-6
    )


def test_anchor_matrix_over_real_anchors_is_square_and_finite(osrm_backend):
    from geo_build.anchors import AnchorGrid, select_anchors

    grid = AnchorGrid.over((-111.87, 33.54, -111.75, 33.63), 2.0)
    anchors = select_anchors(
        grid, [(-111.79, 33.55), (-111.77, 33.57), (-111.76, 33.60)], osrm_backend
    )
    assert anchors
    raw = build_anchor_matrix(anchors, osrm_backend)
    assert all(len(row) == len(anchors) for row in raw)
    assert all(value is not None for row in raw for value in row)


def test_end_to_end_fixture_build_against_real_osrm(osrm_backend, tmp_path):
    result = run_pipeline(
        FIXTURE,
        dataset_version="osrm-fixture",
        backend=osrm_backend,
        scenario_json=OSRM_SCENARIO,
        parameters=OSRM_PARAMETERS,
    )
    out = tmp_path / "geography-osrm"
    manifest = write_artifact(result, out, artifact_id="osrm-fixture")

    statuses = {row["status"] for row in result.catalog_rows}
    assert statuses <= {"usable", "unusable"}
    assert result.funnel.accepted >= 1
    assert result.funnel.is_balanced()

    assert missing_required_fields(manifest) == []
    assert manifest["artifact_sha256"] == canonical_artifact_sha256(out)

    anchors_csv = (out / "routing" / "anchors.csv").read_text(encoding="utf-8")
    n = len(anchors_csv.strip().splitlines()) - 1
    assert (out / "routing" / "anchor_distance.bin").stat().st_size == 4 * n * n

    assert manifest["counts"]["catalog_rows"] == len(result.catalog_rows)
    assert not (out / "addresses" / "rejected.csv").exists()
    assert not (out / "reports").exists()
    assert not (tmp_path / "geography-osrm-reports").exists()


def test_a_second_real_build_reproduces_the_same_canonical_hash(osrm_backend, tmp_path):
    hashes = []
    for name in ("one", "two"):
        result = run_pipeline(
            FIXTURE,
            dataset_version="osrm-fixture",
            backend=osrm_backend,
            scenario_json=OSRM_SCENARIO,
            parameters=OSRM_PARAMETERS,
        )
        out = tmp_path / name
        write_artifact(result, out, artifact_id="osrm-fixture")
        hashes.append(canonical_artifact_sha256(out))
    assert hashes[0] == hashes[1]


def test_the_service_tears_its_container_down(tmp_path):
    import subprocess

    with OsrmService(FIXTURE, work_dir=tmp_path / "work") as backend:
        assert backend.snap(-111.79, 33.55) is not None
        container_id = _running_container_id(backend)
    listed = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"id={container_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.stdout.strip() == ""


def _running_container_id(backend) -> str:
    import subprocess

    port = backend.base_url.rsplit(":", 1)[1]
    proc = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"publish={port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = proc.stdout.split()
    assert ids, "no running OSRM container found for the backend's port"
    return ids[0]


def test_fixture_road_grid_carries_no_address_tag():
    text = Path(FIXTURE).read_text(encoding="utf-8")
    grid_block = text.split("<!-- drivable road grid")[1]
    assert 'k="addr:' not in grid_block
    assert 'k="highway"' in grid_block
