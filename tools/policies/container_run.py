from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from policies.calibration import SCENARIO_DIR
from policies.pi_run import (
    ARGS_FILE_PREFIX,
    INTERRUPTED_EXIT_CODE,
    REFUSED_EXIT_CODE,
    InterruptGuard,
)
from trades_bench.harness.sandbox import (
    PRIVATE_DIR_MODE,
    SHARED_DIR_MODE,
    SHARED_FILE_MODE,
    SandboxError,
    SandboxUser,
    kill_user_processes,
)
from trades_bench.harness.workspace import copy_regular_file

REPO_ROOT = Path("/repo")
ROOT_HOME = Path("/root")
STATE_DIR = Path("/var/lib/trades-bench")
RUN_DIR = STATE_DIR / "run"
WORKSPACE_PARENT = Path("/srv/sunridge")
WORKSPACE = WORKSPACE_PARENT / "workspace"
PI_AGENT_DIR = Path("/opt/pi-agent")
ARGS_FILE = ROOT_HOME / "pi-run.args"
SANDBOX_USER = "sunridge"
DB_FILENAME = "run.db"
WORKSPACE_EXPORT_DIRNAME = "workspace"
EXPORT_REPORT_FILENAME = "workspace-export.json"
SUMMARY_FILENAME = "container-summary.json"
SETTINGS_FILENAME = "settings.json"
WORKSPACE_FILE_LIMIT_BYTES = 64 * 1024 * 1024
WORKSPACE_TOTAL_LIMIT_BYTES = 512 * 1024 * 1024
WORKSPACE_ENTRY_LIMIT = 20_000
MIRROR_INTERVAL_SECONDS = 300
PI_RUN_GRACE_SECONDS = 60
PI_RUN_GROUP_EXIT_SECONDS = 10
GROUP_POLL_SECONDS = 0.05
ARGS_FILE_MODE = 0o600
TEST_LABEL_PREFIX = "test-"
TOOLS_PYTHONPATH = "tools"
DEFAULT_SEED = 5
DEFAULT_SCENARIO = "oct_default"
DEFAULT_RESULTS = STATE_DIR / "results"
DEFAULT_EXECUTABLE = "pi"
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UNKNOWN_VERSION = "unknown"
PACKAGE_NAME = "trades-bench"

PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "xai": "XAI_API_KEY",
    "meta": "META_API_KEY",
}

REQUIRED_ENV = ("TB_PROVIDER", "TB_MODEL", "TB_THINKING")
SKIP_SYMLINK = "symlink"
SKIP_NOT_REGULAR = "not_regular_file"
SKIP_FILE_TOO_LARGE = "file_too_large"
SKIP_TOTAL_LIMIT = "total_size_limit"
SKIP_ENTRY_LIMIT = "entry_limit"
SKIP_CHANGED = "changed_during_export"
SKIP_UNREADABLE = "unreadable"
SKIP_COPY_FAILED = "copy_failed"
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerConfig:
    provider: str
    model: str
    thinking: str
    seed: int
    scenario: str
    label: str
    results: Path
    executable: str
    test_executable: bool = False

    @property
    def key_env(self) -> str:
        return PROVIDER_KEY_ENV[self.provider]

    @property
    def out_dir(self) -> Path:
        return self.results / self.label


