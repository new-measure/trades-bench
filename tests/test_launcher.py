from __future__ import annotations

import json
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config
from trades_bench.adapter import AgentTurn, RunMetadata
from trades_bench.harness import launcher
from trades_bench.harness.client import (
    ControlClient,
    call_attempt_request_id,
    call_final_request_id,
    complete_invocation_request_id,
    fail_run_request_id,
    next_invocation_request_id,
)
from trades_bench.harness.launcher import (
    AGENT_TOKEN_ENV,
    CONTROL_TOKEN_ENV,
    EngineLaunchError,
    EngineLaunchSpec,
    EngineTokens,
    agent_env,
    engine_env,
    launch_engine,
)
from trades_bench.harness.runner import initialize_controlled_run
from trades_bench.http_engine import (
    SUNRIDGE_API_PORT_ENV,
    SUNRIDGE_API_TOKEN_ENV,
    SUNRIDGE_WORKSPACE_ENV,
)

MASTER_SEED = 1

BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/agent",
    "LANG": "en_US.UTF-8",
    "TERM": "xterm",
    "AWS_SECRET_ACCESS_KEY": "leaked",
    "TRADES_BENCH_CONTROL_TOKEN": "leaked-control",
    "DATABASE_PATH": "/private/run.db",
}

_READY_STUB = """
import json, os, signal, sys, time

ready = sys.argv[sys.argv.index("--ready-file") + 1]
marker = os.path.join(os.path.dirname(ready), "terminated.txt")


def _stop(signum, frame):
    with open(marker, "w") as handle:
        handle.write(str(signum))
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _stop)
staging = ready + ".partial"
with open(staging, "w") as handle:
    handle.write(json.dumps({"port": 45001}))
os.replace(staging, ready)
while True:
    time.sleep(0.05)
"""

_STUBBORN_STUB = """
import json, os, signal, sys, time

ready = sys.argv[sys.argv.index("--ready-file") + 1]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
staging = ready + ".partial"
with open(staging, "w") as handle:
    handle.write(json.dumps({"port": 45002}))
os.replace(staging, ready)
while True:
    time.sleep(0.05)
"""

_SILENT_STUB = """
import time

while True:
    time.sleep(0.05)
"""

_CRASHING_STUB = """
import sys

sys.stderr.write("engine could not open the run file\\n")
raise SystemExit(3)
"""


