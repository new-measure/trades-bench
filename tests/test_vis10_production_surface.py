from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import trades_bench
from _adapters import RecordingHarnessAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
)
from policies import oracle
from test_http_engine import _build_engine, _request
from trades_bench.config import load_run_config
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.harness.runner import TRACK, initialize_controlled_run
from trades_bench.http_engine import ORACLE_BYPASS_ENV_NAMES, SUNRIDGE_ORACLE_MODE_ENV

SEED = 5
ONE_DAY = ("2026-10-01", "2026-10-01")
ONE_DAY_HORIZON_DAYS = 1

ENGINE_SOURCE_DIR = Path(trades_bench.__file__).parent

POLICY_PACKAGE_NAME = "policies"

ORACLE_MENTION_PATTERN = re.compile("oracle", re.IGNORECASE)
ORACLE_REFUSAL_TOKENS = (
    "ORACLE_BYPASS_ENV_NAMES",
    "SUNRIDGE_ORACLE_MODE",
    "_refuse_oracle_bypass_environment",
)

ENVIRONMENT_READ_PATTERN = re.compile(r"os\.environ|os\.getenv")
ENVIRONMENT_READ_SITES: dict[str, int] = {
    "__main__.py": 1,
    "harness/launcher.py": 2,
    "harness/pi_adapter.py": 1,
    "harness/runner.py": 1,
}

INSPECTION_PATH_PAIRS = (
    ("/v1/ops/inspect.dump", "/v1/ops/absent.operation", "agent"),
    ("/v1/control/inspect", "/v1/control/absent_operation", "control"),
    ("/v1/inspect", "/v1/absent-route", "agent"),
)


def engine_source_paths() -> list[Path]:
    return sorted(ENGINE_SOURCE_DIR.rglob("*.py"))


def relative_name(path: Path) -> str:
    return path.relative_to(ENGINE_SOURCE_DIR).as_posix()


def imported_module_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.fixture(scope="module")
def one_day_config():
    return load_run_config(
        scenario_json=json.dumps(
            {
                "keys": {
                    "geography.artifact_id": GEO_ARTIFACT_ID,
                    "geography.artifact_sha256": geo_artifact_sha256(),
                    "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
                    "run.start_date": ONE_DAY[0],
                    "run.end_date": ONE_DAY[1],
                }
            }
        )
    )


def test_a_scored_run_refuses_to_start_while_an_oracle_bypass_flag_is_set(
    tmp_path, monkeypatch, one_day_config
):
    monkeypatch.setenv(SUNRIDGE_ORACLE_MODE_ENV, "1")

    with pytest.raises(EngineError) as raised:
        initialize_controlled_run(
            tmp_path / "refused.db",
            master_seed=SEED,
            config=one_day_config,
            adapter=RecordingHarnessAdapter(),
            artifact_dir=GEO_ARTIFACT_DIR,
        )

    assert raised.value.code is ErrorCode.INVARIANT_VIOLATED
    assert SUNRIDGE_ORACLE_MODE_ENV in raised.value.message
    assert not (tmp_path / "refused.db").exists()


def test_a_scored_run_starts_when_no_oracle_bypass_flag_is_present(
    tmp_path, monkeypatch, one_day_config
):
    for name in ORACLE_BYPASS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    run_id = initialize_controlled_run(
        tmp_path / "scored.db",
        master_seed=SEED,
        config=one_day_config,
        adapter=RecordingHarnessAdapter(),
        artifact_dir=GEO_ARTIFACT_DIR,
    )

    assert run_id
    assert (tmp_path / "scored.db").is_file()


def test_an_empty_oracle_bypass_flag_does_not_refuse_a_scored_run(
    tmp_path, monkeypatch, one_day_config
):
    monkeypatch.setenv(SUNRIDGE_ORACLE_MODE_ENV, "")

    run_id = initialize_controlled_run(
        tmp_path / "empty-flag.db",
        master_seed=SEED,
        config=one_day_config,
        adapter=RecordingHarnessAdapter(),
        artifact_dir=GEO_ARTIFACT_DIR,
    )

    assert run_id


def test_the_oracle_policy_runs_while_the_bypass_flag_is_set(
    tmp_path, monkeypatch, one_day_config
):
    monkeypatch.setenv(SUNRIDGE_ORACLE_MODE_ENV, "1")

    result = oracle.run_oracle(
        config=one_day_config,
        master_seed=SEED,
        run_dir=tmp_path / "oracle",
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=ONE_DAY_HORIZON_DAYS,
    )

    assert result.run_status == "completed"
    assert result.report["policy"] == oracle.POLICY_NAME
    assert oracle.TRACK != TRACK


@pytest.mark.parametrize(("inspection_path", "absent_path", "principal"), INSPECTION_PATH_PAIRS)
def test_no_inspection_route_answers_differently_from_an_absent_route(
    tmp_path, inspection_path, absent_path, principal
):
    engine, conn = _build_engine(tmp_path / f"engine-{principal}")
    engine.start()
    try:
        token = getattr(engine.tokens, principal)
        inspection = _request(engine.port, "POST", inspection_path, token=token, body={})
        absent = _request(engine.port, "POST", absent_path, token=token, body={})
    finally:
        engine.stop()
        conn.close()

    assert inspection[0] == absent[0]
    assert inspection[1] == absent[1]


def test_no_engine_module_imports_the_policy_package():
    offenders = [
        relative_name(path)
        for path in engine_source_paths()
        if POLICY_PACKAGE_NAME
        in imported_module_roots(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    ]

    assert offenders == []


def test_every_oracle_mention_in_the_engine_belongs_to_the_refusal_surface():
    offenders: list[tuple[str, int, str]] = []
    mentioning_files: set[str] = set()
    for path in engine_source_paths():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not ORACLE_MENTION_PATTERN.search(line):
                continue
            mentioning_files.add(relative_name(path))
            if not any(token in line for token in ORACLE_REFUSAL_TOKENS):
                offenders.append((relative_name(path), number, line.strip()))

    assert offenders == []
    assert mentioning_files == {"http_engine.py", "harness/runner.py"}


def test_the_engine_reads_the_environment_only_at_the_pinned_sites():
    counts: dict[str, int] = {}
    for path in engine_source_paths():
        hits = len(ENVIRONMENT_READ_PATTERN.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[relative_name(path)] = hits

    assert counts == ENVIRONMENT_READ_SITES
