from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from policies import container_run
from policies.container_run import (
    ARGS_FILE,
    EXPORT_REPORT_FILENAME,
    PI_AGENT_DIR,
    RUN_DIR,
    SANDBOX_USER,
    STATE_DIR,
    SUMMARY_FILENAME,
    WORKSPACE,
    ContainerConfig,
    ContainerError,
    ExportLimits,
    backup_database,
    catalog_line,
    claim_out_dir,
    config_from_env,
    export_workspace,
    hand_over_results,
    main,
    mirror_run,
    pi_run_args,
    prepare_dirs,
    release_out_dir,
    seal_agent_template,
    try_kill,
    write_args,
)
from policies.pi_run import INTERRUPTED_EXIT_CODE, REFUSED_EXIT_CODE, InterruptGuard, parser
from trades_bench.harness.sandbox import SandboxError, SandboxUser
from trades_bench.harness.workspace import copy_regular_file

ROOT_ON_LINUX = sys.platform.startswith("linux") and os.geteuid() == 0

BASE_ENV = {
    "TB_PROVIDER": "openai",
    "TB_MODEL": "gpt-6-astra",
    "TB_THINKING": "high",
    "OPENAI_API_KEY": "sk-test",
}


def _config(**overrides: object) -> ContainerConfig:
    return config_from_env({**BASE_ENV, **{key: str(value) for key, value in overrides.items()}})


def test_the_config_defaults_seed_scenario_label_and_results():
    config = _config()

    assert config == ContainerConfig(
        provider="openai",
        model="gpt-6-astra",
        thinking="high",
        seed=5,
        scenario="oct_default",
        label="gpt-6-astra-high-a1",
        results=STATE_DIR / "results",
        executable="pi",
        test_executable=False,
    )
    assert config.out_dir == STATE_DIR / "results" / "gpt-6-astra-high-a1"


def test_the_config_reads_every_override():
    config = _config(
        TB_SEED=11,
        TB_SCENARIO="july_monsoon",
        TB_LABEL="test-astra-a2",
        TB_RESULTS="/out",
        TB_PI_EXECUTABLE="/opt/fake/pi",
    )

    assert (config.seed, config.scenario, config.label) == (11, "july_monsoon", "test-astra-a2")
    assert (config.results, config.executable) == (Path("/out"), "/opt/fake/pi")
    assert config.test_executable


@pytest.mark.parametrize("label", [None, "astra-a2", "a-test-run"])
def test_a_test_executable_needs_a_test_label(label):
    overrides = {"TB_PI_EXECUTABLE": "/opt/fake/pi"}
    if label is not None:
        overrides["TB_LABEL"] = label

    with pytest.raises(ContainerError, match="TB_PI_EXECUTABLE"):
        _config(**overrides)


@pytest.mark.parametrize("name", ["TB_PROVIDER", "TB_MODEL", "TB_THINKING"])
def test_the_agent_choice_is_required(name):
    env = {key: value for key, value in BASE_ENV.items() if key != name}

    with pytest.raises(ContainerError, match=name):
        config_from_env(env)


@pytest.mark.parametrize(
    ("provider", "key"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GEMINI_API_KEY"),
        ("xai", "XAI_API_KEY"),
        ("meta", "META_API_KEY"),
    ],
)
def test_each_provider_needs_its_pi_key_env(provider, key):
    env = {**BASE_ENV, "TB_PROVIDER": provider}

    if key != "OPENAI_API_KEY":
        with pytest.raises(ContainerError, match=key):
            config_from_env(env)
    assert config_from_env({**env, key: "secret"}).key_env == key


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"TB_PROVIDER": "openai-codex"}, "TB_PROVIDER"),
        ({"TB_SEED": "five"}, "TB_SEED"),
        ({"TB_SCENARIO": "../oct_default"}, "TB_SCENARIO"),
        ({"TB_SCENARIO": "missing"}, "TB_SCENARIO"),
        ({"TB_LABEL": "../escape"}, "TB_LABEL"),
        ({"TB_LABEL": ".."}, "TB_LABEL"),
    ],
)
def test_unsafe_or_unknown_settings_are_refused(overrides, message):
    with pytest.raises(ContainerError, match=message):
        config_from_env({**BASE_ENV, **overrides})


