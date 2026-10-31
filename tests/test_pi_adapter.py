from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pwd
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from policies.pi_run import CATALOG_CHANGED_FLAG, TIMEOUT_FLAG, agent_label, usage_summary
from trades_bench.adapter import AdapterIntegrityError
from trades_bench.harness import prompts, workspace
from trades_bench.harness.pi_adapter import (
    PI_AGENT_DIR_ENV,
    PI_OFFLINE_ENV,
    PRIVATE_OUTPUT_MODE,
    PiAdapter,
    PiAdapterError,
    PiConfig,
)
from trades_bench.harness.sandbox import (
    SHARED_DIR_MODE,
    SandboxError,
    SandboxUser,
    create_session_staging,
    hand_over_workspace,
    kill_user_processes,
    probe_readable,
    remove_session_staging,
    secure_private_dir,
)

SERVICE_DATE = date(2026, 10, 1)
NEXT_DATE = date(2026, 10, 2)
FAKE_VERSION = "1.4.0-fake"

FAKE_PI = r'''
import json, os, sys, time

args = sys.argv[1:]
if args == ["--version"]:
    print("__VERSION__")
    raise SystemExit(0)
if os.environ.get("FAKE_PI_FAIL"):
    raise SystemExit(3)

def value(flag):
    return args[args.index(flag) + 1] if flag in args else None

session_id = value("--session-id")
session_dir = value("--session-dir")
requested = value("--thinking") or "off"
applied = os.environ.get("FAKE_PI_APPLIED_THINKING", requested)
preamble = os.environ.get("FAKE_PI_PREAMBLE", value("--system-prompt"))
prompt = sys.stdin.read()
cwd = os.getcwd()
os.makedirs(session_dir, exist_ok=True)
existing = [
    name for name in sorted(os.listdir(session_dir))
    if name.endswith("_" + session_id + ".jsonl")
    and os.path.isfile(os.path.join(session_dir, name))
    and not os.path.islink(os.path.join(session_dir, name))
]
path = os.path.join(session_dir, existing[0] if existing else "2026-10-30T07-26-15-867Z_" + session_id + ".jsonl")
system = {"role": "system", "content": "", "sections": {
    "preamble": preamble, "cwd": "<cwd>\n" + cwd + "\n</cwd>"}}
opening = [] if existing or os.environ.get("FAKE_PI_NO_SYSTEM") else [system]
if os.environ.get("FAKE_PI_SYSTEM_PATCH"):
    sections = json.loads(os.environ["FAKE_PI_SYSTEM_PATCH"])
    opening.append({"role": "system", "content": "", "sections": sections})
with open(path, "a", encoding="utf-8") as handle:
    if not existing:
        for entry in (
            {"type": "session", "version": 3, "id": session_id, "cwd": cwd},
            {"type": "model_change", "provider": "openai", "modelId": "gpt-test"},
            {"type": "thinking_level_change", "thinkingLevel": applied},
            {"type": "message", "message": system},
        ):
            handle.write(json.dumps(entry) + "\n")
    handle.write(json.dumps({"type": "message", "message": {"role": "user", "content": prompt}}) + "\n")
if os.environ.get("FAKE_PI_BACKGROUND"):
    import subprocess
    child = subprocess.Popen(
        ["/bin/sleep", "300"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with open(os.environ["FAKE_PI_BACKGROUND"], "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))
time.sleep(float(os.environ.get("FAKE_PI_SLEEP", "0")))
usage = {"input": 970, "output": 36, "cacheRead": 512, "cacheWrite": 64, "reasoning": 12,
         "totalTokens": 1594, "cost": {"input": 0.0097, "output": 0.0018, "cacheRead": 0.0005,
                                       "cacheWrite": 0, "total": 0.012}}
assistant = {"role": "assistant", "content": [{"type": "text", "text": "done"}],
             "usage": usage, "stopReason": "stop", "thinkingLevel": applied}
for event in (
    {"type": "session", "version": 3, "id": session_id, "cwd": cwd},
    {"type": "agent_start"},
    *({"type": "message_end", "message": message} for message in opening),
    {"type": "message_end", "message": {"role": "user", "content": prompt}},
    {"type": "message_end", "message": assistant},
    {"type": "agent_end", "messages": [*opening, {"role": "user", "content": prompt}, assistant]},
):
    print(json.dumps(event), flush=True)
if os.environ.get("FAKE_PI_FORGE_STDOUT"):
    forged = {"role": "assistant", "content": "forged", "stopReason": "stop"}
    try:
        with open("/proc/%d/fd/1" % os.getpid(), "a", encoding="utf-8") as reopened:
            reopened.write(json.dumps({"type": "agent_end", "messages": [forged]}) + "\n")
    except OSError:
        pass
'''


