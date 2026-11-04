from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import BinaryIO, Literal

from ..adapter import AdapterIntegrityError, AgentTurn, RunMetadata
from ..http_engine import (
    SUNRIDGE_API_PORT_ENV,
    SUNRIDGE_API_TOKEN_ENV,
    SUNRIDGE_WORKSPACE_ENV,
)
from .launcher import agent_env
from .sandbox import (
    SandboxError,
    SandboxUser,
    create_agent_home,
    empty_directory,
    kill_user_processes,
    remove_agent_home,
    reset_agent_home,
)
from .workspace import copy_regular_file, open_regular_file, regular_file_sha256

ADAPTER_NAME = "pi"
CONTEXT_POLICY = "controlled_daily_reset"
ATTEMPT_DEFINITION = (
    "one completed model turn in reply to the active call prompt or a reminder, "
    "with no terminal action committed"
)

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
PI_OFFLINE_ENV = "PI_OFFLINE"
MODEL_CATALOG_FILENAMES = ("models.json", "models-store.json")
PRIVATE_OUTPUT_MODE = 0o600

_SESSION_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_FAILED_STOP_REASONS = frozenset({"error", "aborted", "pending"})
_HOME_SCOPED_ENV = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR")
_SYSTEM_SEPARATOR = "\n\n"


class PiAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class PiConfig:
    provider: str
    model: str
    thinking: str
    executable: str = "pi"
    output_mode: str = "json"
    session_prefix: str = "sunridge"
    no_context_files: bool = True
    no_extensions: bool = True
    no_skills: bool = True
    no_prompt_templates: bool = True
    no_themes: bool = True
    approve_project_files: bool | None = False
    offline: bool = False
    agent_dir: str | None = None
    sandbox_user: str | None = None
    prompt_on_stdin: bool = True
    extra_args: tuple[str, ...] = ()
    secret_env: tuple[str, ...] = ()
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
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_total: float | None = None
    thinking_level: str | None = None


@dataclass(frozen=True)
class PiDayRecord:
    service_date: str
    session_id: str
    session_files: tuple[str, ...]
    system_sha256: str | None
    system_cwd: str | None
    thinking_level: str | None


