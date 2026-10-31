from __future__ import annotations

import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .workspace import copy_regular_file, regular_file_sha256

PRIVATE_DIR_MODE = 0o700
TRAVERSE_ONLY_DIR_MODE = 0o711
SHARED_DIR_MODE = 0o755
SHARED_FILE_MODE = 0o644
WORKSPACE_ROOT_MODE = 0o1770
AGENT_HOME_MODE = 0o1777
SESSION_STAGING_PREFIX = "pi-sessions-"
SESSION_STAGING_DIRNAME = "sessions"
AGENT_HOME_PREFIX = "pi-agent-"
AGENT_HOME_DIRNAME = "agent"
PROBE_TIMEOUT_SECONDS = 20.0
REAP_ROUNDS = 50
REAP_PAUSE_SECONDS = 0.1
PROC_DIR = Path("/proc")

_PROBE_SCRIPT = 'if [ -d "$1" ]; then cd -- "$1" || ls -- "$1"; else head -c 1 -- "$1"; fi'
_PROBE_PATH = "/usr/bin:/bin"
_REAP_SCRIPT = "kill -KILL -1"
_ABSENT = object()


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxUser:
    name: str
    uid: int
    gid: int
    home: str

    @classmethod
    def lookup(cls, name: str) -> SandboxUser:
        try:
            entry = pwd.getpwnam(name)
        except KeyError as exc:
            raise SandboxError(f"sandbox user {name!r} does not exist") from exc
        if entry.pw_uid == 0:
            raise SandboxError("the sandbox user must not be root")
        return cls(name=entry.pw_name, uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir)

    def process_identity(self) -> dict[str, object]:
        return {"user": self.uid, "group": self.gid, "extra_groups": []}

    def identity_env(self) -> dict[str, str]:
        return {"HOME": self.home, "USER": self.name, "LOGNAME": self.name}


ProbeRunner = Callable[[SandboxUser, Path], bool]


def probe_readable(user: SandboxUser, path: Path) -> bool:
    try:
        return _run_as(user, [_PROBE_SCRIPT, "probe", str(path)]) == 0
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"the readability probe for {path} did not finish") from exc


def kill_user_processes(user: SandboxUser) -> None:
    if user.uid == 0:
        raise SandboxError("refusing to kill every process of root")
    try:
        for _ in range(REAP_ROUNDS):
            _run_as(user, [_REAP_SCRIPT])
            if not PROC_DIR.is_dir() or not live_processes(user.uid):
                return
            time.sleep(REAP_PAUSE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"killing the processes of {user.name} did not finish") from exc
    raise SandboxError(f"processes of {user.name} survived {REAP_ROUNDS} kills")


def live_processes(uid: int) -> list[int]:
    found: list[int] = []
    for entry in PROC_DIR.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        uids = {int(value) for value in fields.get("Uid", "").split()}
        if uid in uids and not fields.get("State", "").strip().startswith("Z"):
            found.append(int(entry.name))
    return sorted(found)


def _run_as(user: SandboxUser, script: Sequence[str]) -> int:
    return subprocess.run(
        ["/bin/sh", "-c", *script],
        cwd="/",
        env={"PATH": _PROBE_PATH, **user.identity_env()},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
        **user.process_identity(),
    ).returncode


def default_forbidden_paths(
    package_dirs: Sequence[Path], *, markers: Sequence[str] = ("pyproject.toml", ".git")
) -> tuple[Path, ...]:
    found: list[Path] = []
    for directory in package_dirs:
        resolved = Path(directory).resolve()
        for candidate in (resolved, *_repo_roots(resolved, markers)):
            if candidate not in found:
                found.append(candidate)
    return tuple(found)


def _repo_roots(directory: Path, markers: Sequence[str]) -> list[Path]:
    for parent in directory.parents:
        if any((parent / marker).exists() for marker in markers):
            return [parent]
    return []


def require_existing(paths: Sequence[Path]) -> None:
    missing = [str(path) for path in paths if not os.path.lexists(path)]
    if missing:
        raise SandboxError(
            f"forbidden paths {', '.join(missing)} do not exist; refusing to start"
        )


def preflight(
    user: SandboxUser,
    *,
    workspace: Path,
    forbidden: Sequence[Path],
    probe: ProbeRunner = probe_readable,
) -> None:
    if not probe(user, Path(workspace)):
        raise SandboxError(
            f"sandbox user {user.name} cannot read its workspace {workspace}; refusing to start"
        )
    readable = [str(path) for path in forbidden if probe(user, Path(path))]
    if readable:
        raise SandboxError(
            f"sandbox user {user.name} can read private paths {', '.join(readable)}; "
            "refusing to start"
        )


