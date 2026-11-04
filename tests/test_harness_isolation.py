from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_har_equivalence import controlled_scripted_run
from trades_bench.config import load_run_config
from trades_bench.harness import workspace
from trades_bench.harness.launcher import (
    AGENT_TOKEN_ENV,
    CONTROL_TOKEN_ENV,
    EngineTokens,
    agent_env,
    engine_env,
)
from trades_bench.harness.scripted import INCOMPLETE_REPLY, ScriptedAdapter

AGENT_VARIABLES = ("SUNRIDGE_API_PORT", "SUNRIDGE_API_TOKEN", "SUNRIDGE_WORKSPACE")
FORBIDDEN_MARKERS = ("trades_bench", "trades-bench", "csr_bench", "bench", "benchmark")


def _tokens() -> EngineTokens:
    return EngineTokens(agent="agent-token-value", control="control-token-value")


def _base_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "TRADES_BENCH_CONTROL_TOKEN": "leaked-control-token",
        "TRADES_BENCH_DB": "/private/run.db",
        "CSR_BENCH_RUN_DIR": "/private/run",
    }


def test_acceptance_29_agent_env_carries_only_the_sunridge_variables(tmp_path):
    tokens = _tokens()
    env = agent_env(port=8123, token=tokens.agent, workspace=tmp_path, base=_base_env())

    assert set(env) == set(AGENT_VARIABLES) | {"PATH", "HOME"}
    assert env["SUNRIDGE_API_PORT"] == "8123"
    assert env["SUNRIDGE_API_TOKEN"] == tokens.agent
    assert env["SUNRIDGE_WORKSPACE"] == str(tmp_path.resolve())


def test_acceptance_29_agent_env_holds_no_control_token_db_path_or_identifier(tmp_path):
    tokens = _tokens()
    env = agent_env(port=1, token=tokens.agent, workspace=tmp_path, base=_base_env())

    joined = " ".join(f"{name}={value}" for name, value in env.items())
    assert tokens.control not in joined
    assert "leaked-control-token" not in joined
    assert "/private/run.db" not in joined
    assert CONTROL_TOKEN_ENV not in env
    assert AGENT_TOKEN_ENV not in env
    for name in env:
        lowered = name.lower()
        assert all(marker not in lowered for marker in FORBIDDEN_MARKERS)


def test_acceptance_29_agent_env_refuses_benchmark_named_extras(tmp_path):
    tokens = _tokens()
    for name in ("TRADES_BENCH_DB", "CSR_BENCH_RUN", "BENCH_MODE", "MY_BENCHMARK"):
        with pytest.raises(ValueError):
            agent_env(
                port=1,
                token=tokens.agent,
                workspace=tmp_path,
                base=_base_env(),
                extra={name: "x"},
            )


def test_acceptance_29_engine_env_keeps_the_control_token_out_of_the_agent_env(tmp_path):
    tokens = _tokens()
    engine = engine_env(tokens, base=_base_env())
    agent = agent_env(port=1, token=tokens.agent, workspace=tmp_path, base=_base_env())

    assert engine[CONTROL_TOKEN_ENV] == tokens.control
    assert engine[AGENT_TOKEN_ENV] == tokens.agent
    assert set(engine) & set(AGENT_VARIABLES) == set()
    assert tokens.control not in agent.values()


def test_acceptance_29_workspace_holds_only_agent_material(tmp_path):
    root = workspace.create(tmp_path / "workspace", load_run_config())
    entries = sorted(path.name for path in root.iterdir())

    assert entries == ["MEMORY.md", "docs"]
    assert workspace.memory_path(root).read_bytes() == b""
    assert list(root.rglob("*.db")) == []
    assert list(root.rglob("*.jsonl")) == []
    assert list(root.rglob("*.log")) == []


def test_acceptance_29_private_artifacts_live_outside_the_workspace(tmp_path):
    root = workspace.create(tmp_path / "workspace", load_run_config())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    private = [run_dir / "run.db", run_dir / "index.jsonl", run_dir / "engine-ready.json"]
    for path in private:
        path.write_text("private", encoding="utf-8")

    for path in private:
        with pytest.raises(ValueError):
            path.resolve().relative_to(root.resolve())
        assert not (root / path.name).exists()


def test_acceptance_29_workspace_contains_no_link_out_of_itself(tmp_path):
    root = workspace.create(tmp_path / "workspace", load_run_config())
    outside = tmp_path / "run"
    outside.mkdir()
    (outside / "run.db").write_text("private", encoding="utf-8")

    for path in root.rglob("*"):
        assert not path.is_symlink()
        resolved = path.resolve()
        assert str(resolved).startswith(str(root.resolve()) + os.sep)


def test_acceptance_29_a_controlled_run_leaves_nothing_private_in_the_workspace(tmp_path):
    adapter = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY)
    paths = controlled_scripted_run(tmp_path, adapter)
    root: Path = paths["workspace"]
    run_dir: Path = paths["run_dir"]

    assert paths["db_path"].exists()
    with pytest.raises(ValueError):
        run_dir.resolve().relative_to(root.resolve())

    for path in root.rglob("*"):
        assert not path.is_symlink()
        if not path.is_file():
            continue
        assert path.suffix not in (".db", ".jsonl", ".sqlite")
        text = path.read_bytes().decode("utf-8", errors="replace").lower()
        assert str(paths["db_path"]).lower() not in text
        assert "trades_bench_control_token" not in text

    env = adapter.record.env
    assert set(env) >= set(AGENT_VARIABLES)
    assert CONTROL_TOKEN_ENV not in env
    assert str(paths["db_path"]) not in " ".join(env.values())