class PiAdapter:
    def __init__(
        self,
        session_dir: Path,
        log_dir: Path,
        config: PiConfig,
        archive_dir: Path | None = None,
        reap: Callable[[SandboxUser], None] = kill_user_processes,
    ) -> None:
        self.config = config
        self.session_dir = Path(session_dir)
        self.log_dir = Path(log_dir)
        self.archive_dir = None if archive_dir is None else Path(archive_dir)
        self.reap = reap
        self.name = ADAPTER_NAME
        self._version: str | None = None
        self.context_policy = CONTEXT_POLICY
        self.attempt_definition = ATTEMPT_DEFINITION
        self.sandbox = (
            None if config.sandbox_user is None else SandboxUser.lookup(config.sandbox_user)
        )
        missing = [name for name in config.secret_env if not _host_value(name)]
        if missing:
            raise PiAdapterError(f"the pi environment is missing {', '.join(missing)}")
        self.agent_home = (
            create_agent_home(Path(config.agent_dir))
            if self.sandbox is not None and config.agent_dir
            else None
        )
        self.turns: list[PiTurnRecord] = []
        self.days: list[PiDayRecord] = []
        self._workspace: Path | None = None
        self._system_prompt: str = ""
        self._agent_env: dict[str, str] = {}
        self._service_date: date | None = None
        self._session_id: str | None = None
        self._turn_index = 0
        self._completed_turns_today = 0
        self._run_status: str | None = None
        self._day_system_texts: list[str] = []
        self._day_cwd: str | None = None
        self._day_thinking: str | None = None
        self.catalog_at_start: dict[str, str] | None = None

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
        if self.archive_dir is not None:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_at_start = self.catalog_digests()

    def start_day(self, service_date: date) -> None:
        if self._stages_sessions():
            empty_directory(self.session_dir)
        self._service_date = service_date
        self._session_id = _sanitize_id(
            f"{self.config.session_prefix}-{service_date.isoformat()}"
        )
        self._turn_index = 0
        self._completed_turns_today = 0
        self._day_system_texts = []
        self._day_cwd = None
        self._day_thinking = None

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
        self._restore_agent_home()
        started = time.monotonic()
        try:
            exit_code, timed_out = self._run_process(
                argv, prompt_path, stdout_path, stderr_path, deadline_s
            )
        finally:
            self._reap()
        duration_s = time.monotonic() - started

        parsed = _parse_json_stream(stdout_path)
        self._day_system_texts.extend(parsed.system_texts)
        if self._day_cwd is None:
            self._day_cwd = parsed.cwd
        if parsed.thinking_levels:
            self._day_thinking = parsed.thinking_levels[-1]
        ended_normally = (
            not timed_out
            and exit_code == 0
            and parsed.saw_assistant_message
            and parsed.stop_reason not in _FAILED_STOP_REASONS
        )
        if ended_normally:
            self._completed_turns_today += 1
        applied_levels = parsed.thinking_levels
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
            cache_read_tokens=parsed.cache_read_tokens,
            cache_write_tokens=parsed.cache_write_tokens,
            reasoning_tokens=parsed.reasoning_tokens,
            cost_total=parsed.cost_total,
            thinking_level=applied_levels[-1] if applied_levels else None,
        )
        self.turns.append(record)
        _write_json(self.log_dir / f"{stem}.turn.json", _record_as_dict(record, argv))
        self._require_thinking(applied_levels)
        self._require_preambles(parsed.preambles)
        if timed_out:
            raise TimeoutError(f"pi turn exceeded its deadline of {deadline_s} seconds")
        return turn

    def end_day(self, service_date: date) -> None:
        session_id = self._session_id
        completed = self._completed_turns_today
        system_texts = tuple(self._day_system_texts)
        self._service_date = None
        self._session_id = None
        self._turn_index = 0
        self._completed_turns_today = 0
        self._day_system_texts = []
        if session_id is None:
            return
        files = self._archive(self._session_files(session_id))
        self.days.append(
            PiDayRecord(
                service_date=service_date.isoformat(),
                session_id=session_id,
                session_files=tuple(str(path) for path in files),
                system_sha256=_sha256_text(system_texts[0]) if system_texts else None,
                system_cwd=self._day_cwd,
                thinking_level=self._day_thinking,
            )
        )
        if completed and not system_texts:
            raise AdapterIntegrityError(
                f"pi reported no system message for {service_date.isoformat()}"
            )

    def end_run(self, status: str) -> None:
        self._run_status = status
        self.archive_sessions()

    def archive_sessions(self) -> None:
        self._archive(_regular_files(self.session_dir.glob("*.jsonl")))

    def catalog_digests(self) -> dict[str, str]:
        if not self.config.agent_dir:
            return {}
        return _catalog(Path(self.config.agent_dir))

    def close(self) -> None:
        if self.agent_home is not None:
            remove_agent_home(self.agent_home)

    def _restore_agent_home(self) -> None:
        if self.agent_home is None or not self.config.agent_dir:
            return
        reset_agent_home(Path(self.config.agent_dir), self.agent_home)
        if _catalog(self.agent_home) != self.catalog_digests():
            raise AdapterIntegrityError(
                "the working pi agent dir does not match its template catalog"
            )

    def process_identity(self) -> dict[str, object]:
        return {} if self.sandbox is None else self.sandbox.process_identity()

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

    def _session_files(self, session_id: str) -> list[Path]:
        return _regular_files(self.session_dir.glob(f"*_{session_id}.jsonl"))

    def _reap(self) -> None:
        if self.sandbox is None:
            return
        try:
            self.reap(self.sandbox)
        except SandboxError as exc:
            raise AdapterIntegrityError(str(exc)) from exc

    def _stages_sessions(self) -> bool:
        return (
            self.archive_dir is not None
            and self.archive_dir.resolve() != self.session_dir.resolve()
        )

    def _archive(self, files: Sequence[Path]) -> list[Path]:
        if self.archive_dir is None or not self._stages_sessions():
            return list(files)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        moved: list[Path] = []
        for path in files:
            target = self.archive_dir / path.name
            if not copy_regular_file(path, target):
                continue
            path.unlink(missing_ok=True)
            moved.append(target)
        return moved

    def _require_thinking(self, applied: Sequence[str]) -> None:
        requested = self.config.thinking
        if not requested:
            return
        differing = sorted({level for level in applied if level != requested})
        if differing:
            raise AdapterIntegrityError(
                f"pi applied thinking level {', '.join(differing)} "
                f"but {requested} was requested"
            )

    def _require_preambles(self, preambles: Sequence[str | None]) -> None:
        sent = [preamble for preamble in preambles if preamble is not None]
        if any(preamble != self._system_prompt for preamble in sent):
            raise AdapterIntegrityError(
                "pi sent a system preamble that differs from the harness system prompt"
            )

    def _base_env(self) -> dict[str, str]:
        excluded = _HOME_SCOPED_ENV if self.sandbox is not None else ()
        extra: dict[str, str] = {}
        for name in self.config.passthrough_env:
            value = _host_value(name)
            if value is not None and name not in excluded:
                extra[name] = value
        extra.update(self.config.extra_env)
        for name in self.config.secret_env:
            extra[name] = _host_value(name) or ""
        if self.sandbox is not None:
            extra.update(self.sandbox.identity_env())
        if self.agent_home is not None:
            extra[PI_AGENT_DIR_ENV] = str(self.agent_home)
        elif self.config.agent_dir:
            extra[PI_AGENT_DIR_ENV] = str(self.config.agent_dir)
        if self.config.offline:
            extra[PI_OFFLINE_ENV] = "1"
        return extra

    def _build_env(self, env: Mapping[str, str]) -> dict[str, str]:
        reserved = (SUNRIDGE_API_PORT_ENV, SUNRIDGE_API_TOKEN_ENV, SUNRIDGE_WORKSPACE_ENV)
        missing = [name for name in reserved if not env.get(name)]
        if missing:
            raise PiAdapterError(f"the agent environment is missing {', '.join(missing)}")
        extra = self._base_env()
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
            _private_output(stdout_path) as stdout_file,
            _private_output(stderr_path) as stderr_file,
        ):
            process = subprocess.Popen(
                list(argv),
                cwd=str(self._workspace),
                env=self._agent_env,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                **self.process_identity(),
            )
            try:
                process.wait(timeout=deadline_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
            except BaseException:
                self._terminate(process)
                raise
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
        self._restore_agent_home()
        try:
            completed = subprocess.run(
                [self.config.executable, "--version"],
                cwd=self._probe_cwd(),
                env=self._agent_env or self._base_env(),
                capture_output=True,
                text=True,
                timeout=self.config.version_probe_timeout_s,
                check=False,
                **self.process_identity(),
            )
        except (OSError, subprocess.SubprocessError):
            return self.config.fallback_version
        reported = completed.stdout.strip().splitlines()
        if completed.returncode != 0 or not reported:
            return self.config.fallback_version
        return reported[-1].strip()

    def _probe_cwd(self) -> str | None:
        if self._workspace is not None:
            return str(self._workspace)
        if self.sandbox is None:
            return None
        return self.sandbox.home if Path(self.sandbox.home).is_dir() else os.sep


@dataclass(frozen=True)
class _ParsedStream:
    text: str
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    saw_assistant_message: bool
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_total: float | None = None
    thinking_levels: tuple[str, ...] = ()
    system_texts: tuple[str, ...] = ()
    preambles: tuple[str | None, ...] = ()
    cwd: str | None = None


def _parse_json_stream(stdout_path: Path) -> _ParsedStream:
    streamed: list[Mapping[str, object]] = []
    finished: list[Mapping[str, object]] | None = None
    cwd: str | None = None

    for event in _json_lines(stdout_path):
        kind = event.get("type")
        if kind == "session" and cwd is None:
            cwd = _optional_str(event.get("cwd"))
        elif kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict):
                streamed.append(message)
        elif kind == "agent_end":
            messages = event.get("messages")
            if isinstance(messages, list):
                finished = [*(finished or []), *(m for m in messages if isinstance(m, dict))]

    everything = finished if finished is not None else streamed
    messages = [m for m in everything if m.get("role") == "assistant"]
    systems = [m for m in everything if m.get("role") == "system"]
    texts: list[str] = []
    stop_reason: str | None = None
    totals = dict.fromkeys(("input", "output", "cacheRead", "cacheWrite", "reasoning"), 0)
    cost_total = 0.0
    saw_usage = False
    thinking_levels: list[str] = []
    for message in messages:
        texts.extend(_message_texts(message))
        reason = message.get("stopReason")
        if isinstance(reason, str):
            stop_reason = reason
        level = message.get("thinkingLevel")
        if isinstance(level, str):
            thinking_levels.append(level)
        usage = message.get("usage")
        if isinstance(usage, dict):
            saw_usage = True
            for name in totals:
                totals[name] += _as_int(usage.get(name))
            cost = usage.get("cost")
            if isinstance(cost, dict):
                cost_total += _as_float(cost.get("total"))

    def total(name: str) -> int | None:
        return totals[name] if saw_usage else None

    return _ParsedStream(
        text="\n".join(part for part in texts if part).strip(),
        stop_reason=stop_reason,
        input_tokens=total("input"),
        output_tokens=total("output"),
        saw_assistant_message=bool(messages),
        cache_read_tokens=total("cacheRead"),
        cache_write_tokens=total("cacheWrite"),
        reasoning_tokens=total("reasoning"),
        cost_total=cost_total if saw_usage else None,
        thinking_levels=tuple(thinking_levels),
        system_texts=tuple(_system_message_text(message) for message in systems),
        preambles=tuple(_preamble(message) for message in systems),
        cwd=cwd,
    )


