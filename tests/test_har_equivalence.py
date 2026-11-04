from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    register_conn,
)
from trades_bench._dump import dump
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.harness import prompts, trajectory, workspace
from trades_bench.harness.client import ControlClient
from trades_bench.harness.launcher import (
    EngineLaunchSpec,
    EngineTokens,
    agent_env,
    launch_engine,
)
from trades_bench.harness.runner import (
    ControlledRunner,
    HarnessLimits,
    initialize_controlled_run,
    resume_state,
)
from trades_bench.harness.scripted import (
    INCOMPLETE_REPLY,
    SCRIPTED_CONTEXT_POLICY,
    ScriptedAdapter,
    ScriptedReply,
    SentPrompt,
)
from trades_bench.harness.trajectory import TrajectoryWriter
from trades_bench.run_init import initialize_run, open_run_db

SEED = 5
ONE_DAY = ("2026-10-01", "2026-10-01")
THREE_DAYS = ("2026-10-01", "2026-10-03")

LABEL_COLUMNS = (
    "track",
    "model_id",
    "harness_name",
    "harness_version",
    "context_policy",
    "attempt_definition",
    "system_prompt_hash",
)

def scripted_config(dates=ONE_DAY, **overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.start_date": dates[0],
        "run.end_date": dates[1],
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def open_world(db_path: Path):
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    return conn


def run_id_of(conn) -> str:
    return str(conn.execute("SELECT run_id FROM _run").fetchone()["run_id"])


def world_dump(db_path: Path) -> bytes:
    conn = open_world(db_path)
    payload = dump(conn, run_id_of(conn))
    for row in payload["tables"]["_run"]:
        for column in LABEL_COLUMNS:
            row.pop(column, None)
    conn.close()
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


NEXT_INVOCATION_MARKER = ":control_next_invocation:"


def _normalised_idempotency(rows: list[dict]) -> list[dict]:
    normalised = []
    for row in rows:
        if NEXT_INVOCATION_MARKER in str(row["request_id"]):
            continue
        stripped = dict(row)
        stripped.pop("request_id")
        stripped.pop("fingerprint")
        normalised.append(stripped)
    return sorted(normalised, key=lambda row: json.dumps(row, sort_keys=True))


def next_invocation_responses(db_path: Path) -> list[str]:
    conn = open_world(db_path)
    payload = dump(conn, run_id_of(conn))
    conn.close()
    return [
        str(row["response"])
        for row in payload["tables"]["_idempotency"]
        if NEXT_INVOCATION_MARKER in str(row["request_id"])
    ]


def cross_track_dump(db_path: Path) -> bytes:
    conn = open_world(db_path)
    payload = dump(conn, run_id_of(conn))
    for row in payload["tables"]["_run"]:
        for column in LABEL_COLUMNS:
            row.pop(column, None)
    payload["tables"]["_idempotency"] = _normalised_idempotency(
        payload["tables"]["_idempotency"]
    )
    conn.close()
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def completed_invocation_ids(db_path: Path) -> list[int]:
    conn = open_world(db_path)
    rows = [
        int(row["invocation_id"])
        for row in conn.execute(
            "SELECT invocation_id FROM _invocations WHERE status = 'completed' "
            "ORDER BY invocation_id"
        ).fetchall()
    ]
    conn.close()
    return rows


def control_request_ids(db_path: Path) -> list[str]:
    conn = open_world(db_path)
    rows = [
        str(row["request_id"])
        for row in conn.execute(
            "SELECT request_id FROM _idempotency ORDER BY request_id"
        ).fetchall()
    ]
    conn.close()
    return rows


def call_attempt_rows(db_path: Path) -> list[tuple[int, int, str]]:
    conn = open_world(db_path)
    rows = [
        (int(row["call_id"]), int(row["attempt_number"]), str(row["outcome"]))
        for row in conn.execute(
            "SELECT call_id, attempt_number, outcome FROM _call_attempts "
            "ORDER BY call_id, attempt_number"
        ).fetchall()
    ]
    conn.close()
    return rows


def call_statuses(db_path: Path) -> list[tuple[int, str]]:
    conn = open_world(db_path)
    rows = [
        (int(row["id"]), str(row["status"]))
        for row in conn.execute("SELECT id, status FROM _calls ORDER BY id").fetchall()
    ]
    conn.close()
    return rows


def open_track_run(db_path: Path, config, *, seed: int = SEED) -> ScriptedNoOpAdapter:
    initialize_run(db_path, master_seed=seed, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_world(db_path)
    adapter = ScriptedNoOpAdapter()
    controller = Controller(conn, config, adapter, artifact_dir=GEO_ARTIFACT_DIR)
    controller.run()
    conn.close()
    return adapter


def harness_paths(tmp_path: Path) -> dict[str, Path]:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return {"run_dir": run_dir, "workspace": workspace_dir, "db_path": run_dir / "run.db"}


def controlled_run(
    adapter,
    paths: dict[str, Path],
    config,
    *,
    seed: int = SEED,
    resume_day: str | None = None,
) -> str:
    workspace.create(paths["workspace"], config)
    db_path = paths["db_path"]
    if resume_day is None:
        run_id = initialize_controlled_run(
            db_path,
            master_seed=seed,
            config=config,
            adapter=adapter,
            artifact_dir=GEO_ARTIFACT_DIR,
        )
    else:
        conn = open_world(db_path)
        run_id = run_id_of(conn)
        conn.close()
    tokens = EngineTokens.mint()
    engine = launch_engine(
        EngineLaunchSpec(
            db_path=db_path, run_dir=paths["run_dir"], geo_dir=GEO_ARTIFACT_DIR
        ),
        tokens=tokens,
    )
    try:
        runner = ControlledRunner(
            client=ControlClient(port=engine.port, token=tokens.control),
            adapter=adapter,
            trajectory=TrajectoryWriter(paths["run_dir"], run_id),
            workspace=paths["workspace"],
            limits=HarnessLimits.from_config(config),
            agent_env=agent_env(
                port=engine.port, token=tokens.agent, workspace=paths["workspace"]
            ),
            engine=engine,
            sleep=lambda seconds: None,
        )
        state = resume_state(paths["run_dir"], resume_day) if resume_day else None
        return runner.run(resume=state)
    finally:
        engine.stop()


def controlled_scripted_run(tmp_path: Path, adapter, dates=ONE_DAY, *, seed: int = SEED):
    paths = harness_paths(tmp_path)
    controlled_run(adapter, paths, scripted_config(dates), seed=seed)
    return paths


def test_scripted_adapter_declares_its_contract_fields():
    adapter = ScriptedAdapter(model_id="scripted-model")
    metadata = adapter.describe()
    assert metadata.context_policy == SCRIPTED_CONTEXT_POLICY == "custom:scripted"
    assert metadata.name == adapter.name
    assert metadata.version == adapter.version
    assert metadata.attempt_definition == adapter.attempt_definition
    assert metadata.model_id == "scripted-model"
    assert "no terminal action" in adapter.attempt_definition


def test_scripted_adapter_records_everything_it_was_sent(tmp_path):
    adapter = ScriptedAdapter([ScriptedReply(text="one"), ScriptedReply(text="two")])
    adapter.start_run(tmp_path, "system", {"SUNRIDGE_API_PORT": "1"})
    import datetime

    day = datetime.date(2026, 10, 1)
    adapter.start_day(day)
    first = adapter.send("prompt one", "opening", 30.0)
    second = adapter.send("prompt two", "call", 30.0)
    adapter.end_day(day)
    adapter.end_run("completed")

    assert (first.text, second.text) == ("one", "two")
    assert adapter.texts == ["prompt one", "prompt two"]
    assert [p.kind for p in adapter.prompts] == ["opening", "call"]
    assert [p.service_date for p in adapter.prompts] == ["2026-10-01", "2026-10-01"]
    assert adapter.record.system_prompt == "system"
    assert adapter.record.workspace == tmp_path
    assert adapter.record.env == {"SUNRIDGE_API_PORT": "1"}
    assert adapter.record.days_started == ["2026-10-01"]
    assert adapter.record.days_ended == ["2026-10-01"]
    assert adapter.record.end_status == "completed"


def test_scripted_adapter_is_deterministic():
    def build():
        adapter = ScriptedAdapter(
            responder=lambda sent: ScriptedReply(text=f"{sent.kind}:{sent.index}")
        )
        return [adapter.send(f"p{n}", "call", 5.0) for n in range(4)]

    assert build() == build()


def test_sm_call_07_reminder_literals_render_verbatim_at_the_frozen_timestamp():
    frozen = "2026-10-01T10:24:00-07:00"
    first = prompts.render_reminder({"call_id": 5512, "attempt": 1, "time": frozen})
    final = prompts.render_reminder({"call_id": 5512, "attempt": 2, "time": frozen})
    assert first == (
        "[2026-10-01 10:24 America/Phoenix]\n\n"
        "Call 5512 is still unresolved and the caller is waiting."
    )
    assert final == (
        "[2026-10-01 10:24 America/Phoenix]\n\nFinal reminder: call 5512 is still unresolved."
    )
    assert first.splitlines()[0] == final.splitlines()[0]


def test_sm_call_07_harness_counts_three_strikes_and_issues_both_reminders(tmp_path):
    adapter = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY)
    paths = controlled_scripted_run(tmp_path, adapter)

    reminders = [p.text for p in adapter.prompts if p.kind == "reminder"]
    assert reminders
    assert all(
        "is still unresolved and the caller is waiting." in text
        for text in reminders[::2]
    )
    assert any("Final reminder: call " in text for text in reminders)

    attempts = call_attempt_rows(paths["db_path"])
    assert attempts
    by_call: dict[int, list[tuple[int, str]]] = {}
    for call_id, number, outcome in attempts:
        by_call.setdefault(call_id, []).append((number, outcome))
    assert all(len(rows) == 3 for rows in by_call.values())
    assert {status for _, status in call_statuses(paths["db_path"])} == {"unanswered"}


def test_sm_call_07_reminders_share_the_call_prompt_timestamp(tmp_path):
    adapter = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY)
    controlled_scripted_run(tmp_path, adapter)

    stamp = None
    for prompt in adapter.prompts:
        if prompt.kind == "call":
            stamp = prompt.text.splitlines()[0]
        elif prompt.kind == "reminder":
            assert prompt.text.splitlines()[0] == stamp