def config_from_env(env: Mapping[str, str]) -> ContainerConfig:
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ContainerError(f"set {', '.join(missing)}")
    provider = env["TB_PROVIDER"]
    if provider not in PROVIDER_KEY_ENV:
        raise ContainerError(f"TB_PROVIDER must be one of {', '.join(sorted(PROVIDER_KEY_ENV))}")
    if not env.get(PROVIDER_KEY_ENV[provider]):
        raise ContainerError(f"set {PROVIDER_KEY_ENV[provider]} for provider {provider}")
    try:
        seed = int(env.get("TB_SEED") or DEFAULT_SEED)
    except ValueError as exc:
        raise ContainerError("TB_SEED must be an integer") from exc
    scenario = env.get("TB_SCENARIO") or DEFAULT_SCENARIO
    if not _safe_name(scenario) or not (SCENARIO_DIR / f"{scenario}.json").is_file():
        raise ContainerError(f"unknown TB_SCENARIO {scenario!r}")
    model, thinking = env["TB_MODEL"], env["TB_THINKING"]
    label = env.get("TB_LABEL") or _UNSAFE_CHARS.sub("_", f"{model}-{thinking}-a1")
    if not _safe_name(label):
        raise ContainerError("TB_LABEL may only use letters, digits, '.', '_' and '-'")
    test_executable = bool(env.get("TB_PI_EXECUTABLE"))
    if test_executable and not label.startswith(TEST_LABEL_PREFIX):
        raise ContainerError(
            f"TB_PI_EXECUTABLE is for tests only; set a TB_LABEL starting with {TEST_LABEL_PREFIX!r}"
        )
    return ContainerConfig(
        provider=provider,
        model=model,
        thinking=thinking,
        seed=seed,
        scenario=scenario,
        label=label,
        results=Path(env.get("TB_RESULTS") or DEFAULT_RESULTS),
        executable=env.get("TB_PI_EXECUTABLE") or DEFAULT_EXECUTABLE,
        test_executable=test_executable,
    )


def _safe_name(value: str) -> bool:
    return bool(_SAFE_NAME.fullmatch(value)) and value not in {".", ".."}


def pi_run_args(config: ContainerConfig) -> list[str]:
    tokens = [
        "--run-dir", str(RUN_DIR),
        "--workspace", str(WORKSPACE),
        "--seed", str(config.seed),
        "--scenario", config.scenario,
        "--provider", config.provider,
        "--model", config.model,
        "--thinking", config.thinking,
        "--sandbox-user", SANDBOX_USER,
        "--pi-agent-dir", str(PI_AGENT_DIR),
        "--offline",
        "--pi-env", config.key_env,
    ]
    if config.test_executable:
        tokens += ["--executable", config.executable]
    for path in (REPO_ROOT, ROOT_HOME, config.results):
        tokens += ["--forbid-path", str(path)]
    return tokens


def write_args(tokens: Sequence[str], path: Path) -> None:
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ARGS_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(tokens) + "\n")


def pi_env(config: ContainerConfig, env: Mapping[str, str]) -> dict[str, str]:
    return {
        "HOME": str(ROOT_HOME),
        "PATH": env.get("PATH") or DEFAULT_PATH,
        "LANG": "C.UTF-8",
        "PI_CODING_AGENT_DIR": str(PI_AGENT_DIR),
        config.key_env: env[config.key_env],
    }