def test_a_default_label_never_leaves_the_results_dir():
    assert _config(TB_MODEL="org/model:v1").label == "org_model_v1-high-a1"


def test_the_args_reproduce_the_isolated_pi_run_invocation():
    config = _config(TB_SEED=7, TB_RESULTS="/out")

    args = parser().parse_args(pi_run_args(config))

    assert (args.run_dir, args.workspace) == (RUN_DIR, WORKSPACE)
    assert (args.seed, args.scenario) == (7, "oct_default")
    assert (args.provider, args.model, args.thinking) == ("openai", "gpt-6-astra", "high")
    assert (args.sandbox_user, args.pi_agent_dir) == (SANDBOX_USER, PI_AGENT_DIR)
    assert args.offline and args.pi_env == ["OPENAI_API_KEY"]
    assert args.forbid_path == [Path("/repo"), Path("/root"), Path("/out")]
    assert args.executable == "pi"


def test_a_test_executable_is_forwarded_to_pi_run():
    args = parser().parse_args(
        pi_run_args(_config(TB_PI_EXECUTABLE="/opt/fake/pi", TB_LABEL="test-fake"))
    )

    assert args.executable == "/opt/fake/pi"


def test_the_args_file_is_private_one_token_per_line_and_replaced(tmp_path):
    path = tmp_path / ARGS_FILE.name
    path.write_text("stale")
    tokens = pi_run_args(_config())

    write_args(tokens, path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().splitlines() == tokens
    assert parser().parse_args([f"@{path}"]).model == "gpt-6-astra"


def test_the_catalog_line_matches_provider_and_model_exactly():
    listing = (
        "provider  model             context\n"
        "openai    gpt-6-astra-mini  400K\n"
        "openai    gpt-6-astra       400K\n"
    )

    assert catalog_line(listing, "openai", "gpt-6-astra") == "openai    gpt-6-astra       400K"
    with pytest.raises(ContainerError, match="openai/gpt-6"):
        catalog_line(listing, "openai", "gpt-6")


def test_the_agent_template_is_sealed_with_settings(tmp_path):
    template = tmp_path / "pi-agent"
    (template / "cache").mkdir(parents=True)
    (template / "models.json").write_text("{}")
    (template / "models.json").chmod(0o600)
    (template / "cache").chmod(0o700)
    outside = tmp_path / "outside"
    outside.write_text("x")
    outside.chmod(0o600)
    (template / "link").symlink_to(outside)

    seal_agent_template(template)

    assert json.loads((template / "settings.json").read_text()) == {}
    assert stat.S_IMODE(template.stat().st_mode) == 0o700
    assert stat.S_IMODE((template / "cache").stat().st_mode) == 0o755
    assert stat.S_IMODE((template / "models.json").stat().st_mode) == 0o644
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600


def test_existing_results_under_the_label_are_never_overwritten(tmp_path):
    assert claim_out_dir(tmp_path / "fresh") is True
    assert (tmp_path / "fresh").is_dir()
    (tmp_path / "empty").mkdir()
    assert claim_out_dir(tmp_path / "empty") is False
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "policy-report.json").write_text("{}")
    (tmp_path / "linked").symlink_to(tmp_path / "empty")
    (tmp_path / "file").write_text("x")

    for name in ("used", "linked", "file"):
        with pytest.raises(ContainerError, match="TB_LABEL"):
            claim_out_dir(tmp_path / name)
    assert (tmp_path / "used" / "policy-report.json").read_text() == "{}"


def test_a_label_claimed_by_a_concurrent_run_is_refused(tmp_path):
    assert claim_out_dir(tmp_path / "fresh") is True
    (tmp_path / "empty").mkdir()
    assert claim_out_dir(tmp_path / "empty") is False

    for name in ("fresh", "empty"):
        with pytest.raises(ContainerError, match="TB_LABEL"):
            claim_out_dir(tmp_path / name)
        assert [path.name for path in (tmp_path / name).iterdir()] == [SUMMARY_FILENAME]


