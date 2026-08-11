from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from geo_build.cli import main  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "pipeline_fixture.osm")
FIXTURE_SCENARIO = str(Path(__file__).parent / "fixtures" / "geo" / "fixture_scenario.json")

FIXTURE_FLAGS = [
    "--fake-backend",
    "--anchor-grid-km",
    "0.5",
    "--accuracy-sample-size",
    "200",
    "--scenario",
    FIXTURE_SCENARIO,
]


def test_cli_build_writes_the_artifact_tree(tmp_path):
    out_dir = tmp_path / "geography-dev"
    exit_code = main(
        ["build", FIXTURE, str(out_dir), "--dataset-version", "dev-test", *FIXTURE_FLAGS]
    )
    assert exit_code == 0
    assert (out_dir / "addresses" / "catalog.csv").exists()
    assert (out_dir / "routing" / "anchors.csv").exists()
    assert (out_dir / "routing" / "anchor_distance.bin").exists()
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "reports" / "data_quality.md").exists()

    catalog_text = (out_dir / "addresses" / "catalog.csv").read_text()
    assert "dev-test" in catalog_text


def test_cli_requires_dataset_version(tmp_path):
    with pytest.raises(SystemExit):
        main(["build", FIXTURE, str(tmp_path / "out")])


def test_cli_has_no_skip_routing_escape_hatch():
    from geo_build.cli import build_parser

    help_text = build_parser().format_help()
    assert "--skip-routing" not in help_text
    with pytest.raises(SystemExit):
        main(["build", FIXTURE, "/tmp/unused", "--dataset-version", "x", "--skip-routing"])


def test_cli_fake_backend_artifact_is_marked_non_packageable(tmp_path):
    out_dir = tmp_path / "geography-dev"
    main(["build", FIXTURE, str(out_dir), "--dataset-version", "dev-test", *FIXTURE_FLAGS])
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["packageable"] is False
    assert manifest["routing_backend"]["name"] == "fake-straight-line"
    assert any(t["name"] == "fake-straight-line" for t in manifest["tools"])


def test_cli_source_flags_flow_into_the_manifest(tmp_path):
    out_dir = tmp_path / "geography-dev"
    main(
        [
            "build",
            FIXTURE,
            str(out_dir),
            "--dataset-version",
            "dev-test",
            "--artifact-id",
            "az-2026-08",
            "--source-url",
            "https://download.geofabrik.de/north-america/us/arizona-260801.osm.pbf",
            "--source-published-at",
            "2026-08-01",
            "--source-bytes",
            "312345678",
            "--source-sha256",
            "a" * 64,
            *FIXTURE_FLAGS,
        ]
    )
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["artifact_id"] == "az-2026-08"
    assert manifest["source"]["url"].endswith("arizona-260801.osm.pbf")
    assert manifest["source"]["published_at"] == "2026-08-01"
    assert manifest["source"]["bytes"] == 312345678
    assert manifest["source"]["sha256"] == "a" * 64
    assert manifest["source"]["placeholder"] is False
    assert manifest["packageable"] is False


def test_cli_scenario_override_flows_through(tmp_path):
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(
        json.dumps(
            {
                "keys": {
                    **json.loads(Path(FIXTURE_SCENARIO).read_text())["keys"],
                    "geography.outlying_buffer_km": 0.5,
                    "geography.min_outlying_addresses": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "geography-dev"
    exit_code = main(
        [
            "build",
            FIXTURE,
            str(out_dir),
            "--dataset-version",
            "dev-test",
            "--fake-backend",
            "--anchor-grid-km",
            "0.5",
            "--accuracy-sample-size",
            "200",
            "--scenario",
            str(scenario_path),
        ]
    )
    assert exit_code == 0
    catalog_text = (out_dir / "addresses" / "catalog.csv").read_text()
    assert "Second Ave" not in catalog_text


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
            *FIXTURE_FLAGS,
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**__import__("os").environ, "PYTHONPATH": "tools"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "addresses" / "catalog.csv").exists()
    assert "usable" in proc.stdout
    assert "artifact_sha256=" in proc.stdout