def _stub(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _spec(tmp_path: Path, stub: Path, **kwargs) -> EngineLaunchSpec:
    return EngineLaunchSpec(
        db_path=tmp_path / "run.db",
        run_dir=tmp_path / "private",
        entry=(str(stub),),
        **kwargs,
    )


class _MinimalAdapter:

    name = "controlled"
    version = "0.0.1-test"
    context_policy = "controlled_daily_reset"
    attempt_definition = "one completed model turn with no terminal action committed"

    def start_run(self, workspace: Path, system_prompt: str, env: Mapping[str, str]) -> None:
        return None

    def start_day(self, service_date: date) -> None:
        return None

    def send(self, prompt: str, kind: str, deadline_s: float) -> AgentTurn:
        return AgentTurn(text="", ended_normally=True, input_tokens=None, output_tokens=None)

    def end_day(self, service_date: date) -> None:
        return None

    def end_run(self, status: str) -> None:
        return None

    def describe(self) -> RunMetadata:
        return RunMetadata(
            name=self.name,
            version=self.version,
            context_policy=self.context_policy,
            attempt_definition=self.attempt_definition,
            model_id="test-model",
        )


@dataclass(frozen=True)
class _LiveEngine:
    process: object
    control: ControlClient
    agent: ControlClient


def _start_live_engine(tmp_path: Path) -> _LiveEngine:
    db_path = tmp_path / "run.db"
    initialize_controlled_run(
        db_path,
        master_seed=MASTER_SEED,
        config=geo_run_config(),
        adapter=_MinimalAdapter(),
        artifact_dir=GEO_ARTIFACT_DIR,
    )
    spec = EngineLaunchSpec(
        db_path=db_path,
        run_dir=tmp_path / "private",
        geo_dir=GEO_ARTIFACT_DIR,
        ready_timeout_seconds=60.0,
    )
    process = launch_engine(spec)
    return _LiveEngine(
        process=process,
        control=ControlClient(port=process.port, token=process.tokens.control, timeout=30.0),
        agent=ControlClient(port=process.port, token=process.tokens.agent, timeout=30.0),
    )


def test_request_id_derivations_are_pinned() -> None:
    assert next_invocation_request_id(4) == "next-invocation-4"
    assert complete_invocation_request_id(17) == "complete-invocation-17"
    assert call_attempt_request_id(9, 2) == "call-attempt-9-2"
    assert call_final_request_id(9) == "call-final-9"
    assert fail_run_request_id(1) == "fail-run-1"


def test_minted_tokens_are_distinct_256_bit_hex() -> None:
    tokens = EngineTokens.mint()
    assert tokens.agent != tokens.control
    for value in (tokens.agent, tokens.control):
        assert len(value) == 64
        int(value, 16)


def test_engine_env_is_built_from_scratch_with_both_tokens() -> None:
    tokens = EngineTokens.mint()
    env = engine_env(tokens, base=BASE_ENV)
    assert env[CONTROL_TOKEN_ENV] == tokens.control
    assert env[AGENT_TOKEN_ENV] == tokens.agent
    assert set(env) == {"PATH", "HOME", "LANG", "TERM", CONTROL_TOKEN_ENV, AGENT_TOKEN_ENV}
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DATABASE_PATH" not in env


def test_agent_env_carries_only_the_three_sunridge_vars_and_the_minimum(tmp_path: Path) -> None:
    tokens = EngineTokens.mint()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = agent_env(port=41111, token=tokens.agent, workspace=workspace, base=BASE_ENV)
    assert set(env) == {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        SUNRIDGE_API_PORT_ENV,
        SUNRIDGE_API_TOKEN_ENV,
        SUNRIDGE_WORKSPACE_ENV,
    }
    assert env[SUNRIDGE_API_PORT_ENV] == "41111"
    assert env[SUNRIDGE_API_TOKEN_ENV] == tokens.agent
    assert env[SUNRIDGE_WORKSPACE_ENV] == str(workspace.resolve())
    assert tokens.control not in env.values()
    joined = " ".join(f"{key}={value}" for key, value in env.items()).lower()
    assert "trades_bench" not in joined
    assert "bench" not in " ".join(env).lower()
    assert "run.db" not in joined


def test_agent_env_rejects_benchmark_named_and_reserved_extras(tmp_path: Path) -> None:
    for name in ("TRADES_BENCH_CONTROL_TOKEN", "CSR_BENCH_MODE", "SOME_BENCH_FLAG"):
        with pytest.raises(ValueError):
            agent_env(port=1, token="t", workspace=tmp_path, base=BASE_ENV, extra={name: "x"})
    with pytest.raises(ValueError):
        agent_env(
            port=1,
            token="t",
            workspace=tmp_path,
            base=BASE_ENV,
            extra={SUNRIDGE_API_TOKEN_ENV: "override"},
        )
    env = agent_env(
        port=1, token="t", workspace=tmp_path, base=BASE_ENV, extra={"ANTHROPIC_CONFIG": "x"}
    )
    assert env["ANTHROPIC_CONFIG"] == "x"


def test_launch_reads_the_ready_file_and_stops_with_sigterm(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _stub(tmp_path, "ready_stub.py", _READY_STUB))
    engine = launch_engine(spec)
    try:
        assert engine.port == 45001
        assert engine.running
        assert engine.stdout_path.is_file()
        assert engine.stderr_path.is_file()
    finally:
        returncode = engine.stop()
    assert returncode == 0
    marker = (tmp_path / "private" / "terminated.txt").read_text(encoding="utf-8")
    assert marker == str(int(signal.SIGTERM))


def test_launch_resolves_a_relative_run_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = EngineLaunchSpec(
        db_path=tmp_path / "run.db",
        run_dir=Path("private"),
        entry=(str(_stub(tmp_path, "relative_ready_stub.py", _READY_STUB)),),
    )
    engine = launch_engine(spec)
    try:
        assert engine.port == 45001
        assert engine.run_dir == (tmp_path / "private").resolve()
        assert (tmp_path / "private" / launcher.READY_FILENAME).is_file()
    finally:
        engine.stop()


def test_stop_falls_back_to_sigkill(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path, _stub(tmp_path, "stubborn_stub.py", _STUBBORN_STUB), stop_timeout_seconds=0.5
    )
    engine = launch_engine(spec)
    try:
        assert engine.port == 45002
    finally:
        returncode = engine.stop()
    assert returncode == -int(signal.SIGKILL)


def test_ready_wait_is_bounded_and_terminates_the_process(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        _stub(tmp_path, "silent_stub.py", _SILENT_STUB),
        ready_timeout_seconds=0.3,
        stop_timeout_seconds=2.0,
    )
    with pytest.raises(EngineLaunchError) as excinfo:
        launch_engine(spec)
    assert "readiness" in str(excinfo.value)


def test_an_interrupted_ready_wait_stops_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(
        tmp_path, _stub(tmp_path, "silent_stub.py", _SILENT_STUB), stop_timeout_seconds=2.0
    )
    started: list[object] = []
    popen = launcher.subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> object:
        started.append(popen(*args, **kwargs))
        return started[-1]

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        launcher, "time", SimpleNamespace(monotonic=time.monotonic, sleep=interrupt)
    )

    with pytest.raises(KeyboardInterrupt):
        launch_engine(spec)

    assert started[0].poll() == -int(signal.SIGTERM)