def test_releasing_a_label_removes_only_an_empty_folder(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "run.db").write_text("x")

    (tmp_path / "kept").mkdir()
    for name in ("empty", "kept"):
        (tmp_path / name / SUMMARY_FILENAME).touch()

    release_out_dir(tmp_path / "empty", created=True)
    release_out_dir(tmp_path / "kept", created=False)
    release_out_dir(tmp_path / "used", created=True)
    release_out_dir(tmp_path / "absent", created=True)

    assert not (tmp_path / "empty").exists()
    assert (tmp_path / "kept").is_dir() and not any((tmp_path / "kept").iterdir())
    assert (tmp_path / "used" / "run.db").exists()


def test_preparing_dirs_leaves_the_results_mount_mode_alone(tmp_path, monkeypatch):
    for name in ("STATE_DIR", "REPO_ROOT", "ROOT_HOME"):
        (tmp_path / name).mkdir()
        monkeypatch.setattr(container_run, name, tmp_path / name)
    monkeypatch.setattr(container_run, "WORKSPACE_PARENT", tmp_path / "srv" / "sunridge")
    monkeypatch.setattr(container_run.os, "chown", lambda *a, **k: None)
    results = tmp_path / "results"
    results.mkdir(mode=0o755)
    results.chmod(0o755)

    prepare_dirs(_config(TB_RESULTS=results))

    assert stat.S_IMODE(results.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "STATE_DIR").stat().st_mode) == 0o700
    assert not (results / "gpt-6-astra-high-a1").exists()