def run_pi(config: ContainerConfig, args: Sequence[str], env: Mapping[str, str]) -> str:
    completed = subprocess.run(
        [config.executable, *args], env=dict(env), capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise ContainerError(
            f"pi {' '.join(args)} exited {completed.returncode}: {completed.stderr.strip()[-500:]}"
        )
    return completed.stdout


def catalog_line(listing: str, provider: str, model: str) -> str:
    for line in listing.splitlines():
        if line.split()[:2] == [provider, model]:
            return line.strip()
    raise ContainerError(f"pi does not list the model {provider}/{model}")


def seal_agent_template(directory: Path) -> None:
    settings = directory / SETTINGS_FILENAME
    if not os.path.lexists(settings):
        settings.write_text("{}\n", encoding="utf-8")
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        for name in dirnames:
            path = Path(current) / name
            if not path.is_symlink():
                path.chmod(SHARED_DIR_MODE)
        for name in filenames:
            path = Path(current) / name
            if not path.is_symlink():
                path.chmod(SHARED_FILE_MODE)
    directory.chmod(PRIVATE_DIR_MODE)


def prepare_pi(config: ContainerConfig, env: Mapping[str, str]) -> str:
    PI_AGENT_DIR.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    PI_AGENT_DIR.chmod(PRIVATE_DIR_MODE)
    run_pi(config, ["update", "--models"], env)
    seal_agent_template(PI_AGENT_DIR)
    return catalog_line(run_pi(config, ["--list-models"], env), config.provider, config.model)


def pi_version(config: ContainerConfig, env: Mapping[str, str]) -> str:
    try:
        lines = run_pi(config, ["--version"], env).strip().splitlines()
    except (OSError, ContainerError):
        return UNKNOWN_VERSION
    return lines[-1].strip() if lines else UNKNOWN_VERSION


def claim_out_dir(out_dir: Path) -> bool:
    try:
        out_dir.mkdir()
        created = True
    except FileExistsError:
        if out_dir.is_symlink() or not out_dir.is_dir() or any(out_dir.iterdir()):
            raise _label_taken(out_dir) from None
        created = False
    try:
        (out_dir / SUMMARY_FILENAME).touch(exist_ok=False)
    except FileExistsError:
        raise _label_taken(out_dir) from None
    return created


def _label_taken(out_dir: Path) -> ContainerError:
    return ContainerError(f"{out_dir} already holds results; choose another TB_LABEL")


def release_out_dir(out_dir: Path, *, created: bool) -> None:
    with suppress(OSError):
        (out_dir / SUMMARY_FILENAME).unlink()
        if created:
            out_dir.rmdir()


def prepare_dirs(config: ContainerConfig) -> None:
    STATE_DIR.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    config.results.mkdir(parents=True, exist_ok=True)
    for path in (STATE_DIR, REPO_ROOT, ROOT_HOME):
        path.chmod(PRIVATE_DIR_MODE)
    WORKSPACE_PARENT.mkdir(parents=True, exist_ok=True)
    for path in (WORKSPACE_PARENT, WORKSPACE_PARENT.parent):
        os.chown(path, 0, 0)
        path.chmod(SHARED_DIR_MODE)


def backup_database(source: Path, target: Path) -> None:
    partial = target.with_name(f"{target.name}.partial")
    partial.unlink(missing_ok=True)
    with (
        closing(sqlite3.connect(source.resolve().as_uri(), uri=True)) as reader,
        closing(sqlite3.connect(partial)) as writer,
    ):
        reader.backup(writer)
    partial.replace(target)


def _skip_database(directory: str, names: list[str]) -> list[str]:
    return [name for name in names if name.startswith(DB_FILENAME)]


def mirror_run(run_dir: Path, out_dir: Path, *, final: bool = False) -> None:
    if not run_dir.is_dir():
        return
    shutil.copytree(run_dir, out_dir, ignore=_skip_database, symlinks=True, dirs_exist_ok=True)
    if final and (run_dir / DB_FILENAME).is_file():
        backup_database(run_dir / DB_FILENAME, out_dir / DB_FILENAME)


def try_mirror(run_dir: Path, out_dir: Path, *, final: bool = False) -> str | None:
    try:
        mirror_run(run_dir, out_dir, final=final)
    except (OSError, shutil.Error, sqlite3.Error) as exc:
        print(f"mirror skipped: {exc}", file=sys.stderr, flush=True)
        return str(exc)
    return None


def mirror_until(stop: threading.Event, run_dir: Path, out_dir: Path, interval: float) -> None:
    while not stop.wait(interval):
        try_mirror(run_dir, out_dir)


@dataclass(frozen=True)
class ExportLimits:
    file_bytes: int = WORKSPACE_FILE_LIMIT_BYTES
    total_bytes: int = WORKSPACE_TOTAL_LIMIT_BYTES
    entries: int = WORKSPACE_ENTRY_LIMIT


@dataclass
class _Export:
    limits: ExportLimits
    entries: int = 0
    files: int = 0
    bytes: int = 0
    truncated: bool = False
    skipped: list[dict[str, str]] = field(default_factory=list)

    def admit(self, relative: Path) -> bool:
        if self.entries >= self.limits.entries:
            self.truncated = True
            self.skip(relative, SKIP_ENTRY_LIMIT)
            return False
        self.entries += 1
        return True

    def skip(self, relative: Path, reason: str) -> None:
        self.skipped.append({"path": relative.as_posix(), "reason": reason})

    def report(self) -> dict[str, object]:
        return {
            "limits": {
                "file_bytes": self.limits.file_bytes,
                "total_bytes": self.limits.total_bytes,
                "entries": self.limits.entries,
            },
            "exported_files": self.files,
            "exported_bytes": self.bytes,
            "truncated": self.truncated,
            "skipped": self.skipped,
        }


def export_workspace(
    workspace: Path, target: Path, limits: ExportLimits = ExportLimits()
) -> dict[str, object]:
    target.mkdir()
    export = _Export(limits)
    for current, dirnames, filenames in os.walk(workspace, followlinks=False):
        relative = Path(current).relative_to(workspace)
        dirnames.sort()
        for name in list(dirnames):
            if not export.admit(relative / name):
                break
            reason = _export_dir(Path(current, name), target / relative / name)
            if reason is not None:
                dirnames.remove(name)
                export.skip(relative / name, reason)
        if export.truncated:
            break
        for name in sorted(filenames):
            if not export.admit(relative / name):
                break
            reason = _export_file(Path(current, name), target / relative / name, export)
            if reason is not None:
                export.skip(relative / name, reason)
        if export.truncated:
            break
    report = export.report()
    (target.parent / EXPORT_REPORT_FILENAME).write_text(json.dumps(report, indent=2) + "\n")
    return report


def _export_dir(source: Path, target: Path) -> str | None:
    if source.is_symlink():
        return SKIP_SYMLINK
    try:
        target.mkdir()
    except OSError:
        return SKIP_COPY_FAILED
    return None


def _export_file(source: Path, target: Path, export: _Export) -> str | None:
    try:
        info = source.lstat()
    except OSError:
        return SKIP_UNREADABLE
    if stat.S_ISLNK(info.st_mode):
        return SKIP_SYMLINK
    if not stat.S_ISREG(info.st_mode):
        return SKIP_NOT_REGULAR
    if info.st_size > export.limits.file_bytes:
        return SKIP_FILE_TOO_LARGE
    remaining = export.limits.total_bytes - export.bytes
    if info.st_size > remaining:
        return SKIP_TOTAL_LIMIT
    try:
        copied = copy_regular_file(
            source, target, max_bytes=min(export.limits.file_bytes, remaining)
        )
        size = target.stat().st_size if copied else 0
    except OSError:
        with suppress(OSError):
            target.unlink()
        return SKIP_COPY_FAILED
    if not copied:
        return SKIP_CHANGED
    export.files += 1
    export.bytes += size
    return None


def try_export(workspace: Path, target: Path) -> tuple[dict[str, object] | None, str | None]:
    if not workspace.is_dir():
        return None, f"{workspace} does not exist"
    try:
        return export_workspace(workspace, target), None
    except OSError as exc:
        print(f"workspace export failed: {exc}", file=sys.stderr, flush=True)
        return None, str(exc)


def try_kill(sandbox: SandboxUser) -> str | None:
    try:
        kill_user_processes(sandbox)
    except (SandboxError, OSError) as exc:
        print(f"sandbox processes survived: {exc}", file=sys.stderr, flush=True)
        return str(exc)
    return None


def hand_over_results(out_dir: Path, uid: int, gid: int) -> None:
    os.chown(out_dir, uid, gid, follow_symlinks=False)
    for current, dirnames, filenames in os.walk(out_dir, followlinks=False):
        for name in [*dirnames, *filenames]:
            os.chown(Path(current, name), uid, gid, follow_symlinks=False)


def package_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


def require_root() -> SandboxUser:
    if os.geteuid() != 0:
        raise ContainerError("container_run must run as root inside the benchmark container")
    return SandboxUser.lookup(SANDBOX_USER)


def start_pi_run(environment: Mapping[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "policies.pi_run", f"{ARGS_FILE_PREFIX}{ARGS_FILE}"],
        cwd=REPO_ROOT,
        env={**environment, "PYTHONPATH": TOOLS_PYTHONPATH},
        start_new_session=True,
    )


def finish_pi_run(process: subprocess.Popen[bytes], grace_seconds: float) -> int:
    with suppress(OSError):
        process.send_signal(signal.SIGTERM)
    try:
        return process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        print("pi_run did not finish its cleanup in time; killing it", file=sys.stderr, flush=True)
        process.kill()
        return process.wait()


def end_process_group(process: subprocess.Popen[bytes], timeout: float) -> str | None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return None
        if time.monotonic() >= deadline:
            break
        time.sleep(GROUP_POLL_SECONDS)
    message = f"processes started by pi_run survived SIGKILL of group {process.pid}"
    print(message, file=sys.stderr, flush=True)
    return message


def run(config: ContainerConfig, env: Mapping[str, str], guard: InterruptGuard) -> int:
    sandbox = require_root()
    prepare_dirs(config)
    created = claim_out_dir(config.out_dir)
    try:
        environment = pi_env(config, env)
        catalog = prepare_pi(config, environment)
        version = pi_version(config, environment)
        write_args(pi_run_args(config), ARGS_FILE)
    except BaseException:
        release_out_dir(config.out_dir, created=created)
        raise
    returncode = supervise(config, sandbox, environment, guard, catalog=catalog, version=version)
    return INTERRUPTED_EXIT_CODE if guard.received else returncode


def supervise(
    config: ContainerConfig,
    sandbox: SandboxUser,
    environment: Mapping[str, str],
    guard: InterruptGuard,
    *,
    catalog: str,
    version: str,
) -> int | None:
    stop = threading.Event()
    mirror = threading.Thread(
        target=mirror_until,
        args=(stop, RUN_DIR, config.out_dir, MIRROR_INTERVAL_SECONDS),
        daemon=True,
    )
    returncode: int | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        mirror.start()
        process = start_pi_run(environment)
        returncode = process.wait()
    except KeyboardInterrupt:
        if process is not None:
            returncode = finish_pi_run(process, PI_RUN_GRACE_SECONDS)
    finally:
        guard.shield()
        stop.set()
        if mirror.is_alive():
            mirror.join()
        group_error = (
            None if process is None else end_process_group(process, PI_RUN_GROUP_EXIT_SECONDS)
        )
        mirror_error = try_mirror(RUN_DIR, config.out_dir, final=True)
        kill_error = try_kill(sandbox)
        workspace_export, export_error = (
            try_export(WORKSPACE, config.out_dir / WORKSPACE_EXPORT_DIRNAME)
            if kill_error is None
            else (None, "skipped because sandbox processes survived")
        )
        summary = {
            "label": config.label,
            "provider": config.provider,
            "model": config.model,
            "thinking": config.thinking,
            "seed": config.seed,
            "scenario": config.scenario,
            "pi_executable": config.executable,
            "test_executable": config.test_executable,
            "returncode": returncode,
            "interrupted": guard.received,
            "catalog_line": catalog,
            "pi_version": version,
            "trades_bench_version": package_version(),
            "workspace_export": workspace_export,
            "workspace_export_error": export_error,
            "pi_run_group_error": group_error,
            "final_mirror_error": mirror_error,
            "kill_error": kill_error,
        }
        (config.out_dir / SUMMARY_FILENAME).write_text(json.dumps(summary, indent=2) + "\n")
        owner = config.results.stat()
        if owner.st_uid != 0:
            hand_over_results(config.out_dir, owner.st_uid, owner.st_gid)
    return returncode


def main(env: Mapping[str, str] | None = None) -> int:
    environment = os.environ if env is None else env
    with InterruptGuard() as guard:
        try:
            config = config_from_env(environment)
            return run(config, environment, guard)
        except (ContainerError, SandboxError) as exc:
            print(f"container_run refused to start: {exc}", file=sys.stderr)
            return REFUSED_EXIT_CODE
        except KeyboardInterrupt:
            print("container_run interrupted; partial results were kept", file=sys.stderr)
            return INTERRUPTED_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARGS_FILE",
    "EXPORT_REPORT_FILENAME",
    "MIRROR_INTERVAL_SECONDS",
    "PI_AGENT_DIR",
    "PI_RUN_GRACE_SECONDS",
    "PI_RUN_GROUP_EXIT_SECONDS",
    "PROVIDER_KEY_ENV",
    "RUN_DIR",
    "SANDBOX_USER",
    "STATE_DIR",
    "SUMMARY_FILENAME",
    "TEST_LABEL_PREFIX",
    "WORKSPACE",
    "WORKSPACE_ENTRY_LIMIT",
    "WORKSPACE_FILE_LIMIT_BYTES",
    "WORKSPACE_TOTAL_LIMIT_BYTES",
    "ContainerConfig",
    "ContainerError",
    "ExportLimits",
    "backup_database",
    "catalog_line",
    "claim_out_dir",
    "config_from_env",
    "end_process_group",
    "export_workspace",
    "finish_pi_run",
    "hand_over_results",
    "main",
    "mirror_run",
    "pi_run_args",
    "pi_version",
    "prepare_pi",
    "release_out_dir",
    "run",
    "seal_agent_template",
    "start_pi_run",
    "supervise",
    "try_export",
    "try_kill",
    "try_mirror",
    "write_args",
]