def test_sm_call_08_controlled_and_open_track_reach_the_same_world(tmp_path):
    controlled_paths = harness_paths(tmp_path / "controlled")
    open_db = tmp_path / "open" / "run.db"
    open_db.parent.mkdir(parents=True, exist_ok=True)

    adapter = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY)
    controlled_run(adapter, controlled_paths, scripted_config(ONE_DAY))
    open_track_run(open_db, scripted_config(ONE_DAY))

    assert cross_track_dump(controlled_paths["db_path"]) == cross_track_dump(open_db)
    assert call_attempt_rows(controlled_paths["db_path"]) == call_attempt_rows(open_db)
    assert call_statuses(controlled_paths["db_path"]) == call_statuses(open_db)
    assert completed_invocation_ids(controlled_paths["db_path"]) == completed_invocation_ids(
        open_db
    )

    controlled_ids = control_request_ids(controlled_paths["db_path"])
    open_ids = control_request_ids(open_db)
    assert controlled_ids != open_ids
    assert any("complete-invocation-" in request_id for request_id in controlled_ids)
    assert any("invocation-complete-" in request_id for request_id in open_ids)

    controlled_openings = next_invocation_responses(controlled_paths["db_path"])
    assert len(controlled_openings) == len(completed_invocation_ids(open_db)) + 1
    assert next_invocation_responses(open_db) == []