def _database(path: Path, rows: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("create table if not exists ledger (amount integer)")
        connection.executemany("insert into ledger values (?)", [(n,) for n in range(rows)])
    connection.close()


def _rows(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("select count(*) from ledger").fetchone()[0]
    finally:
        connection.close()


def test_a_periodic_mirror_copies_the_logs_but_never_touches_the_live_database(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "pi-logs").mkdir(parents=True)
    (run_dir / "pi-logs" / "turn.json").write_text("{}")
    _database(run_dir / "run.db", 3)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    writer = sqlite3.connect(run_dir / "run.db", isolation_level=None, timeout=0)
    try:
        writer.execute("begin exclusive")
        mirror_run(run_dir, out_dir)
        writer.execute("insert into ledger values (1)")
        writer.execute("commit")
    finally:
        writer.close()

    assert (out_dir / "pi-logs" / "turn.json").read_text() == "{}"
    assert sorted(path.name for path in out_dir.iterdir()) == ["pi-logs"]


def test_the_final_mirror_backs_up_the_database(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.db-journal").write_text("stale")
    _database(run_dir / "run.db", 3)
    _database(run_dir / "run.db", 2)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    mirror_run(run_dir, out_dir, final=True)

    assert sorted(path.name for path in out_dir.iterdir()) == ["run.db"]
    assert _rows(out_dir / "run.db") == 5


def test_a_missing_run_dir_mirrors_nothing(tmp_path):
    mirror_run(tmp_path / "absent", tmp_path / "out")

    assert not (tmp_path / "out").exists()


def _hot_journal_database(path: Path, rows: int) -> None:
    live = path.parent / "live" / path.name
    live.parent.mkdir()
    _database(live, rows)
    connection = sqlite3.connect(live, isolation_level=None)
    try:
        connection.execute("pragma cache_size=1")
        connection.execute("begin immediate")
        connection.executemany("insert into ledger values (?)", [(n,) for n in range(20_000)])
        shutil.copyfile(live, path)
        shutil.copyfile(f"{live}-journal", f"{path}-journal")
        connection.execute("rollback")
    finally:
        connection.close()


def test_the_final_backup_rolls_back_a_hot_journal(tmp_path):
    source = tmp_path / "run.db"
    _hot_journal_database(source, 3)

    backup_database(source, tmp_path / "final.db")

    assert _rows(tmp_path / "final.db") == 3
    assert not Path(f"{source}-journal").exists()


def test_the_final_mirror_recovers_a_hot_journal(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _hot_journal_database(run_dir / "run.db", 4)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    assert container_run.try_mirror(run_dir, out_dir) is None
    assert not (out_dir / "run.db").exists()
    assert container_run.try_mirror(run_dir, out_dir, final=True) is None

    assert _rows(out_dir / "run.db") == 4


def test_a_backup_replaces_the_target_atomically(tmp_path):
    _database(tmp_path / "source.db", 4)
    (tmp_path / "copy.db").write_text("stale")
    (tmp_path / "copy.db.partial").write_text("leftover")

    backup_database(tmp_path / "source.db", tmp_path / "copy.db")

    assert _rows(tmp_path / "copy.db") == 4
    assert not (tmp_path / "copy.db.partial").exists()


def test_the_workspace_export_copies_only_bounded_regular_files(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "docs" / "nested").mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("notes")
    (workspace / "docs" / "nested" / "guide.md").write_text("guide")
    (workspace / "big.bin").write_bytes(b"x" * 11)
    secret = tmp_path / "score.json"
    secret.write_text("secret")
    (workspace / "link.json").symlink_to(secret)
    (workspace / "linked-dir").symlink_to(tmp_path)
    os.mkfifo(workspace / "pipe")
    target = tmp_path / "out" / "workspace"
    target.parent.mkdir()

    report = export_workspace(workspace, target, ExportLimits(file_bytes=10))

    assert report["skipped"] == [
        {"path": "linked-dir", "reason": "symlink"},
        {"path": "big.bin", "reason": "file_too_large"},
        {"path": "link.json", "reason": "symlink"},
        {"path": "pipe", "reason": "not_regular_file"},
    ]
    assert (report["exported_files"], report["exported_bytes"]) == (2, 10)
    assert report["truncated"] is False
    assert json.loads((tmp_path / "out" / EXPORT_REPORT_FILENAME).read_text()) == report
    assert (target / "MEMORY.md").read_text() == "notes"
    assert (target / "docs" / "nested" / "guide.md").read_text() == "guide"
    assert sorted(path.name for path in target.iterdir()) == ["MEMORY.md", "docs"]


def test_a_workspace_path_too_long_to_export_is_skipped_not_fatal(tmp_path):
    limit = os.pathconf(tmp_path, "PC_PATH_MAX")
    workspace = tmp_path / "w"
    target = tmp_path / ("o" * 120) / "workspace"
    target.parent.mkdir()
    deep = workspace
    while len(str(deep)) < limit - 60:
        deep = deep / ("d" * min(100, limit - 60 - len(str(deep))))
    deep.mkdir(parents=True)
    (deep / "x").write_text("deep")
    (workspace / "MEMORY.md").write_text("notes")

    skipped = export_workspace(workspace, target)["skipped"]

    assert (target / "MEMORY.md").read_text() == "notes"
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "copy_failed"
    assert (workspace / skipped[0]["path"]).is_dir()


def test_a_file_that_cannot_be_written_is_skipped_without_a_partial_copy(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("notes")
    (workspace / "full.bin").write_text("data")
    target = tmp_path / "out" / "workspace"
    target.parent.mkdir()

    def failing_copy(source, destination, *, max_bytes=None):
        if Path(source).name == "full.bin":
            Path(destination).write_text("par")
            raise OSError(28, "No space left on device")
        return copy_regular_file(source, destination, max_bytes=max_bytes)

    monkeypatch.setattr(container_run, "copy_regular_file", failing_copy)

    assert export_workspace(workspace, target)["skipped"] == [
        {"path": "full.bin", "reason": "copy_failed"}
    ]
    assert sorted(path.name for path in target.iterdir()) == ["MEMORY.md"]


def test_the_workspace_export_stops_at_the_total_size_budget(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, size in (("a.bin", 6), ("b.bin", 6), ("c.bin", 4)):
        (workspace / name).write_bytes(b"x" * size)
    target = tmp_path / "out" / "workspace"
    target.parent.mkdir()

    report = export_workspace(workspace, target, ExportLimits(file_bytes=10, total_bytes=10))

    assert report["skipped"] == [{"path": "b.bin", "reason": "total_size_limit"}]
    assert (report["exported_files"], report["exported_bytes"]) == (2, 10)
    assert sorted(path.name for path in target.iterdir()) == ["a.bin", "c.bin"]


def test_the_workspace_export_is_truncated_at_the_entry_limit(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "notes").mkdir(parents=True)
    for index in range(5):
        (workspace / "notes" / f"{index}.md").write_text("n")
    target = tmp_path / "out" / "workspace"
    target.parent.mkdir()

    report = export_workspace(workspace, target, ExportLimits(entries=3))

    assert report["truncated"] is True
    assert report["exported_files"] == 2
    assert report["skipped"] == [{"path": "notes/2.md", "reason": "entry_limit"}]
    assert sorted(path.name for path in (target / "notes").iterdir()) == ["0.md", "1.md"]


def test_surviving_sandbox_processes_are_reported_not_raised(monkeypatch, capsys):
    def survive(user):
        raise SandboxError("processes of sunridge survived 50 kills")

    monkeypatch.setattr(container_run, "kill_user_processes", survive)

    assert try_kill(SandboxUser("sunridge", 10001, 10001, "/home/sunridge")) == (
        "processes of sunridge survived 50 kills"
    )
    assert "survived" in capsys.readouterr().err


def test_the_workspace_export_refuses_an_existing_target(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "out" / "workspace").mkdir(parents=True)

    with pytest.raises(FileExistsError):
        export_workspace(tmp_path / "workspace", tmp_path / "out" / "workspace")


def test_a_capped_copy_skips_oversized_files(tmp_path):
    (tmp_path / "small").write_bytes(b"abc")

    assert copy_regular_file(tmp_path / "small", tmp_path / "a", max_bytes=3)
    assert not copy_regular_file(tmp_path / "small", tmp_path / "b", max_bytes=2)
    assert not (tmp_path / "b").exists()


def test_main_refuses_a_bad_configuration_without_touching_anything(capsys):
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    assert main({"TB_PROVIDER": "openai"}) == REFUSED_EXIT_CODE

    assert "TB_MODEL" in capsys.readouterr().err
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root refusal")
def test_main_refuses_to_run_without_root(capsys):
    assert main(BASE_ENV) == REFUSED_EXIT_CODE

    assert "root" in capsys.readouterr().err


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to change owners")
def test_results_are_handed_to_the_mount_owner_without_following_links():
    base = Path(tempfile.mkdtemp(prefix="container-results-", dir="/tmp"))
    try:
        out_dir = base / "label"
        (out_dir / "pi-logs").mkdir(parents=True)
        (out_dir / "pi-logs" / "turn.json").write_text("{}")
        outside = base / "outside"
        outside.write_text("root only")
        (out_dir / "link").symlink_to(outside)

        hand_over_results(out_dir, 4321, 4321)

        for path in (out_dir, out_dir / "pi-logs", out_dir / "pi-logs" / "turn.json"):
            assert (path.stat().st_uid, path.stat().st_gid) == (4321, 4321)
        assert (out_dir / "link").lstat().st_uid == 4321
        assert outside.stat().st_uid == 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _FakePiRun:
    def __init__(self, harness: _Harness, *, interrupt: bool, cleanup_code: int | None) -> None:
        self.harness = harness
        self.interrupt = interrupt
        self.cleanup_code = cleanup_code
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.harness.calls.append(f"wait({timeout:g})")
            if self.cleanup_code is None:
                raise subprocess.TimeoutExpired("pi_run", timeout)
            return self.cleanup_code
        self.harness.calls.append("wait")
        if self.killed:
            return -signal.SIGKILL
        self.harness.write_run()
        if self.interrupt:
            os.kill(os.getpid(), signal.SIGTERM)
        return 0

    def send_signal(self, sig: int) -> None:
        self.harness.calls.append(f"signal({signal.Signals(sig).name})")

    def kill(self) -> None:
        self.harness.calls.append("kill_pi_run")
        self.killed = True


class _Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.run_dir = tmp_path / "state" / "run"
        self.workspace = tmp_path / "srv" / "workspace"
        self.results = tmp_path / "results"
        self.interrupt = False
        self.cleanup_code: int | None = INTERRUPTED_EXIT_CODE
        self.signal_during_kill = False
        self.kill_error: Exception | None = None
        self.export_error: Exception | None = None
        self.prepare_error: Exception | None = None
        self.config = _config(TB_RESULTS=self.results)
        sandbox = SandboxUser(SANDBOX_USER, 10001, 10001, "/home/sunridge")
        monkeypatch.setattr(container_run, "RUN_DIR", self.run_dir)
        monkeypatch.setattr(container_run, "WORKSPACE", self.workspace)
        monkeypatch.setattr(container_run, "ARGS_FILE", tmp_path / "pi-run.args")
        monkeypatch.setattr(container_run, "require_root", self._record("require_root", sandbox))
        monkeypatch.setattr(container_run, "prepare_dirs", self._prepare_dirs)
        monkeypatch.setattr(container_run, "prepare_pi", self._prepare_pi)
        monkeypatch.setattr(container_run, "pi_version", self._record("pi_version", "1.4.0"))
        monkeypatch.setattr(container_run, "start_pi_run", self._start)
        monkeypatch.setattr(container_run, "kill_user_processes", self._kill)
        monkeypatch.setattr(container_run, "end_process_group", self._end_group)
        monkeypatch.setattr(container_run, "hand_over_results", self._hand_over)
        mirror, export = container_run.try_mirror, container_run.export_workspace
        monkeypatch.setattr(container_run, "try_mirror", self._wrap("mirror", mirror))
        monkeypatch.setattr(container_run, "export_workspace", self._export(export))

    def _record(self, name: str, result: object):
        def recorded(*args: object, **kwargs: object) -> object:
            self.calls.append(name)
            return result

        return recorded

    def _wrap(self, name: str, function):
        def wrapped(*args: object, **kwargs: object) -> object:
            self.calls.append(f"{name}{kwargs or ''}")
            return function(*args, **kwargs)

        return wrapped

    def _export(self, function):
        def export(*args: object, **kwargs: object) -> object:
            self.calls.append("export")
            if self.export_error is not None:
                raise self.export_error
            return function(*args, **kwargs)

        return export

    def _prepare_dirs(self, config: ContainerConfig) -> None:
        self.calls.append("prepare_dirs")
        config.results.mkdir(parents=True, exist_ok=True)

    def _prepare_pi(self, config: ContainerConfig, environment: object) -> str:
        self.calls.append("prepare_pi")
        if self.prepare_error is not None:
            raise self.prepare_error
        return "openai    gpt-6-astra       400K"

    def _start(self, environment: dict[str, str]) -> _FakePiRun:
        self.calls.append("start_pi_run")
        assert environment["OPENAI_API_KEY"] == "sk-test"
        return _FakePiRun(self, interrupt=self.interrupt, cleanup_code=self.cleanup_code)

    def _kill(self, user: SandboxUser) -> None:
        self.calls.append("kill_sandbox")
        if self.signal_during_kill:
            os.kill(os.getpid(), signal.SIGTERM)
        if self.kill_error is not None:
            raise self.kill_error

    def _end_group(self, process: _FakePiRun, timeout: float) -> str | None:
        self.calls.append(f"end_group({timeout:g})")
        return None

    def _hand_over(self, out_dir: Path, uid: int, gid: int) -> None:
        self.calls.append("hand_over")
        assert (out_dir / SUMMARY_FILENAME).is_file()

    def write_run(self) -> None:
        (self.run_dir / "pi-logs").mkdir(parents=True)
        (self.run_dir / "pi-logs" / "turn.json").write_text("{}")
        _database(self.run_dir / "run.db", 3)
        self.workspace.mkdir(parents=True)
        (self.workspace / "MEMORY.md").write_text("notes")

    def run(self) -> int:
        with InterruptGuard() as guard:
            return container_run.run(self.config, BASE_ENV, guard)

    @property
    def out_dir(self) -> Path:
        return self.config.out_dir

    def summary(self) -> dict:
        return json.loads((self.out_dir / SUMMARY_FILENAME).read_text())

    def handed_over(self) -> list[str]:
        return ["hand_over"] if self.results.stat().st_uid != 0 else []


@pytest.fixture
def harness(tmp_path, monkeypatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch)


SETUP_CALLS = ["require_root", "prepare_dirs", "prepare_pi", "pi_version", "start_pi_run"]
FINAL_MIRROR = "mirror{'final': True}"
END_GROUP = f"end_group({container_run.PI_RUN_GROUP_EXIT_SECONDS})"


def test_a_run_mirrors_kills_exports_and_summarises_in_order(harness):
    assert harness.run() == 0

    assert harness.calls == [
        *SETUP_CALLS,
        "wait",
        END_GROUP,
        FINAL_MIRROR,
        "kill_sandbox",
        "export",
        *harness.handed_over(),
    ]
    summary = harness.summary()
    assert summary["returncode"] == 0
    assert summary["interrupted"] is False
    assert (summary["pi_executable"], summary["test_executable"]) == ("pi", False)
    assert summary["catalog_line"] == "openai    gpt-6-astra       400K"
    assert summary["workspace_export"]["exported_files"] == 1
    assert (summary["final_mirror_error"], summary["kill_error"]) == (None, None)
    assert summary["pi_run_group_error"] is None
    assert summary["workspace_export_error"] is None
    assert _rows(harness.out_dir / "run.db") == 3
    assert (harness.out_dir / "workspace" / "MEMORY.md").read_text() == "notes"
    assert parser().parse_args([f"@{container_run.ARGS_FILE}"]).run_dir == harness.run_dir


def test_surviving_sandbox_processes_skip_the_export_but_not_the_summary(harness):
    harness.kill_error = SandboxError("processes of sunridge survived 50 kills")

    assert harness.run() == 0

    assert harness.calls == [
        *SETUP_CALLS, "wait", END_GROUP, FINAL_MIRROR, "kill_sandbox", *harness.handed_over()
    ]
    summary = harness.summary()
    assert summary["kill_error"] == "processes of sunridge survived 50 kills"
    assert summary["workspace_export"] is None
    assert "survived" in summary["workspace_export_error"]
    assert not (harness.out_dir / "workspace").exists()


def test_a_failed_export_is_recorded_in_the_summary(harness):
    harness.export_error = OSError(28, "No space left on device")

    assert harness.run() == 0

    summary = harness.summary()
    assert summary["workspace_export"] is None
    assert "No space left" in summary["workspace_export_error"]
    assert summary["kill_error"] is None


def test_an_interrupt_waits_for_pi_run_cleanup_and_ignores_further_signals(harness):
    harness.interrupt = True
    harness.signal_during_kill = True
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    assert harness.run() == INTERRUPTED_EXIT_CODE

    assert harness.calls == [
        *SETUP_CALLS,
        "wait",
        "signal(SIGTERM)",
        f"wait({container_run.PI_RUN_GRACE_SECONDS})",
        END_GROUP,
        FINAL_MIRROR,
        "kill_sandbox",
        "export",
        *harness.handed_over(),
    ]
    summary = harness.summary()
    assert (summary["returncode"], summary["interrupted"]) == (INTERRUPTED_EXIT_CODE, True)
    assert _rows(harness.out_dir / "run.db") == 3
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


def test_a_pi_run_that_overruns_its_cleanup_grace_is_killed(harness):
    harness.interrupt = True
    harness.cleanup_code = None

    assert harness.run() == INTERRUPTED_EXIT_CODE

    assert harness.calls[len(SETUP_CALLS):len(SETUP_CALLS) + 7] == [
        "wait",
        "signal(SIGTERM)",
        f"wait({container_run.PI_RUN_GRACE_SECONDS})",
        "kill_pi_run",
        "wait",
        END_GROUP,
        FINAL_MIRROR,
    ]
    assert harness.summary()["returncode"] == -signal.SIGKILL
    assert harness.summary()["interrupted"] is True


def test_a_setup_failure_removes_the_label_folder_it_created(harness):
    harness.prepare_error = ContainerError("pi does not list the model openai/gpt-6-astra")

    with pytest.raises(ContainerError):
        harness.run()

    assert harness.calls == ["require_root", "prepare_dirs", "prepare_pi"]
    assert harness.results.is_dir()
    assert not harness.out_dir.exists()


def test_a_setup_failure_keeps_an_empty_label_folder_it_did_not_create(harness):
    harness.out_dir.mkdir(parents=True)
    harness.prepare_error = ContainerError("pi exited 1")

    with pytest.raises(ContainerError):
        harness.run()

    assert harness.out_dir.is_dir()
    assert not any(harness.out_dir.iterdir())


def test_a_used_label_is_refused_before_pi_is_prepared(harness):
    harness.out_dir.mkdir(parents=True)
    (harness.out_dir / "policy-report.json").write_text("{}")

    with pytest.raises(ContainerError, match="TB_LABEL"):
        harness.run()

    assert harness.calls == ["require_root", "prepare_dirs"]
    assert (harness.out_dir / "policy-report.json").read_text() == "{}"


def test_main_reports_a_setup_failure_as_a_refusal(harness, monkeypatch, capsys):
    harness.prepare_error = ContainerError("pi exited 1")

    code = main({**BASE_ENV, "TB_RESULTS": str(harness.results)})

    assert code == REFUSED_EXIT_CODE
    assert "pi exited 1" in capsys.readouterr().err
    assert not harness.out_dir.exists()


CLEANUP_CHILD = """
import signal, sys, time
def cleanup(signum, frame):
    open(sys.argv[1], "w").write("cleaned")
    raise SystemExit(130)
signal.signal(signal.SIGTERM, cleanup)
open(sys.argv[1], "w").write("ready")
time.sleep(60)
"""

STUBBORN_CHILD = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "w").write("ready")
time.sleep(60)
"""


def _child(tmp_path: Path, source: str) -> tuple[subprocess.Popen, Path]:
    marker = tmp_path / "marker"
    process = subprocess.Popen([sys.executable, "-c", source, str(marker)])
    deadline = time.monotonic() + 10
    while not (marker.exists() and marker.read_text() == "ready"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return process, marker


def test_finishing_pi_run_lets_it_clean_up(tmp_path):
    process, marker = _child(tmp_path, CLEANUP_CHILD)

    assert container_run.finish_pi_run(process, 10) == INTERRUPTED_EXIT_CODE
    assert marker.read_text() == "cleaned"


def test_finishing_pi_run_kills_it_after_the_grace_period(tmp_path):
    process, _marker = _child(tmp_path, STUBBORN_CHILD)

    assert container_run.finish_pi_run(process, 0.5) == -signal.SIGKILL


ORPHANING_CHILD = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
engine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(engine.pid))
os.rename(sys.argv[1], sys.argv[1] + ".ready")
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_killed_pi_run_takes_its_engine_with_it(tmp_path):
    marker = tmp_path / "engine"
    process = subprocess.Popen(
        [sys.executable, "-c", ORPHANING_CHILD, str(marker)], start_new_session=True
    )
    ready = tmp_path / "engine.ready"
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    engine = int(ready.read_text())

    assert container_run.finish_pi_run(process, 0.5) == -signal.SIGKILL
    assert _alive(engine)
    assert container_run.end_process_group(process, 10) is None

    deadline = time.monotonic() + 10
    while _alive(engine):
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_pi_run_starts_in_its_own_process_group(monkeypatch):
    started: dict[str, object] = {}
    monkeypatch.setattr(
        container_run.subprocess, "Popen", lambda argv, **options: started.update(options)
    )

    container_run.start_pi_run({"PATH": "/usr/bin"})

    assert started["start_new_session"] is True
