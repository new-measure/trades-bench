from __future__ import annotations

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from _geo_real_fixture import (  # noqa: E402
    TEMPE_ARTIFACT_ID,
    TEMPE_PBF,
    TEMPE_SOURCE,
    run_tempe_fixture_pipeline,
)
from csr_bench.geo_artifact import canonical_artifact_sha256  # noqa: E402
from geo_build.manifest import missing_required_fields  # noqa: E402
from geo_build.osrm import OsrmService, docker_available  # noqa: E402
from geo_build.pipeline import write_artifact  # noqa: E402

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon is not reachable"),
]


def test_rebuilding_the_committed_mini_pbf_produces_a_self_consistent_artifact(tmp_path):
    with OsrmService(TEMPE_PBF) as backend:
        result = run_tempe_fixture_pipeline(backend=backend)
        out = tmp_path / "rebuilt"
        manifest = write_artifact(
            result, out, artifact_id=TEMPE_ARTIFACT_ID, source=TEMPE_SOURCE
        )

    assert missing_required_fields(manifest) == []
    assert canonical_artifact_sha256(out) == manifest["artifact_sha256"]