def test_checkpoints_carry_the_engines_own_origin_event_id(tmp_path):
    adapter = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY)
    paths = controlled_scripted_run(tmp_path, adapter)

    checkpoints = [
        record
        for record in trajectory.read_all(paths["run_dir"])
        if record["kind"] == "checkpoint"
    ]
    assert checkpoints

    conn = open_world(paths["db_path"])
    origins = {
        int(row["invocation_id"]): row["origin_event_id"]
        for row in conn.execute(
            "SELECT invocation_id, origin_event_id FROM _invocations"
        ).fetchall()
    }
    conn.close()

    assert all(origins[int(r["invocation_id"])] is not None for r in checkpoints)
    assert [record["engine_checkpoint_id"] for record in checkpoints] == [
        origins[int(record["invocation_id"])] for record in checkpoints
    ]


def test_har08_two_adapters_reach_the_same_world_without_engine_change(tmp_path):
    first_paths = harness_paths(tmp_path / "first")
    second_paths = harness_paths(tmp_path / "second")
    config = scripted_config(ONE_DAY)

    first = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY, model_id="model-a")
    second = ScriptedAdapter(
        responder=lambda sent: INCOMPLETE_REPLY,
        model_id="model-b",
        name="scripted-alternate",
    )
    controlled_run(first, first_paths, config)
    controlled_run(second, second_paths, config)

    assert world_dump(first_paths["db_path"]) == world_dump(second_paths["db_path"])
    assert [p.text for p in first.prompts] == [p.text for p in second.prompts]


