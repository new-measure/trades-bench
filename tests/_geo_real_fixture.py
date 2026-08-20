from __future__ import annotations

import json
from pathlib import Path

from geo_build.manifest import SourceSnapshot
from geo_build.pipeline import BuildParameters, run_pipeline

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "geo"
REPO_ROOT = Path(__file__).parent.parent

TEMPE_PBF = str(FIXTURE_DIR / "tempe_crop.osm.pbf")
TEMPE_ZONES_GEOJSON = str(FIXTURE_DIR / "zone_definitions.geojson")
TEMPE_ARTIFACT_DIR = FIXTURE_DIR / "geography-tempe-fixture"
TEMPE_DATASET_VERSION = "tempe-fixture-v1"
TEMPE_ARTIFACT_ID = "tempe-fixture-v1"
TEMPE_ZONE_ID = 4

TEMPE_SCENARIO_PATH = FIXTURE_DIR / "fixture_scenario.json"
TEMPE_SCENARIO_KEYS: dict = json.loads(TEMPE_SCENARIO_PATH.read_text(encoding="utf-8"))["keys"]

_SOURCE_METADATA = json.loads(
    (REPO_ROOT / "data" / "snapshots" / "arizona.source.json").read_text(encoding="utf-8")
)

TEMPE_SOURCE = SourceSnapshot(
    url=_SOURCE_METADATA["resolved_url"],
    published_at=_SOURCE_METADATA["published_at"],
    bytes=_SOURCE_METADATA["bytes"],
    sha256=_SOURCE_METADATA["sha256"],
)

TEMPE_PARAMETERS = BuildParameters(
    anchor_grid_km=0.03,
    accuracy_sample_size=150,
    accuracy_seed="csr-003d/tempe-fixture/accuracy-gate/v1",
)


def tempe_scenario_json(**overrides) -> str:
    keys = dict(TEMPE_SCENARIO_KEYS)
    keys.update(overrides)
    return json.dumps({"keys": keys})


def run_tempe_fixture_pipeline(*, backend, overrides=None, parameters=None, **kwargs):
    return run_pipeline(
        TEMPE_PBF,
        dataset_version=TEMPE_DATASET_VERSION,
        backend=backend,
        scenario_json=tempe_scenario_json(**(overrides or {})),
        parameters=parameters or TEMPE_PARAMETERS,
        zones_from_file=TEMPE_ZONES_GEOJSON,
        **kwargs,
    )


__all__ = [
    "FIXTURE_DIR",
    "TEMPE_ARTIFACT_DIR",
    "TEMPE_ARTIFACT_ID",
    "TEMPE_DATASET_VERSION",
    "TEMPE_PARAMETERS",
    "TEMPE_PBF",
    "TEMPE_SCENARIO_KEYS",
    "TEMPE_SOURCE",
    "TEMPE_ZONE_ID",
    "TEMPE_ZONES_GEOJSON",
    "run_tempe_fixture_pipeline",
    "tempe_scenario_json",
]
