from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from test_har_equivalence import (
    THREE_DAYS,
    call_attempt_rows,
    controlled_run,
    harness_paths,
    open_world,
    scripted_config,
    world_dump,
)
from trades_bench.harness import workspace
from trades_bench.harness.scripted import ScriptedAdapter, ScriptedReply, SentPrompt

pytestmark = pytest.mark.nightly

SNAPSHOT_DAY = "2026-10-02"
SNAPSHOT_AFTER_CALLS = 3


class HarnessKill(BaseException):
    pass


def _note_writer(root: Path):
    def write(sent: SentPrompt) -> None:
        memory = workspace.memory_path(root)
        existing = memory.read_text(encoding="utf-8") if memory.is_file() else ""
        stamp = sent.text.splitlines()[0]
        memory.write_text(
            existing + f"{sent.service_date} closed at {stamp}\n", encoding="utf-8"
        )

    return write


def _scripted_responder(root: Path, snapshot=None):
    calls_per_day: dict[str | None, int] = {}

    def responder(sent: SentPrompt) -> ScriptedReply:
        if sent.kind == "closing":
            return ScriptedReply(text="closing", action=_note_writer(root))
        if sent.kind == "opening":
            return ScriptedReply(text="good morning")
        if sent.kind == "call":
            seen = calls_per_day.get(sent.service_date, 0) + 1
            calls_per_day[sent.service_date] = seen
            if (
                snapshot is not None
                and sent.service_date == SNAPSHOT_DAY
                and seen == SNAPSHOT_AFTER_CALLS
            ):
                return ScriptedReply(text="still working", action=snapshot)
        return ScriptedReply(text="still working")

    return responder


def _snapshot(paths: dict[str, Path], destination: Path):
    def take(sent: SentPrompt) -> None:
        if destination.exists():
            return
        destination.mkdir(parents=True)
        shutil.copytree(paths["run_dir"], destination / "run")
        shutil.copytree(paths["workspace"], destination / "workspace")
        raise HarnessKill("harness killed mid-day-2")

    return take


def _restore(snapshot: Path, target: Path) -> dict[str, Path]:
    run_dir = target / "run"
    ws = target / "workspace"
    shutil.copytree(snapshot / "run", run_dir)
    shutil.copytree(snapshot / "workspace", ws)
    return {"run_dir": run_dir, "workspace": ws, "db_path": run_dir / "run.db"}


def _first_prompt_of_current_invocation(texts_kinds, snapshot_index: int) -> int:
    for index in range(snapshot_index, -1, -1):
        if texts_kinds[index][0] != "reminder":
            return index
    return 0


def test_det03_uninterrupted_and_resumed_runs_agree(tmp_path):
    config = scripted_config(THREE_DAYS)

    clean_paths = harness_paths(tmp_path / "clean")
    clean_adapter = ScriptedAdapter(responder=_scripted_responder(clean_paths["workspace"]))
    controlled_run(clean_adapter, clean_paths, config)

    killed_paths = harness_paths(tmp_path / "killed")
    snapshot_dir = tmp_path / "snapshot"
    killed_adapter = ScriptedAdapter(
        responder=_scripted_responder(
            killed_paths["workspace"], snapshot=_snapshot(killed_paths, snapshot_dir)
        )
    )
    with pytest.raises(HarnessKill):
        controlled_run(killed_adapter, killed_paths, config)

    assert snapshot_dir.exists()

    resumed_paths = _restore(snapshot_dir, tmp_path / "resumed")
    resumed_adapter = ScriptedAdapter(
        responder=_scripted_responder(resumed_paths["workspace"])
    )
    controlled_run(resumed_adapter, resumed_paths, config, resume_day=SNAPSHOT_DAY)

    assert world_dump(resumed_paths["db_path"]) == world_dump(clean_paths["db_path"])
    assert call_attempt_rows(resumed_paths["db_path"]) == call_attempt_rows(
        clean_paths["db_path"]
    )
    assert workspace.workspace_digest(resumed_paths["workspace"]) == workspace.workspace_digest(
        clean_paths["workspace"]
    )
    assert (
        workspace.memory_path(resumed_paths["workspace"]).read_bytes()
        == workspace.memory_path(clean_paths["workspace"]).read_bytes()
    )

    clean_prompts = [(p.kind, p.text) for p in clean_adapter.prompts]
    killed_prompts = [(p.kind, p.text) for p in killed_adapter.prompts]
    resume_start = _first_prompt_of_current_invocation(clean_prompts, len(killed_prompts) - 1)
    resumed_prompts = [(p.kind, p.text) for p in resumed_adapter.prompts]
    assert resumed_prompts[0][0] == clean_prompts[resume_start][0]
    assert resumed_prompts[0][1].endswith(clean_prompts[resume_start][1])
    assert resumed_prompts[1:] == clean_prompts[resume_start + 1 :]


def test_det03_snapshot_kill_produces_no_duplicate_invocation(tmp_path):
    config = scripted_config(THREE_DAYS)
    paths = harness_paths(tmp_path / "killed")
    snapshot_dir = tmp_path / "snapshot"
    adapter = ScriptedAdapter(
        responder=_scripted_responder(paths["workspace"], snapshot=_snapshot(paths, snapshot_dir))
    )
    with pytest.raises(HarnessKill):
        controlled_run(adapter, paths, config)

    resumed_paths = _restore(snapshot_dir, tmp_path / "resumed")
    resumed_adapter = ScriptedAdapter(responder=_scripted_responder(resumed_paths["workspace"]))
    controlled_run(resumed_adapter, resumed_paths, config, resume_day=SNAPSHOT_DAY)

    conn = open_world(resumed_paths["db_path"])
    duplicates = conn.execute(
        "SELECT origin_event_id, COUNT(*) c FROM _invocations "
        "WHERE origin_event_id IS NOT NULL GROUP BY origin_event_id HAVING c > 1"
    ).fetchall()
    conn.close()
    assert duplicates == []