def test_acceptance_33_the_harness_never_invents_or_reorders_invocations(tmp_path):
    seen: list[SentPrompt] = []

    def responder(sent: SentPrompt) -> ScriptedReply:
        seen.append(sent)
        return INCOMPLETE_REPLY

    adapter = ScriptedAdapter(responder=responder)
    controlled_scripted_run(tmp_path, adapter)

    kinds = [p.kind for p in seen]
    assert kinds[0] == "opening"
    assert kinds[-1] == "closing"
    assert kinds.count("opening") == kinds.count("closing") == 1
    assert all(
        previous.kind != "reminder" or previous.text != current.text
        for previous, current in zip(seen, seen[1:], strict=False)
    )


MEMORY_NOTES = "call back Morgan Lee about the rooftop unit\n"
RETIRED_MEMORY_LABEL = "Your own notes, carried over from earlier days"


def test_har04_memory_is_never_injected_and_never_touched_across_days(tmp_path):
    paths = harness_paths(tmp_path)
    workspace.memory_path(paths["workspace"]).write_text(MEMORY_NOTES, encoding="utf-8")
    adapter = ScriptedAdapter(responder=lambda sent: ScriptedReply(text="noted"))

    controlled_run(adapter, paths, scripted_config(THREE_DAYS))

    openings = [sent for sent in adapter.prompts if sent.kind == "opening"]
    assert len(openings) == 3
    for sent in openings:
        assert sent.text.endswith(f"\n\n{prompts.OPENING_BODY}")
        assert sent.text.startswith("[")
    for sent in adapter.prompts:
        assert MEMORY_NOTES.strip() not in sent.text
        assert RETIRED_MEMORY_LABEL not in sent.text
        assert "MEMORY.md" not in sent.text or sent.kind == "closing"
    assert (
        workspace.memory_path(paths["workspace"]).read_text(encoding="utf-8") == MEMORY_NOTES
    )


def test_har07_recorded_system_prompt_hash_matches_the_bytes_the_adapter_received(tmp_path):
    adapter = ScriptedAdapter(responder=lambda sent: INCOMPLETE_REPLY)
    paths = controlled_scripted_run(tmp_path, adapter)

    delivered = adapter.record.system_prompt
    assert delivered is not None
    conn = open_world(paths["db_path"])
    recorded = str(conn.execute("SELECT system_prompt_hash FROM _run").fetchone()[0])
    conn.close()
    assert recorded == hashlib.sha256(delivered.encode("utf-8")).hexdigest()
    assert recorded == prompts.system_prompt_hash()


@pytest.mark.parametrize("dates", [ONE_DAY, THREE_DAYS])
def test_open_track_scripted_driver_is_repeatable(tmp_path, dates):
    first = tmp_path / "a" / "run.db"
    second = tmp_path / "b" / "run.db"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    open_track_run(first, scripted_config(dates))
    open_track_run(second, scripted_config(dates))
    assert world_dump(first) == world_dump(second)
