from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from ..adapter import AgentTurn, RunMetadata
from ..http_engine import (
    SUNRIDGE_API_PORT_ENV,
    SUNRIDGE_API_TOKEN_ENV,
    SUNRIDGE_WORKSPACE_ENV,
)
from .launcher import agent_env

ADAPTER_NAME = "pi"
CONTEXT_POLICY = "controlled_daily_reset"
ATTEMPT_DEFINITION = (
    "one completed model turn in reply to the active call prompt or a reminder, "
    "with no terminal action committed"
)

_SESSION_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_FAILED_STOP_REASONS = frozenset({"error", "aborted", "pending"})


class PiAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class PiConfig:
    executable: str = "pi"
    provider: str = "opencode-go"
    model: str = "gpt-5.6-luna:high"
    thinking: str | None = None
    output_mode: str = "json"
    session_prefix: str = "sunridge"
    no_context_files: bool = True
    no_extensions: bool = True
    no_skills: bool = True
    no_prompt_templates: bool = True
    no_themes: bool = True
    approve_project_files: bool | None = False
    offline: bool = False
    prompt_on_stdin: bool = True
    extra_args: tuple[str, ...] = ()
    passthrough_env: tuple[str, ...] = (
        "HOME",
        "PATH",
        "SHELL",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )
    extra_env: Mapping[str, str] = field(default_factory=dict)
    version_probe_timeout_s: float = 20.0
    terminate_grace_s: float = 5.0
    fallback_version: str = "unknown"


@dataclass(frozen=True)
class PiTurnRecord:
    service_date: str
    session_id: str
    kind: str
    turn_index: int
    exit_code: int | None
    timed_out: bool
    stop_reason: str | None
    duration_s: float
    input_tokens: int | None
    output_tokens: int | None
    ended_normally: bool
    stdout_path: str
    stderr_path: str