def _fake_pi(tmp_path: Path) -> str:
    script = tmp_path / "bin" / "pi"
    script.parent.mkdir(parents=True, exist_ok=True)
    body = FAKE_PI.replace("__VERSION__", FAKE_VERSION)
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _agent_env(workspace: Path) -> dict[str, str]:
    return {
        "SUNRIDGE_API_PORT": "8123",
        "SUNRIDGE_API_TOKEN": "agent-token",
        "SUNRIDGE_WORKSPACE": str(workspace),
    }


def _adapter(
    tmp_path: Path,
    *,
    thinking: str = "high",
    extra_env: dict[str, str] | None = None,
    staged: bool = True,
    **overrides: object,
) -> PiAdapter:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config = PiConfig(
        provider="openai",
        model="gpt-test",
        thinking=thinking,
        executable=_fake_pi(tmp_path),
        extra_env=dict(extra_env or {}),
        **overrides,
    )
    archive = tmp_path / "run" / "pi-sessions"
    adapter = PiAdapter(
        session_dir=tmp_path / "staging" if staged else archive,
        log_dir=tmp_path / "run" / "pi-logs",
        config=config,
        archive_dir=archive if staged else None,
    )
    adapter.start_run(workspace, prompts.SYSTEM_PROMPT, _agent_env(workspace))
    return adapter


def _expected_system_sha256(workspace: Path) -> str:
    cwd = os.path.realpath(workspace)
    text = f"{prompts.SYSTEM_PROMPT}\n\n<cwd>\n{cwd}\n</cwd>"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_turns_record_cache_reasoning_and_cost_alongside_input_and_output(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)

    first = adapter.send("[opening]", "opening", 30.0)
    adapter.send("[closing]", "closing", 30.0)

    assert first.ended_normally
    assert (first.input_tokens, first.output_tokens) == (970, 36)
    record = adapter.turns[0]
    assert record.cache_read_tokens == 512
    assert record.cache_write_tokens == 64
    assert record.reasoning_tokens == 12
    assert record.cost_total == pytest.approx(0.012)
    assert record.thinking_level == "high"
    written = json.loads(
        (tmp_path / "run" / "pi-logs" / "2026-10-01-turn-0001-opening.turn.json").read_text()
    )
    assert written["cache_read_tokens"] == 512
    assert written["cache_write_tokens"] == 64
    assert written["reasoning_tokens"] == 12
    assert written["thinking_level"] == "high"
    assert "<system-prompt>" in written["argv"]

    adapter.end_day(SERVICE_DATE)
    usage = usage_summary(adapter, workspace_dir=tmp_path / "workspace")
    assert usage["input_tokens"] == 1940
    assert usage["output_tokens"] == 72
    assert usage["cache_read_tokens"] == 1024
    assert usage["cache_write_tokens"] == 128
    assert usage["reasoning_tokens"] == 24
    assert usage["cost_total"] == pytest.approx(0.024)
    assert usage["thinking_requested"] == "high"
    assert usage["thinking_applied"] == ["high"]
    assert usage["pi_version"] == FAKE_VERSION
    assert usage["agent"] == "pi:openai/gpt-test@high"
    assert usage["flags"] == []
    assert usage["timed_out_turns"] == 0


