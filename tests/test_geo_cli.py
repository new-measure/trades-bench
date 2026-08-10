"""tools.geo_build.cli: the `python -m geo_build build ...` skeleton
(research/osm-data.md §7's "single scripted, non-interactive pipeline";
CSR-003b, RES-60).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from geo_build.cli import main  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "pipeline_fixture.osm")


def test_cli_build_writes_the_artifact_tree(tmp_path):
    out_dir = tmp_path / "geography-dev"
    exit_code = main(["build", FIXTURE, str(out_dir), "--dataset-version", "dev-test"])
    assert exit_code == 0
    assert (out_dir / "addresses" / "catalog.csv").exists()
    assert (out_dir / "reports" / "funnel_counts.json").exists()

    catalog_text = (out_dir / "addresses" / "catalog.csv").read_text()
    assert "dev-test" in catalog_text


def test_cli_requires_dataset_version(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["build", FIXTURE, str(tmp_path / "out")])


def test_cli_scenario_override_flows_through(tmp_path):
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(
        '{"keys": {"geography.outlying_buffer_km": 0.5}}', encoding="utf-8"
    )
    out_dir = tmp_path / "geography-dev"
    exit_code = main(
        [
            "build",
            FIXTURE,
            str(out_dir),
            "--dataset-version",
            "dev-test",
            "--scenario",
            str(scenario_path),
        ]
    )
    assert exit_code == 0
    catalog_text = (out_dir / "addresses" / "catalog.csv").read_text()
    assert "Second Ave" not in catalog_text  # the outlying node's street


def test_module_entry_point_runs_as_a_subprocess(tmp_path):
    out_dir = tmp_path / "geography-dev"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "geo_build",
            "build",
            FIXTURE,
            str(out_dir),
            "--dataset-version",
            "dev-test",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**__import__("os").environ, "PYTHONPATH": "tools"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "addresses" / "catalog.csv").exists()
    assert "accepted" in proc.stdout