class PiAdapter:
    def __init__(
        self,
        session_dir: Path,
        log_dir: Path,
        run_label: str,
        config: PiConfig | None = None,
    ) -> None:
        self.config = config or PiConfig()
        self.session_dir = Path(session_dir)
        self.log_dir = Path(log_dir)
        self.run_label = _sanitize_id(run_label)
        self.name = ADAPTER_NAME
        self._version: str | None = None
        self.context_policy = CONTEXT_POLICY
        self.attempt_definition = ATTEMPT_DEFINITION
        self.turns: list[PiTurnRecord] = []
        self._workspace: Path | None = None
        self._system_prompt: str = ""
        self._agent_env: dict[str, str] = {}
        self._service_date: date | None = None
        self._session_id: str | None = None
        self._turn_index = 0
        self._run_status: str | None = None

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = self._probe_version()
        return self._version

    def start_run(self, workspace: Path, system_prompt: str, env: Mapping[str, str]) -> None:
        self._workspace = Path(workspace)
        self._system_prompt = system_prompt
        self._agent_env = self._build_env(env)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def start_day(self, service_date: date) -> None:
        self._service_date = service_date
        self._session_id = _sanitize_id(
            f"{self.config.session_prefix}-{self.run_label}-{service_date.isoformat()}"
        )
        self._turn_index = 0

    def send(
        self,
        prompt: str,
        kind: Literal["opening", "call", "reminder", "closing"],
        deadline_s: float,
    ) -> AgentTurn:
        workspace = self._workspace
        session_id = self._session_id
        if workspace is None:
            raise PiAdapterError("start_run must run before send")
        if session_id is None:
            raise PiAdapterError("start_day must run before send")

        self._turn_index += 1
        stem = self._turn_stem(kind)
        stdout_path = self.log_dir / f"{stem}.stdout.jsonl"
        stderr_path = self.log_dir / f"{stem}.stderr.log"
        prompt_path = self.log_dir / f"{stem}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        argv = self._build_argv(session_id, prompt)
        started = time.monotonic()
        exit_code, timed_out = self._run_process(
            argv, prompt_path, stdout_path, stderr_path, deadline_s
        )
        duration_s = time.monotonic() - started

        parsed = _parse_json_stream(stdout_path)
        ended_normally = (
            not timed_out
            and exit_code == 0
            and parsed.saw_assistant_message
            and parsed.stop_reason not in _FAILED_STOP_REASONS
        )
        turn = AgentTurn(
            text=parsed.text,
            ended_normally=ended_normally,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
        )
        record = PiTurnRecord(
            service_date=self._service_date.isoformat() if self._service_date else "",
            session_id=session_id,
            kind=kind,
            turn_index=self._turn_index,
            exit_code=exit_code,
            timed_out=timed_out,
            stop_reason=parsed.stop_reason,
            duration_s=duration_s,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            ended_normally=ended_normally,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        self.turns.append(record)
        _write_json(self.log_dir / f"{stem}.turn.json", _record_as_dict(record, argv))
        if timed_out:
            raise TimeoutError(f"pi turn exceeded its deadline of {deadline_s} seconds")
        return turn

    def end_day(self, service_date: date) -> None:
        self._service_date = None
        self._session_id = None
        self._turn_index = 0

    def end_run(self, status: str) -> None:
        self._run_status = status

    def describe(self) -> RunMetadata:
        return RunMetadata(
            name=self.name,
            version=self.version,
            context_policy=self.context_policy,
            attempt_definition=self.attempt_definition,
            model_id=f"{self.config.provider}/{self.config.model}",
        )

    def _turn_stem(self, kind: str) -> str:
        day = self._service_date.isoformat() if self._service_date else "no-day"
        return f"{day}-turn-{self._turn_index:04d}-{kind}"

    def _build_env(self, env: Mapping[str, str]) -> dict[str, str]:
        reserved = (SUNRIDGE_API_PORT_ENV, SUNRIDGE_API_TOKEN_ENV, SUNRIDGE_WORKSPACE_ENV)
        missing = [name for name in reserved if not env.get(name)]
        if missing:
            raise PiAdapterError(f"the agent environment is missing {', '.join(missing)}")
        extra: dict[str, str] = {}
        for name in self.config.passthrough_env:
            value = os.environ.get(name)
            if value is not None:
                extra[name] = value
        extra.update(self.config.extra_env)
        for name in reserved:
            extra.pop(name, None)
        return agent_env(
            port=int(env[SUNRIDGE_API_PORT_ENV]),
            token=env[SUNRIDGE_API_TOKEN_ENV],
            workspace=env[SUNRIDGE_WORKSPACE_ENV],
            extra=extra,
        )

    def _build_argv(self, session_id: str, prompt: str) -> list[str]:
        cfg = self.config
        argv = [
            cfg.executable,
            "--print",
            "--mode",
            cfg.output_mode,
            "--provider",
            cfg.provider,
            "--model",
            cfg.model,
            "--system-prompt",
            self._system_prompt,
            "--session-id",
            session_id,
            "--session-dir",
            str(self.session_dir),
        ]
        if cfg.thinking:
            argv += ["--thinking", cfg.thinking]
        if cfg.no_context_files:
            argv.append("--no-context-files")
        if cfg.no_extensions:
            argv.append("--no-extensions")
        if cfg.no_skills:
            argv.append("--no-skills")
        if cfg.no_prompt_templates:
            argv.append("--no-prompt-templates")
        if cfg.no_themes:
            argv.append("--no-themes")
        if cfg.approve_project_files is True:
            argv.append("--approve")
        elif cfg.approve_project_files is False:
            argv.append("--no-approve")
        if cfg.offline:
            argv.append("--offline")
        argv += list(cfg.extra_args)
        if not cfg.prompt_on_stdin:
            argv.append(prompt)
        return argv

    def _run_process(
        self,
        argv: Sequence[str],
        prompt_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        deadline_s: float,
    ) -> tuple[int | None, bool]:
        stdin_source = prompt_path if self.config.prompt_on_stdin else os.devnull
        timed_out = False
        with (
            open(stdin_source, "rb") as stdin_file,
            open(stdout_path, "wb") as stdout_file,
            open(stderr_path, "wb") as stderr_file,
        ):
            process = subprocess.Popen(
                list(argv),
                cwd=str(self._workspace),
                env=self._agent_env,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            try:
                process.wait(timeout=deadline_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
        return process.returncode, timed_out

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        _signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=self.config.terminate_grace_s)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=self.config.terminate_grace_s)
            except subprocess.TimeoutExpired:
                pass

    def _probe_version(self) -> str:
        try:
            completed = subprocess.run(
                [self.config.executable, "--version"],
                cwd=str(self._workspace) if self._workspace else None,
                env=self._agent_env or None,
                capture_output=True,
                text=True,
                timeout=self.config.version_probe_timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return self.config.fallback_version
        reported = completed.stdout.strip().splitlines()
        if completed.returncode != 0 or not reported:
            return self.config.fallback_version
        return reported[-1].strip()


@dataclass(frozen=True)
class _ParsedStream:
    text: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    saw_assistant_message: bool


def _parse_json_stream(stdout_path: Path) -> _ParsedStream:
    streamed: list[Mapping[str, object]] = []
    finished: list[Mapping[str, object]] | None = None

    try:
        raw = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "message_end":
            message = event.get("message")
            if _is_assistant(message):
                streamed.append(message)
        elif kind == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                finished = [m for m in messages if _is_assistant(m)]

    messages = finished if finished is not None else streamed
    texts: list[str] = []
    stop_reason: str | None = None
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    for message in messages:
        texts.extend(_message_texts(message))
        reason = message.get("stopReason")
        if isinstance(reason, str):
            stop_reason = reason
        usage = message.get("usage")
        if isinstance(usage, dict):
            saw_usage = True
            input_tokens += _as_int(usage.get("input"))
            output_tokens += _as_int(usage.get("output"))

    return _ParsedStream(
        text="\n".join(part for part in texts if part).strip(),
        stop_reason=stop_reason,
        input_tokens=input_tokens if saw_usage else None,
        output_tokens=output_tokens if saw_usage else None,
        saw_assistant_message=bool(messages),
    )


def _is_assistant(message: object) -> bool:
    return isinstance(message, dict) and message.get("role") == "assistant"


def _message_texts(message: Mapping[str, object]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return parts


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _record_as_dict(record: PiTurnRecord, argv: Sequence[str]) -> dict[str, object]:
    payload = {
        "service_date": record.service_date,
        "session_id": record.session_id,
        "kind": record.kind,
        "turn_index": record.turn_index,
        "exit_code": record.exit_code,
        "timed_out": record.timed_out,
        "stop_reason": record.stop_reason,
        "duration_s": round(record.duration_s, 3),
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "ended_normally": record.ended_normally,
        "stdout_path": record.stdout_path,
        "stderr_path": record.stderr_path,
        "argv": _redact_argv(argv),
    }
    return payload


def _redact_argv(argv: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            redacted.append("<system-prompt>")
            skip_next = False
            continue
        redacted.append(item)
        skip_next = item == "--system-prompt"
    return redacted


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def _sanitize_id(value: str) -> str:
    cleaned = _SESSION_ID_ALLOWED.sub("-", value).strip("-._")
    return cleaned or "session"


__all__ = [
    "ADAPTER_NAME",
    "ATTEMPT_DEFINITION",
    "CONTEXT_POLICY",
    "PiAdapter",
    "PiAdapterError",
    "PiConfig",
    "PiTurnRecord",
]
