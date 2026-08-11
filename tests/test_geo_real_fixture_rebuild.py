from __future__ import annotations

import json

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from _geo_real_fixture import (  # noqa: E402
    TEMPE_ARTIFACT_DIR,
    TEMPE_ARTIFACT_ID,
    TEMPE_PBF,
    TEMPE_SOURCE,
    run_tempe_fixture_pipeline,
)
from csr_bench.geo_artifact import canonical_artifact_sha256  # noqa: E402
from geo_build.osrm import OsrmService, docker_available  # noqa: E402
from geo_build.pipeline import write_artifact  # noqa: E402

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon is not reachable"),
]


def test_rebuilding_the_committed_mini_pbf_reproduces_the_committed_hash(tmp_path):
    committed_manifest = json.loads(
        (TEMPE_ARTIFACT_DIR / "manifest.json").read_text(encoding="utf-8")
    )

    with OsrmService(TEMPE_PBF) as backend:
        result = run_tempe_fixture_pipeline(backend=backend)
        out = tmp_path / "rebuilt"
        manifest = write_artifact(
            result, out, artifact_id=TEMPE_ARTIFACT_ID, source=TEMPE_SOURCE
        )

    rebuilt_hash = canonical_artifact_sha256(out)
    assert rebuilt_hash == manifest["artifact_sha256"]
    assert rebuilt_hash == committed_manifest["artifact_sha256"]