def test_launch_reports_an_engine_that_dies_before_readiness(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _stub(tmp_path, "crashing_stub.py", _CRASHING_STUB))
    with pytest.raises(EngineLaunchError) as excinfo:
        launch_engine(spec)
    message = str(excinfo.value)
    assert "exited with code 3" in message
    assert "engine could not open the run file" in message
    assert (tmp_path / "private" / launcher.STDERR_FILENAME).is_file()


def test_engine_subprocess_serves_the_control_plane(tmp_path: Path) -> None:
    live = _start_live_engine(tmp_path)
    try:
        assert live.control.health() is True

        opened = live.control.next_invocation(1)
        assert opened.ok, opened.body
        assert opened.replayed is False
        assert opened.data["kind"] == "opening"
        assert opened.data["prompt_version"]
        assert opened.request_id == "next-invocation-1"
        invocation_id = int(opened.data["invocation_id"])

        replay = live.control.next_invocation(1)
        assert replay.replayed is True
        assert replay.body["data"] == opened.body["data"]

        completed = live.control.complete_invocation(invocation_id)
        assert completed.ok, completed.body

        second = live.control.next_invocation(2)
        assert second.ok, second.body
        assert second.data["invocation_id"] != invocation_id
    finally:
        live.process.stop()


def test_control_routes_are_invisible_to_the_agent_token(tmp_path: Path) -> None:
    live = _start_live_engine(tmp_path)
    try:
        refused = live.agent.next_invocation(1)
        assert refused.status == 404
        assert refused.error_code == "unknown_operation"
        assert refused.ok is False
    finally:
        live.process.stop()


def test_ready_file_and_logs_live_outside_the_workspace(tmp_path: Path) -> None:
    live = _start_live_engine(tmp_path)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        private = tmp_path / "private"
        ready = private / launcher.READY_FILENAME
        assert json.loads(ready.read_text(encoding="utf-8"))["port"] == live.process.port
        assert workspace not in ready.parents
        assert workspace not in (tmp_path / "run.db").parents
    finally:
        live.process.stop()
