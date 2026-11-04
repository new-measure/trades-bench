from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from trades_bench.config import load_run_config
from trades_bench.harness import workspace
from trades_bench.harness.sandbox import (
    AGENT_HOME_MODE,
    PRIVATE_DIR_MODE,
    SHARED_DIR_MODE,
    SHARED_FILE_MODE,
    TRAVERSE_ONLY_DIR_MODE,
    WORKSPACE_ROOT_MODE,
    SandboxError,
    SandboxUser,
    create_agent_home,
    create_session_staging,
    default_forbidden_paths,
    hand_over_workspace,
    kill_user_processes,
    live_processes,
    preflight,
    probe_readable,
    remove_agent_home,
    remove_session_staging,
    require_existing,
    require_fresh_workspace,
    require_sealed_ancestors,
    reset_agent_home,
    secure_private_dir,
)

USER = SandboxUser(name="sunridge", uid=4242, gid=4242, home="/home/sunridge")
ROOT_ON_LINUX = sys.platform.startswith("linux") and os.geteuid() == 0


def _probe(readable: set[Path]):
    probed: list[Path] = []

    def probe(user: SandboxUser, path: Path) -> bool:
        assert user is USER
        probed.append(path)
        return path in readable

    return probe, probed


def test_preflight_passes_when_only_the_workspace_is_readable(tmp_path):
    workspace_dir = tmp_path / "workspace"
    forbidden = [tmp_path / "run" / "run.db", tmp_path / "run", tmp_path / "secrets"]
    probe, probed = _probe({workspace_dir})

    preflight(USER, workspace=workspace_dir, forbidden=forbidden, probe=probe)

    assert probed == [workspace_dir, *forbidden]


def test_preflight_refuses_when_any_forbidden_path_is_readable(tmp_path):
    workspace_dir = tmp_path / "workspace"
    db_path = tmp_path / "run" / "run.db"
    extra = tmp_path / "secrets"
    probe, _ = _probe({workspace_dir, db_path, extra})

    with pytest.raises(SandboxError) as raised:
        preflight(
            USER, workspace=workspace_dir, forbidden=[db_path, tmp_path / "run", extra], probe=probe
        )

    message = str(raised.value)
    assert f"private paths {db_path}, {extra};" in message
    assert "refusing to start" in message


def test_preflight_refuses_when_the_workspace_is_unreadable(tmp_path):
    probe, _ = _probe(set())

    with pytest.raises(SandboxError, match="cannot read its workspace"):
        preflight(USER, workspace=tmp_path / "workspace", forbidden=[], probe=probe)


def test_a_sandbox_user_carries_no_supplementary_groups():
    assert USER.process_identity() == {"user": 4242, "group": 4242, "extra_groups": []}
    assert USER.identity_env() == {
        "HOME": "/home/sunridge",
        "USER": "sunridge",
        "LOGNAME": "sunridge",
    }


def test_an_unknown_sandbox_user_is_refused():
    with pytest.raises(SandboxError, match="does not exist"):
        SandboxUser.lookup("no-such-user-for-the-sandbox-test")


def test_secure_private_dir_closes_the_run_directory(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o755)

    secure_private_dir(run_dir)

    assert stat.S_IMODE(run_dir.stat().st_mode) == PRIVATE_DIR_MODE