def _preamble(message: Mapping[str, object]) -> str | None:
    sections = message.get("sections")
    if isinstance(sections, dict):
        return _optional_str(sections.get("preamble"))
    return None


def _system_message_text(message: Mapping[str, object]) -> str:
    parts = ["\n".join(_message_texts(message))]
    sections = message.get("sections")
    if isinstance(sections, dict):
        parts.extend(value for value in sections.values() if isinstance(value, str))
    return _SYSTEM_SEPARATOR.join(part for part in parts if part)


def _host_value(name: str) -> str | None:
    return os.environ.get(name)


def _catalog(directory: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in MODEL_CATALOG_FILENAMES:
        digest = regular_file_sha256(directory / name)
        if digest is not None:
            digests[name] = digest
    return digests


def _regular_files(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode):
            found.append(path)
    return sorted(found)


def _json_lines(path: Path) -> Iterator[Mapping[str, object]]:
    handle = open_regular_file(path)
    if handle is None:
        return
    with handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


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
        "cache_read_tokens": record.cache_read_tokens,
        "cache_write_tokens": record.cache_write_tokens,
        "reasoning_tokens": record.reasoning_tokens,
        "cost_total": record.cost_total,
        "thinking_level": record.thinking_level,
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


def _private_output(path: Path) -> BinaryIO:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, PRIVATE_OUTPUT_MODE
    )
    os.fchmod(descriptor, PRIVATE_OUTPUT_MODE)
    return os.fdopen(descriptor, "wb")


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
    "MODEL_CATALOG_FILENAMES",
    "PI_AGENT_DIR_ENV",
    "PI_OFFLINE_ENV",
    "PRIVATE_OUTPUT_MODE",
    "PiAdapter",
    "PiAdapterError",
    "PiConfig",
    "PiDayRecord",
    "PiTurnRecord",
]
