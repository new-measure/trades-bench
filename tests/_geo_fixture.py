from __future__ import annotations

import json
from pathlib import Path

from geo_build.pipeline import BuildParameters, run_pipeline
from geo_build.routing import FakeBackend

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "geo"
FIXTURE = str(FIXTURE_DIR / "pipeline_fixture.osm")
FIXTURE_SCENARIO_PATH = FIXTURE_DIR / "fixture_scenario.json"
FIXTURE_FLOORS: dict = json.loads(FIXTURE_SCENARIO_PATH.read_text(encoding="utf-8"))["keys"]

FIXTURE_PARAMETERS = BuildParameters(anchor_grid_km=0.5, accuracy_sample_size=200)


def scenario_json(**overrides) -> str:
    keys = dict(FIXTURE_FLOORS)
    keys.update(overrides)
    return json.dumps({"keys": keys})


def run_fixture_pipeline(*, overrides=None, backend=None, parameters=None, **kwargs):
    return run_pipeline(
        FIXTURE,
        dataset_version="dev-test",
        backend=backend if backend is not None else FakeBackend(),
        scenario_json=scenario_json(**(overrides or {})),
        parameters=parameters or FIXTURE_PARAMETERS,
        **kwargs,
    )


__all__ = [
    "FIXTURE",
    "FIXTURE_DIR",
    "FIXTURE_FLOORS",
    "FIXTURE_PARAMETERS",
    "FIXTURE_SCENARIO_PATH",
    "run_fixture_pipeline",
    "scenario_json",
]