def test_a_missing_or_empty_workspace_is_fresh(tmp_path):
    require_fresh_workspace(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    require_fresh_workspace(tmp_path / "empty")


def test_a_used_or_linked_workspace_is_refused(tmp_path):
    used = tmp_path / "used"
    used.mkdir()
    (used / "MEMORY.md").write_text("notes from another run", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "empty-target", target_is_directory=True)
    (tmp_path / "empty-target").mkdir()
    plain = tmp_path / "plain"
    plain.write_text("", encoding="utf-8")

    for path in (used, linked, plain):
        with pytest.raises(SandboxError, match="refusing to reuse it"):
            require_fresh_workspace(path)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_the_real_probe_counts_a_traversable_directory_as_readable():
    user = SandboxUser.lookup("nobody")
    base = Path(tempfile.mkdtemp(prefix="sandbox-traverse-", dir="/tmp"))
    try:
        os.chmod(base, SHARED_DIR_MODE)
        sources = base / "sources"
        sources.mkdir()
        (sources / "scenario.json").write_text("{}", encoding="utf-8")
        os.chmod(sources / "scenario.json", SHARED_FILE_MODE)
        os.chmod(sources, 0o711)

        assert probe_readable(user, sources / "scenario.json")
        assert probe_readable(user, sources)
        with pytest.raises(SandboxError):
            preflight(user, workspace=base, forbidden=[sources])
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_the_sandbox_user_cannot_swap_its_session_staging_dir():
    user = SandboxUser.lookup("nobody")
    staging = create_session_staging(user)
    try:
        swapped = subprocess.run(
            ["/bin/sh", "-c", 'mv -- "$1" "$1.moved"', "swap", str(staging)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            **user.process_identity(),
        )

        assert swapped.returncode != 0
        assert staging.is_dir() and not staging.is_symlink()
        assert staging.stat().st_uid == user.uid
        assert stat.S_IMODE(staging.stat().st_mode) == PRIVATE_DIR_MODE
        assert probe_readable(user, staging)
    finally:
        remove_session_staging(staging)
    assert not staging.parent.exists()


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_the_real_probe_and_workspace_hand_over_as_an_unprivileged_user():
    user = SandboxUser.lookup("nobody")
    base = Path(tempfile.mkdtemp(prefix="sandbox-probe-", dir="/tmp"))
    try:
        os.chmod(base, SHARED_DIR_MODE)
        private = base / "run"
        private.mkdir()
        (private / "run.db").write_text("hidden world", encoding="utf-8")
        secure_private_dir(private)
        public = base / "public.txt"
        public.write_text("visible", encoding="utf-8")
        os.chmod(public, SHARED_FILE_MODE)
        root = workspace.create(base / "workspace", load_run_config())
        hand_over_workspace(
            root,
            user,
            read_only=workspace.docs_path(root),
            writable=(workspace.memory_path(root),),
        )

        assert probe_readable(user, public)
        assert probe_readable(user, root)
        assert not probe_readable(user, private)
        assert not probe_readable(user, private / "run.db")
        assert (root.stat().st_uid, root.stat().st_gid) == (0, user.gid)
        assert stat.S_IMODE(root.stat().st_mode) == WORKSPACE_ROOT_MODE
        assert workspace.memory_path(root).stat().st_uid == user.uid
        docs = workspace.docs_path(root)
        assert docs.stat().st_uid == 0
        assert stat.S_IMODE(docs.stat().st_mode) == SHARED_DIR_MODE
        for path in docs.rglob("*"):
            assert path.stat().st_uid == 0
        preflight(user, workspace=root, forbidden=[private, private / "run.db"])
        with pytest.raises(SandboxError):
            preflight(user, workspace=root, forbidden=[public])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_missing_forbidden_path_refuses_the_run(tmp_path):
    present = tmp_path / "secrets"
    present.mkdir()
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")

    require_existing([present, tmp_path / "dangling"])
    with pytest.raises(SandboxError, match="scenarois do not exist"):
        require_existing([present, tmp_path / "scenarois"])


def test_default_forbidden_paths_add_the_enclosing_repository(tmp_path):
    repo = tmp_path / "repo"
    package = repo / "src" / "trades_bench"
    scenarios = repo / "tools" / "scenarios"
    package.mkdir(parents=True)
    scenarios.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    installed = tmp_path / "site-packages" / "policies"
    installed.mkdir(parents=True)

    found = default_forbidden_paths((package, scenarios, installed))

    assert found == (package.resolve(), repo.resolve(), scenarios.resolve(), installed.resolve())


def test_a_workspace_under_a_dir_the_agent_could_rewrite_is_refused(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    owned = SandboxUser(name="sunridge", uid=os.getuid(), gid=os.getgid(), home="/home/x")

    os.chmod(parent, 0o755)
    require_sealed_ancestors(parent / "workspace", USER)
    require_sealed_ancestors(parent / "missing" / "workspace", USER)
    with pytest.raises(SandboxError, match="could replace the workspace"):
        require_sealed_ancestors(parent / "workspace", owned)
    os.chmod(parent, 0o777)
    with pytest.raises(SandboxError, match="could replace the workspace"):
        require_sealed_ancestors(parent / "workspace", USER)
    os.chmod(parent, 0o1777)
    require_sealed_ancestors(parent / "workspace", USER)
    os.chmod(parent, 0o755)


def _as_user(user: SandboxUser, script: str, *args: str) -> int:
    return subprocess.run(
        ["/bin/sh", "-c", script, "sh", *args],
        cwd="/",
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        **user.process_identity(),
    ).returncode


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_the_agent_can_write_its_workspace_but_never_replace_the_docs():
    user = SandboxUser.lookup("nobody")
    base = Path(tempfile.mkdtemp(prefix="sandbox-docs-", dir="/tmp"))
    try:
        os.chmod(base, SHARED_DIR_MODE)
        root = workspace.create(base / "workspace", load_run_config())
        hand_over_workspace(
            root,
            user,
            read_only=workspace.docs_path(root),
            writable=(workspace.memory_path(root),),
        )
        docs = workspace.docs_path(root)
        before = sorted(path.name for path in docs.iterdir())

        assert _as_user(user, 'cd "$1" && echo notes > notes.txt && mkdir scripts', str(root)) == 0
        assert _as_user(user, 'echo remembered >> "$1"', str(workspace.memory_path(root))) == 0
        assert _as_user(user, 'mv "$1/MEMORY.md" "$1/m" && mv "$1/m" "$1/MEMORY.md"', str(root)) == 0
        assert _as_user(user, 'mv "$1/docs" "$1/docs.old"', str(root)) != 0
        assert _as_user(user, 'rm -rf "$1/docs"', str(root)) != 0
        assert _as_user(user, 'touch "$1/docs/planted"', str(root)) != 0
        assert _as_user(user, 'chmod 777 "$1"', str(root)) != 0
        assert (root / "notes.txt").stat().st_uid == user.uid
        assert (root / "scripts").is_dir()
        assert sorted(path.name for path in docs.iterdir()) == before
        assert docs.stat().st_uid == 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_the_reaper_kills_every_sandbox_process_and_spares_root():
    user = SandboxUser.lookup("nobody")
    detached = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "start_new_session": True}
    agent = subprocess.Popen(["/bin/sleep", "300"], **detached, **user.process_identity())
    harness = subprocess.Popen(["/bin/sleep", "300"], **detached)
    try:
        kill_user_processes(user)

        assert agent.wait(timeout=5) == -9
        assert live_processes(user.uid) == []
        assert harness.poll() is None
        kill_user_processes(user)
    finally:
        harness.kill()
        harness.wait()
        agent.kill()
        agent.wait()


def _agent_template(base: Path) -> Path:
    template = base / "template"
    (template / "themes").mkdir(parents=True)
    (template / "models.json").write_text('{"providers": {}}', encoding="utf-8")
    (template / "settings.json").write_text("{}", encoding="utf-8")
    (template / "themes" / "plain.json").write_text("{}", encoding="utf-8")
    return template


def test_the_working_agent_home_is_reset_to_its_template(tmp_path):
    template = _agent_template(tmp_path)
    home = create_agent_home(template)
    try:
        assert stat.S_IMODE(home.stat().st_mode) == AGENT_HOME_MODE
        assert stat.S_IMODE(home.parent.stat().st_mode) == TRAVERSE_ONLY_DIR_MODE
        assert stat.S_IMODE((home / "models.json").stat().st_mode) == SHARED_FILE_MODE
        (home / "auth.json.lock").mkdir()
        (home / "extensions").mkdir()
        (home / "extensions" / "evil.js").write_text("evil", encoding="utf-8")
        (home / "planted.json").symlink_to(tmp_path / "elsewhere")
        os.chmod(home / "settings.json", 0o666)
        (home / "settings.json").write_text('{"packages": ["evil"]}', encoding="utf-8")
        (home / "themes" / "plain.json").unlink()
        (home / "models.json").unlink()
        (home / "models.json").symlink_to(template / "models.json")

        reset_agent_home(template, home)

        assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == [
            "models.json",
            "settings.json",
            "themes",
            "themes/plain.json",
        ]
        assert not (home / "models.json").is_symlink()
        for name in ("models.json", "settings.json", "themes/plain.json"):
            assert (home / name).read_bytes() == (template / name).read_bytes()
            assert stat.S_IMODE((home / name).stat().st_mode) == SHARED_FILE_MODE
    finally:
        remove_agent_home(home)
    assert not home.parent.exists()


def test_a_missing_agent_template_is_refused(tmp_path):
    with pytest.raises(SandboxError, match="not a directory"):
        create_agent_home(tmp_path / "missing")


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_the_sandbox_user_can_lock_but_never_keep_changes_in_its_agent_home():
    user = SandboxUser.lookup("nobody")
    base = Path(tempfile.mkdtemp(prefix="sandbox-agent-", dir="/tmp"))
    try:
        template = _agent_template(base)
        home = create_agent_home(template)
        try:
            assert _as_user(user, 'mkdir "$1/auth.json.lock"', str(home)) == 0
            assert _as_user(user, 'echo x > "$1/models.json"', str(home)) != 0
            assert _as_user(user, 'rm -f "$1/models.json"', str(home)) != 0
            assert _as_user(user, 'mv "$1/settings.json" "$1/s"', str(home)) != 0
            assert _as_user(user, 'touch "$1/themes/planted.json"', str(home)) != 0
            assert _as_user(user, 'mv "$1" "$1.moved"', str(home)) != 0
            assert _as_user(user, 'mkdir "$1/extensions" && echo evil > "$1/x.js"', str(home)) == 0

            reset_agent_home(template, home)

            assert sorted(path.name for path in home.iterdir()) == [
                "models.json",
                "settings.json",
                "themes",
            ]
            assert (home / "models.json").read_bytes() == (template / "models.json").read_bytes()
        finally:
            remove_agent_home(home)
    finally:
        shutil.rmtree(base, ignore_errors=True)