def secure_private_dir(path: Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
    os.chmod(path, PRIVATE_DIR_MODE)


def hand_over_workspace(
    root: Path, user: SandboxUser, *, read_only: Path, writable: Sequence[Path]
) -> None:
    require_sealed_ancestors(Path(root), user)
    os.chown(root, os.getuid(), user.gid)
    os.chmod(root, WORKSPACE_ROOT_MODE)
    _seal_read_only(Path(read_only))
    for path in writable:
        if Path(path).is_symlink():
            continue
        os.chown(path, user.uid, user.gid)
        os.chmod(path, SHARED_FILE_MODE)


def require_sealed_ancestors(root: Path, user: SandboxUser) -> None:
    for ancestor in Path(root).resolve().parents:
        try:
            status = ancestor.stat()
        except FileNotFoundError:
            continue
        open_to_agent = status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if status.st_uid == user.uid or (open_to_agent and not status.st_mode & stat.S_ISVTX):
            raise SandboxError(
                f"the sandbox user {user.name} could replace the workspace through "
                f"{ancestor}; refusing to start"
            )


def require_fresh_workspace(path: Path) -> None:
    candidate = Path(path)
    if not os.path.lexists(candidate):
        return
    if candidate.is_symlink() or not candidate.is_dir() or any(candidate.iterdir()):
        raise SandboxError(
            f"the workspace {candidate} already exists and is not an empty directory; "
            "refusing to reuse it"
        )


def create_session_staging(user: SandboxUser) -> Path:
    holder = Path(tempfile.mkdtemp(prefix=SESSION_STAGING_PREFIX))
    os.chmod(holder, TRAVERSE_ONLY_DIR_MODE)
    staging = holder / SESSION_STAGING_DIRNAME
    staging.mkdir(mode=PRIVATE_DIR_MODE)
    os.chown(staging, user.uid, user.gid)
    os.chmod(staging, PRIVATE_DIR_MODE)
    return staging


def remove_session_staging(staging: Path) -> None:
    shutil.rmtree(Path(staging).parent, ignore_errors=True)


def empty_directory(directory: Path) -> None:
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def create_agent_home(template: Path) -> Path:
    if Path(template).is_symlink() or not Path(template).is_dir():
        raise SandboxError(f"the pi agent template {template} is not a directory")
    holder = Path(tempfile.mkdtemp(prefix=AGENT_HOME_PREFIX))
    os.chmod(holder, TRAVERSE_ONLY_DIR_MODE)
    home = holder / AGENT_HOME_DIRNAME
    home.mkdir()
    reset_agent_home(template, home)
    return home


def remove_agent_home(home: Path) -> None:
    shutil.rmtree(Path(home).parent, ignore_errors=True)


def reset_agent_home(template: Path, home: Path) -> None:
    expected = _template_entries(Path(template))
    for current, dirnames, filenames in os.walk(home, topdown=True, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(current) / name
            relative = path.relative_to(home).as_posix()
            if _intact(path, expected.get(relative, _ABSENT)):
                continue
            _remove(path)
            if name in dirnames:
                dirnames.remove(name)
    for relative, digest in sorted(expected.items()):
        path = Path(home) / relative
        if os.path.lexists(path):
            continue
        if digest is None:
            path.mkdir()
            _seal(path, os.getuid(), os.getgid(), SHARED_DIR_MODE)
        else:
            copy_regular_file(Path(template) / relative, path)
            _seal(path, os.getuid(), os.getgid(), SHARED_FILE_MODE)
    _seal(Path(home), os.getuid(), os.getgid(), AGENT_HOME_MODE)


def _template_entries(template: Path) -> dict[str, str | None]:
    entries: dict[str, str | None] = {}
    for current, dirnames, filenames in os.walk(template, followlinks=False):
        for name in dirnames:
            path = Path(current) / name
            if not path.is_symlink():
                entries[path.relative_to(template).as_posix()] = None
        for name in filenames:
            path = Path(current) / name
            digest = regular_file_sha256(path)
            if digest is not None:
                entries[path.relative_to(template).as_posix()] = digest
    return entries


def _intact(path: Path, digest: str | None | object) -> bool:
    if digest is _ABSENT:
        return False
    status = path.lstat()
    if (status.st_uid, status.st_gid) != (os.getuid(), os.getgid()):
        return False
    if digest is None:
        return stat.S_ISDIR(status.st_mode) and stat.S_IMODE(status.st_mode) == SHARED_DIR_MODE
    return (
        stat.S_ISREG(status.st_mode)
        and stat.S_IMODE(status.st_mode) == SHARED_FILE_MODE
        and status.st_nlink == 1
        and regular_file_sha256(path) == digest
    )


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _seal_read_only(root: Path) -> None:
    if root.is_symlink():
        raise SandboxError(f"the read-only tree {root} is a symbolic link")
    owner = os.getuid()
    group = os.getgid()
    _seal(root, owner, group, SHARED_DIR_MODE)
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(current) / name
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                _seal(path, owner, group, SHARED_DIR_MODE)
            else:
                _seal(path, owner, group, SHARED_FILE_MODE)


def _seal(path: Path, owner: int, group: int, mode: int) -> None:
    os.chown(path, owner, group, follow_symlinks=False)
    os.chmod(path, mode)


__all__ = [
    "AGENT_HOME_DIRNAME",
    "AGENT_HOME_MODE",
    "AGENT_HOME_PREFIX",
    "PRIVATE_DIR_MODE",
    "PROBE_TIMEOUT_SECONDS",
    "PROC_DIR",
    "REAP_PAUSE_SECONDS",
    "REAP_ROUNDS",
    "SESSION_STAGING_DIRNAME",
    "SESSION_STAGING_PREFIX",
    "SHARED_DIR_MODE",
    "SHARED_FILE_MODE",
    "TRAVERSE_ONLY_DIR_MODE",
    "WORKSPACE_ROOT_MODE",
    "ProbeRunner",
    "SandboxError",
    "SandboxUser",
    "create_agent_home",
    "create_session_staging",
    "default_forbidden_paths",
    "empty_directory",
    "hand_over_workspace",
    "kill_user_processes",
    "live_processes",
    "preflight",
    "probe_readable",
    "remove_agent_home",
    "remove_session_staging",
    "require_existing",
    "require_fresh_workspace",
    "require_sealed_ancestors",
    "reset_agent_home",
    "secure_private_dir",
]