def test_a_clamped_thinking_level_fails_the_turn(tmp_path):
    adapter = _adapter(tmp_path, thinking="xhigh", extra_env={"FAKE_PI_APPLIED_THINKING": "high"})
    adapter.start_day(SERVICE_DATE)

    with pytest.raises(AdapterIntegrityError, match="thinking level high"):
        adapter.send("[opening]", "opening", 30.0)

    assert adapter.turns[0].thinking_level == "high"


def test_a_system_preamble_that_differs_fails_the_turn(tmp_path):
    adapter = _adapter(tmp_path, extra_env={"FAKE_PI_PREAMBLE": "You are a helpful assistant."})
    adapter.start_day(SERVICE_DATE)

    with pytest.raises(AdapterIntegrityError, match="preamble"):
        adapter.send("[opening]", "opening", 30.0)


def test_a_rewritten_session_file_never_drives_an_integrity_check(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    staged = next((tmp_path / "staging").glob("*.jsonl"))
    lines = staged.read_text(encoding="utf-8").splitlines()
    system = json.loads(lines[3])
    system["message"]["sections"]["preamble"] = "forged"
    lines[3] = json.dumps(system)
    lines.insert(3, json.dumps({"type": "thinking_level_change", "thinkingLevel": "low"}))
    staged.write_text("\n".join(lines) + "\n", encoding="utf-8")

    adapter.send("[closing]", "closing", 30.0)
    adapter.end_day(SERVICE_DATE)

    day = adapter.days[0]
    assert day.system_sha256 == _expected_system_sha256(tmp_path / "workspace")
    assert day.thinking_level == "high"


def test_day_end_archives_the_session_and_records_the_system_digest(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    adapter.send("[closing]", "closing", 30.0)
    assert len(list((tmp_path / "staging").glob("*.jsonl"))) == 1

    adapter.end_day(SERVICE_DATE)

    assert list((tmp_path / "staging").iterdir()) == []
    archived = sorted((tmp_path / "run" / "pi-sessions").glob("*.jsonl"))
    assert [path.name for path in archived] == [
        "2026-10-30T07-26-15-867Z_sunridge-2026-10-01.jsonl"
    ]
    day = adapter.days[0]
    assert day.service_date == "2026-10-01"
    assert day.session_files == (str(archived[0]),)
    assert day.system_sha256 == _expected_system_sha256(tmp_path / "workspace")
    assert day.system_cwd == os.path.realpath(tmp_path / "workspace")
    assert day.thinking_level == "high"

    adapter.start_day(NEXT_DATE)
    adapter.send("[opening]", "opening", 30.0)
    staged = list((tmp_path / "staging").glob("*.jsonl"))
    assert [path.name.endswith("2026-10-02.jsonl") for path in staged] == [True]
    adapter.end_run("failed")
    assert list((tmp_path / "staging").iterdir()) == []
    assert len(list((tmp_path / "run" / "pi-sessions").glob("*.jsonl"))) == 2


def test_unstaged_sessions_stay_where_pi_wrote_them(tmp_path):
    adapter = _adapter(tmp_path, staged=False)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)

    adapter.end_day(SERVICE_DATE)

    archived = list((tmp_path / "run" / "pi-sessions").glob("*.jsonl"))
    assert len(archived) == 1
    assert adapter.days[0].session_files == (str(archived[0]),)


def test_a_deleted_session_file_does_not_fail_the_day(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    for path in (tmp_path / "staging").glob("*.jsonl"):
        path.unlink()

    adapter.end_day(SERVICE_DATE)

    assert adapter.days[0].session_files == ()
    assert adapter.days[0].system_sha256 == _expected_system_sha256(tmp_path / "workspace")


def test_a_completed_day_whose_stdout_carried_no_system_message_fails_the_day_end(tmp_path):
    adapter = _adapter(tmp_path, extra_env={"FAKE_PI_NO_SYSTEM": "1"})
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)

    with pytest.raises(AdapterIntegrityError, match="no system message"):
        adapter.end_day(SERVICE_DATE)


def test_a_timed_out_turn_is_flagged_in_the_usage_summary(tmp_path):
    adapter = _adapter(tmp_path, extra_env={"FAKE_PI_SLEEP": "5"}, terminate_grace_s=1.0)
    adapter.start_day(SERVICE_DATE)

    with pytest.raises(TimeoutError):
        adapter.send("[call]", "call", 0.5)

    usage = usage_summary(adapter, workspace_dir=tmp_path / "workspace")
    assert usage["timed_out_turns"] == 1
    assert usage["flags"] == [TIMEOUT_FLAG]


def test_an_interrupted_turn_terminates_pi_before_the_interrupt_propagates(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, extra_env={"FAKE_PI_SLEEP": "30"}, terminate_grace_s=1.0)
    adapter.start_day(SERVICE_DATE)
    terminated = []
    terminate = adapter._terminate

    def recording_terminate(process):
        terminate(process)
        terminated.append(process.poll())

    monkeypatch.setattr(adapter, "_terminate", recording_terminate)
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            adapter.send("[call]", "call", 60.0)
    finally:
        timer.cancel()

    assert time.monotonic() - started < 10
    assert len(terminated) == 1 and terminated[0] is not None


def test_agent_dir_and_offline_reach_the_pi_environment(tmp_path):
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text('{"providers": {}}', encoding="utf-8")
    adapter = _adapter(tmp_path, agent_dir=str(agent_dir), offline=True)

    env = adapter._agent_env
    assert env[PI_AGENT_DIR_ENV] == str(agent_dir)
    assert env[PI_OFFLINE_ENV] == "1"
    assert "--offline" in adapter._build_argv("session", "prompt")
    assert adapter.catalog_digests() == {
        "models.json": hashlib.sha256(b'{"providers": {}}').hexdigest()
    }
    assert adapter.process_identity() == {}


def test_model_catalog_links_are_never_followed(tmp_path):
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text("{}", encoding="utf-8")
    private = tmp_path / "private.json"
    private.write_text("private", encoding="utf-8")
    (agent_dir / "models-store.json").symlink_to(private)
    adapter = _adapter(tmp_path, agent_dir=str(agent_dir))

    assert adapter.catalog_digests() == {"models.json": hashlib.sha256(b"{}").hexdigest()}


def test_a_session_planted_under_an_archived_name_never_overwrites_the_archive(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    adapter.end_day(SERVICE_DATE)
    archived = (
        tmp_path / "run" / "pi-sessions"
        / "2026-10-30T07-26-15-867Z_sunridge-2026-10-01.jsonl"
    )
    genuine = archived.read_bytes()
    (tmp_path / "staging" / archived.name).write_text("{}\n", encoding="utf-8")

    adapter.archive_sessions()

    assert archived.read_bytes() == genuine


def test_without_an_agent_dir_no_pi_override_is_set(tmp_path):
    adapter = _adapter(tmp_path)

    assert PI_AGENT_DIR_ENV not in adapter._agent_env
    assert PI_OFFLINE_ENV not in adapter._agent_env
    assert adapter.catalog_digests() == {}


def _unprivileged_user() -> pwd.struct_passwd:
    if os.getuid() != 0:
        return pwd.getpwuid(os.getuid())
    return pwd.getpwnam("nobody")


def test_a_sandbox_user_runs_pi_under_its_own_identity_and_home(tmp_path, monkeypatch):
    entry = _unprivileged_user()
    monkeypatch.setenv("XDG_CONFIG_HOME", "/root/.config")
    monkeypatch.setenv("HOME", "/root")
    adapter = _adapter(tmp_path, sandbox_user=entry.pw_name)

    assert adapter.process_identity() == {
        "user": entry.pw_uid,
        "group": entry.pw_gid,
        "extra_groups": [],
    }
    env = adapter._agent_env
    assert env["HOME"] == entry.pw_dir
    assert env["USER"] == entry.pw_name
    assert env["LOGNAME"] == entry.pw_name
    assert "XDG_CONFIG_HOME" not in env


def test_the_agent_label_names_the_requested_thinking_level():
    config = PiConfig(provider="openai", model="gpt-6-astra", thinking="xhigh")

    assert agent_label(config) == "pi:openai/gpt-6-astra@xhigh"


ROOT_ON_LINUX = sys.platform.startswith("linux") and os.geteuid() == 0


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_a_sandboxed_pi_turn_runs_as_the_sandbox_user_and_is_archived():
    user = SandboxUser.lookup("nobody")
    if not probe_readable(user, Path(sys.executable)):
        pytest.skip("the test interpreter is not reachable by the sandbox user")
    base = Path(tempfile.mkdtemp(prefix="sandboxed-pi-", dir="/tmp"))
    try:
        os.chmod(base, SHARED_DIR_MODE)
        executable = Path(_fake_pi(base))
        os.chmod(executable.parent, SHARED_DIR_MODE)
        run_dir = base / "run"
        secure_private_dir(run_dir)
        root = workspace.create(base / "workspace")
        hand_over_workspace(
            root, user, read_only=workspace.docs_path(root), writable=(workspace.memory_path(root),)
        )
        staging = create_session_staging(user)
        adapter = PiAdapter(
            session_dir=staging,
            log_dir=run_dir / "pi-logs",
                config=PiConfig(
                provider="openai",
                model="gpt-test",
                thinking="high",
                executable=str(executable),
                sandbox_user=user.name,
            ),
            archive_dir=run_dir / "pi-sessions",
        )
        assert adapter.version == FAKE_VERSION
        adapter.start_run(root, prompts.SYSTEM_PROMPT, _agent_env(root))
        adapter.start_day(SERVICE_DATE)

        turn = adapter.send("[opening]", "opening", 30.0)

        assert turn.ended_normally
        staged = list(staging.glob("*.jsonl"))
        assert [path.stat().st_uid for path in staged] == [user.uid]
        assert not probe_readable(user, run_dir)
        adapter.end_day(SERVICE_DATE)
        assert list(staging.iterdir()) == []
        assert len(list((run_dir / "pi-sessions").glob("*.jsonl"))) == 1
        remove_session_staging(staging)
        assert not staging.parent.exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_planted_fifos_and_links_in_the_staging_dir_are_never_read(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    staging = tmp_path / "staging"
    os.mkfifo(staging / "2026-10-30T00-00-00-000Z_sunridge-2026-10-01.jsonl")
    (staging / "2026-10-30T00-00-01-000Z_sunridge-2026-10-01.jsonl").symlink_to(
        tmp_path / "run" / "pi-logs" / "2026-10-01-turn-0001-opening.turn.json"
    )

    adapter.send("[closing]", "closing", 30.0)
    adapter.end_day(SERVICE_DATE)

    archived = list((tmp_path / "run" / "pi-sessions").iterdir())
    assert [path.name for path in archived] == [
        "2026-10-30T07-26-15-867Z_sunridge-2026-10-01.jsonl"
    ]
    assert adapter.days[0].system_sha256 == _expected_system_sha256(tmp_path / "workspace")


def test_the_session_id_carries_the_date_but_no_run_identity(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    adapter.end_day(SERVICE_DATE)

    assert adapter.days[0].session_id == "sunridge-2026-10-01"
    archived = next((tmp_path / "run" / "pi-sessions").glob("*.jsonl"))
    for needle in ("oct_default", "gpt-test", "openai", "high"):
        assert needle not in archived.name
        assert needle not in adapter.days[0].session_id


def test_a_forged_session_preamble_cannot_fail_the_turn(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    staged = next((tmp_path / "staging").glob("*.jsonl"))
    with staged.open("a", encoding="utf-8") as handle:
        forged = {"role": "system", "content": "", "sections": {"preamble": "forged"}}
        handle.write(json.dumps({"type": "message", "message": forged}) + "\n")

    turn = adapter.send("[call]", "call", 30.0)

    assert turn.ended_normally


def _reaping_adapter(tmp_path, monkeypatch, **overrides):
    reaped: list[SandboxUser] = []
    adapter = _adapter(tmp_path, sandbox_user=_unprivileged_user().pw_name, **overrides)
    adapter.reap = reaped.append
    monkeypatch.setattr(adapter, "process_identity", lambda: {})
    return adapter, reaped


def test_every_sandboxed_turn_reaps_the_sandbox_users_processes(tmp_path, monkeypatch):
    adapter, reaped = _reaping_adapter(tmp_path, monkeypatch)
    adapter.start_day(SERVICE_DATE)

    adapter.send("[opening]", "opening", 30.0)

    assert reaped == [adapter.sandbox]


def test_a_failed_sandboxed_turn_still_reaps(tmp_path, monkeypatch):
    adapter, reaped = _reaping_adapter(tmp_path, monkeypatch, extra_env={"FAKE_PI_FAIL": "1"})
    adapter.start_day(SERVICE_DATE)

    turn = adapter.send("[opening]", "opening", 30.0)

    assert not turn.ended_normally
    assert reaped == [adapter.sandbox]


def test_a_timed_out_sandboxed_turn_still_reaps(tmp_path, monkeypatch):
    adapter, reaped = _reaping_adapter(
        tmp_path, monkeypatch, extra_env={"FAKE_PI_SLEEP": "5"}, terminate_grace_s=1.0
    )
    adapter.start_day(SERVICE_DATE)

    with pytest.raises(TimeoutError):
        adapter.send("[call]", "call", 0.5)

    assert reaped == [adapter.sandbox]


def test_a_turn_that_cannot_start_still_reaps(tmp_path, monkeypatch):
    adapter, reaped = _reaping_adapter(tmp_path, monkeypatch)
    adapter.config = dataclasses.replace(adapter.config, executable=str(tmp_path / "missing-pi"))
    adapter.start_day(SERVICE_DATE)

    with pytest.raises(OSError):
        adapter.send("[opening]", "opening", 30.0)

    assert reaped == [adapter.sandbox]


def test_an_unsandboxed_turn_never_reaps(tmp_path):
    reaped: list[SandboxUser] = []
    adapter = _adapter(tmp_path)
    adapter.reap = reaped.append
    adapter.start_day(SERVICE_DATE)

    adapter.send("[opening]", "opening", 30.0)

    assert reaped == []


def test_the_reaper_refuses_root():
    with pytest.raises(SandboxError, match="root"):
        kill_user_processes(SandboxUser(name="root", uid=0, gid=0, home="/root"))


def test_a_catalog_changed_during_the_run_is_flagged(tmp_path):
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text('{"providers": {}}', encoding="utf-8")
    adapter = _adapter(tmp_path, agent_dir=str(agent_dir))
    start = adapter.catalog_at_start
    unchanged = usage_summary(adapter, workspace_dir=tmp_path / "workspace")

    (agent_dir / "models.json").write_text('{"providers": {"x": {}}}', encoding="utf-8")
    changed = usage_summary(adapter, workspace_dir=tmp_path / "workspace")

    assert start == {"models.json": hashlib.sha256(b'{"providers": {}}').hexdigest()}
    assert unchanged["catalog_changed"] is False
    assert CATALOG_CHANGED_FLAG not in unchanged["flags"]
    assert changed["model_catalog_sha256_at_start"] == start
    assert changed["model_catalog_sha256"] != start
    assert changed["catalog_changed"] is True
    assert CATALOG_CHANGED_FLAG in changed["flags"]


def _process_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def _gone(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while _process_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_a_sandboxed_turn_leaves_no_background_process_behind():
    user = SandboxUser.lookup("nobody")
    if not probe_readable(user, Path(sys.executable)):
        pytest.skip("the test interpreter is not reachable by the sandbox user")
    base = Path(tempfile.mkdtemp(prefix="sandboxed-reap-", dir="/tmp"))
    try:
        os.chmod(base, SHARED_DIR_MODE)
        executable = Path(_fake_pi(base))
        os.chmod(executable.parent, SHARED_DIR_MODE)
        run_dir = base / "run"
        secure_private_dir(run_dir)
        root = workspace.create(base / "workspace")
        hand_over_workspace(
            root, user, read_only=workspace.docs_path(root), writable=(workspace.memory_path(root),)
        )
        staging = create_session_staging(user)
        pid_file = root / "background.pid"
        adapter = PiAdapter(
            session_dir=staging,
            log_dir=run_dir / "pi-logs",
            config=PiConfig(
                provider="openai",
                model="gpt-test",
                thinking="high",
                executable=str(executable),
                sandbox_user=user.name,
                extra_env={"FAKE_PI_BACKGROUND": str(pid_file)},
            ),
            archive_dir=run_dir / "pi-sessions",
        )
        adapter.start_run(root, prompts.SYSTEM_PROMPT, _agent_env(root))
        adapter.start_day(SERVICE_DATE)

        assert adapter.send("[opening]", "opening", 30.0).ended_normally

        background = int(pid_file.read_text(encoding="utf-8"))
        assert _gone(background)
        remove_session_staging(staging)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="needs root on Linux to switch users")
def test_a_sandboxed_pi_cannot_reopen_its_own_output_to_forge_it():
    user = SandboxUser.lookup("nobody")
    if not probe_readable(user, Path(sys.executable)):
        pytest.skip("the test interpreter is not reachable by the sandbox user")
    base = Path(tempfile.mkdtemp(prefix="sandboxed-forge-", dir="/tmp"))
    previous = os.umask(0)
    try:
        os.chmod(base, SHARED_DIR_MODE)
        executable = Path(_fake_pi(base))
        os.chmod(executable.parent, SHARED_DIR_MODE)
        run_dir = base / "run"
        secure_private_dir(run_dir)
        root = workspace.create(base / "workspace")
        hand_over_workspace(
            root, user, read_only=workspace.docs_path(root), writable=(workspace.memory_path(root),)
        )
        staging = create_session_staging(user)
        adapter = PiAdapter(
            session_dir=staging,
            log_dir=run_dir / "pi-logs",
            config=PiConfig(
                provider="openai",
                model="gpt-test",
                thinking="high",
                executable=str(executable),
                sandbox_user=user.name,
                extra_env={"FAKE_PI_FORGE_STDOUT": "1"},
            ),
            archive_dir=run_dir / "pi-sessions",
        )
        adapter.start_run(root, prompts.SYSTEM_PROMPT, _agent_env(root))
        adapter.start_day(SERVICE_DATE)

        turn = adapter.send("[opening]", "opening", 30.0)

        assert turn.text == "done"
        assert "forged" not in Path(adapter.turns[0].stdout_path).read_text(encoding="utf-8")
        remove_session_staging(staging)
    finally:
        os.umask(previous)
        shutil.rmtree(base, ignore_errors=True)


def test_a_sandboxed_pi_gets_a_working_agent_dir_reset_before_every_turn(tmp_path, monkeypatch):
    template = tmp_path / "pi-agent"
    template.mkdir()
    (template / "models.json").write_text('{"providers": {}}', encoding="utf-8")
    adapter, _ = _reaping_adapter(tmp_path, monkeypatch, agent_dir=str(template))
    home = adapter.agent_home
    try:
        assert home is not None and home != template
        assert adapter._agent_env[PI_AGENT_DIR_ENV] == str(home)
        adapter.start_day(SERVICE_DATE)
        adapter.send("[opening]", "opening", 30.0)
        (home / "settings.json").write_text('{"packages": ["evil"]}', encoding="utf-8")
        (home / "auth.json.lock").mkdir()

        adapter.send("[call]", "call", 30.0)

        assert sorted(path.name for path in home.iterdir()) == ["models.json"]
        assert adapter.catalog_digests() == {
            "models.json": hashlib.sha256(b'{"providers": {}}').hexdigest()
        }
        assert sorted(path.name for path in template.iterdir()) == ["models.json"]
    finally:
        adapter.close()
    assert not home.exists()


def test_an_unsandboxed_pi_uses_the_agent_dir_directly(tmp_path):
    template = tmp_path / "pi-agent"
    template.mkdir()
    adapter = _adapter(tmp_path, agent_dir=str(template))

    assert adapter.agent_home is None
    assert adapter._agent_env[PI_AGENT_DIR_ENV] == str(template)


def test_named_provider_keys_reach_pi_but_no_record(tmp_path, monkeypatch):
    secret = "sk-test-secret-value-1234"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    adapter = _adapter(tmp_path, secret_env=("OPENAI_API_KEY",))
    adapter.start_day(SERVICE_DATE)

    adapter.send("[opening]", "opening", 30.0)
    adapter.end_day(SERVICE_DATE)

    assert adapter._agent_env["OPENAI_API_KEY"] == secret
    usage = usage_summary(adapter, workspace_dir=tmp_path / "workspace")
    assert usage["pi_env"] == ["OPENAI_API_KEY"]
    assert secret not in json.dumps(usage)
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_a_missing_provider_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    with pytest.raises(PiAdapterError, match="OPENAI_API_KEY, ANTHROPIC_API_KEY"):
        _adapter(tmp_path, secret_env=("OPENAI_API_KEY", "ANTHROPIC_API_KEY"))


def test_a_system_patch_that_keeps_the_preamble_passes_the_turn(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    adapter._agent_env["FAKE_PI_SYSTEM_PATCH"] = json.dumps({"cwd": "<cwd>\nelsewhere\n</cwd>"})

    turn = adapter.send("[call]", "call", 30.0)
    adapter.end_day(SERVICE_DATE)

    assert turn.ended_normally
    assert adapter.days[0].system_sha256 == _expected_system_sha256(tmp_path / "workspace")


def test_a_system_patch_that_replaces_the_preamble_fails_the_turn(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    adapter._agent_env["FAKE_PI_SYSTEM_PATCH"] = json.dumps({"preamble": "forged"})

    with pytest.raises(AdapterIntegrityError, match="preamble"):
        adapter.send("[call]", "call", 30.0)


def test_a_session_planted_for_the_next_day_is_cleared_before_the_day_starts(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    adapter.send("[opening]", "opening", 30.0)
    staging = tmp_path / "staging"
    header = {"type": "session", "version": 3, "id": "sunridge-2026-10-02", "cwd": "x"}
    planted = staging / "2026-10-30T00-00-00-000Z_sunridge-2026-10-02.jsonl"
    planted.write_text(json.dumps(header) + "\n", encoding="utf-8")
    (staging / "linked.jsonl").symlink_to(planted)
    (staging / "nested").mkdir()
    (staging / "nested" / "notes.jsonl").write_text("{}\n", encoding="utf-8")
    adapter.end_day(SERVICE_DATE)

    adapter.start_day(NEXT_DATE)

    assert list(staging.iterdir()) == []
    adapter.send("[opening]", "opening", 30.0)
    adapter.end_day(NEXT_DATE)
    assert adapter.days[1].system_sha256 == _expected_system_sha256(tmp_path / "workspace")


def test_a_reaper_that_cannot_clear_the_sandbox_fails_the_turn_as_an_integrity_error(
    tmp_path, monkeypatch
):
    adapter, _ = _reaping_adapter(tmp_path, monkeypatch)

    def survive(user: SandboxUser) -> None:
        raise SandboxError(f"processes of {user.name} survived")

    adapter.reap = survive
    adapter.start_day(SERVICE_DATE)

    with pytest.raises(AdapterIntegrityError, match="survived"):
        adapter.send("[opening]", "opening", 30.0)


def test_turn_output_is_private_to_the_harness_whatever_the_umask(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_day(SERVICE_DATE)
    previous = os.umask(0)
    try:
        adapter.send("[opening]", "opening", 30.0)
    finally:
        os.umask(previous)

    record = adapter.turns[0]
    for path in (record.stdout_path, record.stderr_path):
        assert stat.S_IMODE(os.stat(path).st_mode) == PRIVATE_OUTPUT_MODE
