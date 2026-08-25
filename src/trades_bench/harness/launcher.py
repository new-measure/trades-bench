from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from trades_bench.http_engine import (
    SUNRIDGE_API_PORT_ENV,
    SUNRIDGE_API_TOKEN_ENV,
    SUNRIDGE_WORKSPACE_ENV,
)

CONTROL_TOKEN_ENV = "TRADES_BENCH_CONTROL_TOKEN"
AGENT_TOKEN_ENV = "TRADES_BENCH_AGENT_TOKEN"

ENGINE_ENTRY = ("-m", "trades_bench")
READY_FILENAME = "engine-ready.json"
STDOUT_FILENAME = "engine.stdout.log"
STDERR_FILENAME = "engine.stderr.log"

TOKEN_BYTES = 32
READY_TIMEOUT_SECONDS = 30.0
READY_POLL_SECONDS = 0.02
STOP_TIMEOUT_SECONDS = 10.0

_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "TERM")
_FORBIDDEN_AGENT_PREFIXES = ("TRADES_BENCH", "CSR_BENCH")
_FORBIDDEN_AGENT_MARKERS = ("bench",)


class EngineLaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineTokens:

    agent: str
    control: str

    @classmethod
    def mint(cls) -> EngineTokens:
        return cls(agent=secrets.token_hex(TOKEN_BYTES), control=secrets.token_hex(TOKEN_BYTES))


def _passthrough(base: Mapping[str, str]) -> dict[str, str]:
    return {name: base[name] for name in _PASSTHROUGH_ENV if base.get(name)}


def _reject_forbidden(name: str) -> None:
    upper = name.upper()
    if any(upper.startswith(prefix) for prefix in _FORBIDDEN_AGENT_PREFIXES):
        raise ValueError(f"{name} must never reach the agent environment")
    lowered = name.lower()
    if any(marker in lowered for marker in _FORBIDDEN_AGENT_MARKERS):
        raise ValueError(f"{name} must never reach the agent environment")


def engine_env(
    tokens: EngineTokens, *, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    source = os.environ if base is None else base
    env = _passthrough(source)
    env[CONTROL_TOKEN_ENV] = tokens.control
    env[AGENT_TOKEN_ENV] = tokens.agent
    return env


def agent_env(
    *,
    port: int,
    token: str,
    workspace: str | Path,
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if base is None else base
    env = _passthrough(source)
    for name, value in dict(extra or {}).items():
        _reject_forbidden(name)
        if name in (SUNRIDGE_API_PORT_ENV, SUNRIDGE_API_TOKEN_ENV, SUNRIDGE_WORKSPACE_ENV):
            raise ValueError(f"{name} is set by the harness and cannot be overridden")
        env[name] = value
    env[SUNRIDGE_API_PORT_ENV] = str(int(port))
    env[SUNRIDGE_API_TOKEN_ENV] = token
    env[SUNRIDGE_WORKSPACE_ENV] = str(Path(workspace).resolve())
    return env


@dataclass(frozen=True)
class EngineLaunchSpec:

    db_path: Path
    run_dir: Path
    geo_dir: Path | None = None
    entry: Sequence[str] = field(default=ENGINE_ENTRY)
    python: str = field(default_factory=lambda: sys.executable)
    ready_timeout_seconds: float = READY_TIMEOUT_SECONDS
    stop_timeout_seconds: float = STOP_TIMEOUT_SECONDS

    def argv(self, ready_file: Path) -> list[str]:
        argv = [
            self.python,
            *self.entry,
            "--db",
            str(Path(self.db_path).resolve()),
            "--ready-file",
            str(ready_file),
        ]
        if self.geo_dir is not None:
            argv.extend(["--geo", str(Path(self.geo_dir).resolve())])
        return argv


class EngineProcess:

    def __init__(
        self,
        *,
        process: subprocess.Popen,
        port: int,
        tokens: EngineTokens,
        run_dir: Path,
        stdout_path: Path,
        stderr_path: Path,
        stop_timeout_seconds: float,
        streams: Sequence,
    ) -> None:
        self.process = process
        self.port = port
        self.tokens = tokens
        self.run_dir = run_dir
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.stop_timeout_seconds = stop_timeout_seconds
        self._streams = list(streams)
        self._stopped = False

    def __enter__(self) -> EngineProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> int | None:
        if self._stopped:
            return self.process.returncode
        self._stopped = True
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=self.stop_timeout_seconds)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=self.stop_timeout_seconds)
        finally:
            for stream in self._streams:
                try:
                    stream.close()
                except OSError:
                    pass
            self._streams = []
        return self.process.returncode


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _read_port(ready_file: Path) -> int:
    payload = json.loads(ready_file.read_text(encoding="utf-8"))
    port = int(payload["port"])
    if port <= 0:
        raise EngineLaunchError("engine reported an invalid port")
    return port


def launch_engine(
    spec: EngineLaunchSpec,
    *,
    tokens: EngineTokens | None = None,
    base_env: Mapping[str, str] | None = None,
) -> EngineProcess:
    tokens = tokens or EngineTokens.mint()
    run_dir = Path(spec.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    ready_file = run_dir / READY_FILENAME
    if ready_file.exists():
        ready_file.unlink()
    stdout_path = run_dir / STDOUT_FILENAME
    stderr_path = run_dir / STDERR_FILENAME
    stdout_stream = stdout_path.open("ab")
    stderr_stream = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            spec.argv(ready_file),
            env=engine_env(tokens, base=base_env),
            cwd=str(run_dir),
            stdin=subprocess.DEVNULL,
            stdout=stdout_stream,
            stderr=stderr_stream,
        )
    except OSError as exc:
        stdout_stream.close()
        stderr_stream.close()
        raise EngineLaunchError("failed to start the engine process") from exc

    engine = EngineProcess(
        process=process,
        port=0,
        tokens=tokens,
        run_dir=run_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stop_timeout_seconds=spec.stop_timeout_seconds,
        streams=(stdout_stream, stderr_stream),
    )

    deadline = time.monotonic() + spec.ready_timeout_seconds
    while True:
        if ready_file.is_file():
            try:
                engine.port = _read_port(ready_file)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                engine.stop()
                raise EngineLaunchError("engine ready file is unreadable") from exc
            return engine
        if process.poll() is not None:
            engine.stop()
            raise EngineLaunchError(
                f"engine process exited with code {process.returncode} before signalling "
                f"readiness: {_tail(stderr_path)}"
            )
        if time.monotonic() >= deadline:
            engine.stop()
            raise EngineLaunchError(
                f"engine did not signal readiness within {spec.ready_timeout_seconds} seconds: "
                f"{_tail(stderr_path)}"
            )
        time.sleep(READY_POLL_SECONDS)


__all__ = [
    "AGENT_TOKEN_ENV",
    "CONTROL_TOKEN_ENV",
    "ENGINE_ENTRY",
    "EngineLaunchError",
    "EngineLaunchSpec",
    "EngineProcess",
    "EngineTokens",
    "READY_FILENAME",
    "STDERR_FILENAME",
    "STDOUT_FILENAME",
    "agent_env",
    "engine_env",
    "launch_engine",
]
