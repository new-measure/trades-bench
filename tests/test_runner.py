from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from trades_bench import clock
from trades_bench.adapter import AdapterIntegrityError, AgentTurn, RunMetadata
from trades_bench.harness import prompts, trajectory
from trades_bench.harness.client import ControlClient, ControlResult
from trades_bench.harness.runner import (
    CALL_ATTEMPT_LIMIT,
    ControlledRunner,
    HarnessLimits,
    HarnessRunFailed,
    HarnessRunNotTerminated,
    resume_state,
    resume_state_from_records,
)

SERVICE_DATE = "2026-10-01"
OPENING_TS = clock.phoenix_instant(2026, 10, 1, 6, 45)
CALL_TS = clock.phoenix_instant(2026, 10, 1, 9, 15)
CLOSING_TS = clock.phoenix_instant(2026, 10, 1, 19, 0)
CALL_ID = 77
MEMORY_LABEL_TEXT = "Your own notes, carried over from earlier days"

LIMITS = HarnessLimits(
    invocation_timeout_seconds=30,
    model_retry_limit=2,
    model_retry_backoff_seconds=5,
)


def _opening() -> dict:
    return {
        "invocation_id": 1,
        "kind": "opening",
        "ts_ms": OPENING_TS,
        "prompt_version": prompts.PROMPT_TEMPLATE_VERSION,
        "payload": {"service_date": SERVICE_DATE, "time": clock.to_iso8601(OPENING_TS)},
        "applied_events": [
            {"event_id": 10, "kind": "demand_generation", "ts_ms": OPENING_TS},
            {"event_id": 11, "kind": "opening_invocation", "ts_ms": OPENING_TS},
        ],
    }


def _call(invocation_id: int = 2) -> dict:
    return {
        "invocation_id": invocation_id,
        "kind": "call",
        "ts_ms": CALL_TS,
        "prompt_version": prompts.PROMPT_TEMPLATE_VERSION,
        "payload": {
            "time": clock.to_iso8601(CALL_TS),
            "call_id": CALL_ID,
            "caller": "Dana Reyes",
            "phone": "480-555-0143",
            "address": "1200 E Apache Blvd, Tempe, AZ 85281",
            "customer_type": "residential",
            "requested_service": "AC not cooling",
            "urgent": True,
        },
        "applied_events": [{"event_id": 20, "kind": "call_invocation", "ts_ms": CALL_TS}],
    }


def _closing(invocation_id: int = 3) -> dict:
    return {
        "invocation_id": invocation_id,
        "kind": "closing",
        "ts_ms": CLOSING_TS,
        "prompt_version": prompts.PROMPT_TEMPLATE_VERSION,
        "payload": {"service_date": SERVICE_DATE, "time": clock.to_iso8601(CLOSING_TS)},
        "applied_events": [{"event_id": 30, "kind": "closing_invocation", "ts_ms": CLOSING_TS}],
    }


def _conflict(rule: str, **details: Any) -> dict:
    return {
        "error": {
            "code": "conflict",
            "message": "conflict",
            "details": {"rule": rule, **details},
        },
        "meta": {"as_of": clock.to_iso8601(CALL_TS)},
    }


def _error(code: str, **details: Any) -> dict:
    return {
        "error": {"code": code, "message": code, "details": dict(details)},
        "meta": {"as_of": clock.to_iso8601(CALL_TS)},
    }


class FakeControl(ControlClient):

    def __init__(
        self,
        invocations: Sequence[dict],
        *,
        attempt_script: Mapping | None = None,
        fail_script: Sequence[str] = (),
        status_script: Sequence[bool] = (),
    ):
        super().__init__(port=0, token="unused-in-tests")
        self.invocations = list(invocations)
        self.attempt_script = dict(attempt_script or {})
        self.fail_script = list(fail_script)
        self.status_script = list(status_script)
        self.sent: list[tuple[str, dict]] = []

    def request_ids(self, operation: str) -> list[str]:
        return [body["request_id"] for name, body in self.sent if name == operation]

    def post(self, operation: str, body: Mapping[str, Any]) -> ControlResult:
        if self.on_request is not None:
            self.on_request(operation, dict(body))
        self.sent.append((operation, dict(body)))
        status, payload = self._respond(operation, body)
        return ControlResult(
            operation=operation, request=dict(body), status=status, body=payload, replayed=False
        )

    def _respond(self, operation: str, body: Mapping[str, Any]) -> tuple[int, dict]:
        if operation == "next_invocation":
            if self.invocations:
                data = self.invocations.pop(0)
            else:
                data = {"run_finished": True, "run_status": "completed"}
            return 200, {"data": data, "meta": {}}
        if operation in ("record_call_attempt", "record_final_unanswered_attempt"):
            key = (int(body["call_id"]), int(body["attempt_number"]))
            scripted = self.attempt_script.get(key, "ok")
            if scripted == "ok":
                return 200, {"data": dict(body), "meta": {}}
            if scripted == "call_not_active":
                return 409, _conflict("call_not_active")
            if scripted == "mismatch":
                return 409, _conflict("attempt_number_mismatch")
            return 409, _conflict("attempt_number_mismatch", expected=int(scripted))
        if operation == "call_status":
            active = self.status_script.pop(0) if self.status_script else True
            data = {
                "call_id": int(body["call_id"]),
                "status": "active" if active else "booked",
                "active": active,
            }
            return 200, {"data": data, "meta": {}}
        if operation == "fail_run":
            index = len(self.request_ids("fail_run")) - 1
            scripted = self.fail_script[index] if index < len(self.fail_script) else "ok"
            if scripted != "ok":
                return 409, _error(scripted)
        return 200, {"data": dict(body), "meta": {}}


class FakeAdapter:

    name = "controlled"
    version = "0.0.1-test"
    context_policy = "controlled_daily_reset"
    attempt_definition = "one completed model turn with no terminal action committed"

    def __init__(self, *, script: Sequence[str] = (), elapsed: Sequence[float] = ()) -> None:
        self.script = list(script)
        self.elapsed = list(elapsed)
        self.clock_value = 0.0
        self.sent: list[tuple[str, str, float]] = []
        self.days_started: list[date] = []
        self.days_ended: list[date] = []
        self.started: tuple[Path, str, dict] | None = None
        self.ended_status: str | None = None
        self.restored: tuple[tuple[str, str], ...] | None = None

    def monotonic(self) -> float:
        return self.clock_value

    def start_run(self, workspace: Path, system_prompt: str, env: Mapping[str, str]) -> None:
        self.started = (workspace, system_prompt, dict(env))

    def start_day(self, service_date: date) -> None:
        self.days_started.append(service_date)

    def end_day(self, service_date: date) -> None:
        self.days_ended.append(service_date)

    def end_run(self, status: str) -> None:
        self.ended_status = status

    def restore_context(self, transcript: tuple[tuple[str, str], ...]) -> None:
        self.restored = tuple(transcript)

    def describe(self) -> RunMetadata:
        return RunMetadata(
            name=self.name,
            version=self.version,
            context_policy=self.context_policy,
            attempt_definition=self.attempt_definition,
            model_id="test-model",
        )

    def send(self, prompt: str, kind: str, deadline_s: float) -> AgentTurn:
        index = len(self.sent)
        self.sent.append((prompt, kind, deadline_s))
        self.clock_value += self.elapsed[index] if index < len(self.elapsed) else 0.0
        behaviour = self.script[index] if index < len(self.script) else "ok"
        if behaviour == "raise":
            raise ConnectionError("provider unavailable")
        if behaviour == "integrity":
            raise AdapterIntegrityError("pi applied thinking level low but high was requested")
        if behaviour == "timeout":
            raise TimeoutError("the adapter enforced its deadline")
        if behaviour == "incomplete":
            return AgentTurn(
                text="", ended_normally=False, input_tokens=None, output_tokens=None
            )
        return AgentTurn(
            text=f"turn-{index}", ended_normally=True, input_tokens=11, output_tokens=22
        )


class FakeEngine:

    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> int:
        self.stops += 1
        return 0


def _workspace(tmp_path: Path, memory: str = "") -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text(memory, encoding="utf-8")
    return workspace


def _build(
    tmp_path: Path,
    invocations: Sequence[dict],
    *,
    adapter: FakeAdapter | None = None,
    attempt_script: Mapping | None = None,
    status_script: Sequence[bool] = (),
    limits: HarnessLimits = LIMITS,
    memory: str = "",
) -> tuple[ControlledRunner, FakeControl, FakeAdapter, list[float], Path, FakeEngine]:
    adapter = adapter or FakeAdapter()
    control = FakeControl(
        invocations, attempt_script=attempt_script, status_script=status_script
    )
    writer = trajectory.TrajectoryWriter(tmp_path / "private", "run-under-test")
    workspace = _workspace(tmp_path, memory)
    slept: list[float] = []
    engine = FakeEngine()
    runner = ControlledRunner(
        client=control,
        adapter=adapter,
        trajectory=writer,
        workspace=workspace,
        limits=limits,
        engine=engine,
        sleep=slept.append,
        monotonic=adapter.monotonic,
    )
    return runner, control, adapter, slept, tmp_path / "private", engine


def _records(run_dir: Path) -> list[dict]:
    return trajectory.read_all(run_dir)


def _kinds(records: Sequence[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [record for record in records if record["kind"] == kind]


def test_har04_one_conversation_per_day_with_nothing_auto_injected(tmp_path: Path) -> None:
    runner, control, adapter, _, run_dir, engine = _build(
        tmp_path, [_opening(), _call(), _closing()], attempt_script={(CALL_ID, 1): "call_not_active"}
    )

    assert runner.run() == "completed"

    assert [kind for _, kind, _ in adapter.sent] == ["opening", "call", "closing"]
    assert adapter.sent[0][0] == prompts.render_opening(_opening()["payload"])
    assert adapter.sent[1][0] == prompts.render_call(_call()["payload"])
    assert adapter.sent[2][0] == prompts.render_closing(_closing()["payload"])
    assert adapter.days_started == [date.fromisoformat(SERVICE_DATE)]
    assert adapter.days_ended == [date.fromisoformat(SERVICE_DATE)]
    assert adapter.started is not None
    assert adapter.started[1] == prompts.SYSTEM_PROMPT
    assert adapter.ended_status == "completed"
    assert engine.stops == 1

    assert control.request_ids("next_invocation") == [
        "next-invocation-1",
        "next-invocation-2",
        "next-invocation-3",
        "next-invocation-4",
    ]
    assert control.request_ids("complete_invocation") == [
        "complete-invocation-1",
        "complete-invocation-2",
        "complete-invocation-3",
    ]


def test_har04_memory_is_never_injected_and_never_rewritten(tmp_path: Path) -> None:
    notes = "keep the elderly customer on the 9am block, always\n"
    runner, _, adapter, _, run_dir, _ = _build(
        tmp_path,
        [_opening(), _call(), _closing()],
        attempt_script={(CALL_ID, 1): "call_not_active"},
        memory=notes,
    )

    assert runner.run() == "completed"

    assert adapter.sent[0][0] == prompts.render_opening(_opening()["payload"])
    for prompt_text, _, _ in adapter.sent:
        assert MEMORY_LABEL_TEXT not in prompt_text
        assert notes.strip() not in prompt_text
    assert (tmp_path / "workspace" / "MEMORY.md").read_text(encoding="utf-8") == notes
    assert {record["prompt_kind"] for record in _kinds(_records(run_dir), "prompt_issued")} == {
        "opening",
        "call",
        "closing",
    }


def test_har07_trajectory_records_every_kind(tmp_path: Path) -> None:
    runner, _, _, _, run_dir, _ = _build(
        tmp_path,
        [_opening(), _call(), _closing()],
        adapter=FakeAdapter(script=("ok", "ok", "ok", "ok")),
    )

    runner.run()

    produced = {record["kind"] for record in _records(run_dir)}
    assert produced == {
        "prompt_issued",
        "model_turn",
        "control_request",
        "control_response",
        "attempt_recorded",
        "reminder_issued",
        "world_event_applied",
        "checkpoint",
        "run_outcome",
    }


def test_checkpoint_follows_every_completed_invocation(tmp_path: Path) -> None:
    runner, _, _, _, run_dir, _ = _build(
        tmp_path, [_opening(), _closing()],
    )

    runner.run()

    checkpoints = _kinds(_records(run_dir), "checkpoint")
    assert [record["invocation_kind"] for record in checkpoints] == ["opening", "closing"]
    assert [record["engine_checkpoint_id"] for record in checkpoints] == [11, 30]
    assert all(len(record["workspace_digest"]) == 64 for record in checkpoints)


def test_control_traffic_is_logged_without_any_token(tmp_path: Path) -> None:
    runner, _, _, _, run_dir, _ = _build(tmp_path, [_opening()])

    runner.run()

    records = _records(run_dir)
    requests = _kinds(records, "control_request")
    assert [record["operation"] for record in requests][0] == "next_invocation"
    serialized = "".join(str(record) for record in records).lower()
    assert "authorization" not in serialized
    assert "bearer" not in serialized
    assert "unused-in-tests" not in serialized


def test_sm_call_07_three_attempts_issue_both_reminders_at_the_frozen_timestamp(
    tmp_path: Path,
) -> None:
    runner, control, adapter, _, run_dir, _ = _build(tmp_path, [_opening(), _call()])

    runner.run()

    assert [kind for _, kind, _ in adapter.sent] == ["opening", "call", "reminder", "reminder"]
    payload = _call()["payload"]
    first = prompts.render_reminder({**payload, "attempt": 1, "final": False})
    final = prompts.render_reminder({**payload, "attempt": 2, "final": True})
    assert adapter.sent[2][0] == first
    assert adapter.sent[3][0] == final
    assert "still unresolved and the caller is waiting" in first
    assert "Final reminder" in final
    assert first.startswith(prompts.render_call(payload).splitlines()[0])

    assert control.request_ids("record_call_attempt") == [
        f"call-attempt-{CALL_ID}-1",
        f"call-attempt-{CALL_ID}-2",
    ]
    assert control.request_ids("record_final_unanswered_attempt") == [f"call-final-{CALL_ID}"]
    final_body = [body for name, body in control.sent if name == "record_final_unanswered_attempt"]
    assert final_body[0]["attempt_number"] == CALL_ATTEMPT_LIMIT
    assert f"complete-invocation-{_call()['invocation_id']}" in control.request_ids(
        "complete_invocation"
    )


def test_a_settled_call_stops_the_attempt_loop(tmp_path: Path) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_call()],
        attempt_script={(CALL_ID, 2): "call_not_active"},
    )

    runner.run()

    assert [kind for _, kind, _ in adapter.sent] == ["call", "reminder"]
    assert control.request_ids("record_final_unanswered_attempt") == []


def test_attempt_number_mismatch_resynchronises_from_the_engine(tmp_path: Path) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_call()],
        attempt_script={(CALL_ID, 1): 2, (CALL_ID, 2): "ok", (CALL_ID, 3): "ok"},
    )

    runner.run()

    assert control.request_ids("record_call_attempt") == [
        f"call-attempt-{CALL_ID}-1",
        f"call-attempt-{CALL_ID}-2",
    ]
    assert [kind for _, kind, _ in adapter.sent] == ["call", "reminder"]


def test_attempt_number_mismatch_without_an_expected_detail_scans_for_the_counter(
    tmp_path: Path,
) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_call()],
        attempt_script={(CALL_ID, 1): "mismatch"},
    )

    runner.run()

    assert control.request_ids("record_call_attempt") == [
        f"call-attempt-{CALL_ID}-1",
        f"call-attempt-{CALL_ID}-2",
    ]
    assert control.request_ids("record_final_unanswered_attempt") == [f"call-final-{CALL_ID}"]
    assert [kind for _, kind, _ in adapter.sent] == ["call", "reminder"]


def test_acceptance_37_retries_are_not_attempts(tmp_path: Path) -> None:
    adapter = FakeAdapter(script=("raise", "raise", "ok"))
    runner, control, adapter, slept, run_dir, _ = _build(
        tmp_path,
        [_call()],
        adapter=adapter,
        attempt_script={(CALL_ID, 1): "call_not_active"},
    )

    runner.run()

    assert len(adapter.sent) == 3
    assert slept == [5.0, 5.0]
    assert control.request_ids("call_status") == [f"call-status-{CALL_ID}"] * 2
    assert len(control.request_ids("record_call_attempt")) == 1
    assert len(_kinds(_records(run_dir), "model_turn")) == 1
    assert len(_kinds(_records(run_dir), "prompt_issued")) == 1


def test_acceptance_37_exhausted_retries_fail_the_run(tmp_path: Path) -> None:
    runner, control, _, _, run_dir, engine = _build(
        tmp_path,
        [_opening()],
        adapter=FakeAdapter(script=("raise", "raise", "raise")),
    )

    with pytest.raises(HarnessRunFailed):
        runner.run()

    assert control.request_ids("fail_run") == ["fail-run-1"]
    reason = [body["reason"] for name, body in control.sent if name == "fail_run"][0]
    assert "retries" in reason
    outcomes = _kinds(_records(run_dir), "run_outcome")
    assert outcomes[0]["run_status"] == "failed"
    assert engine.stops == 1


def test_acceptance_37_timeout_branches(tmp_path: Path) -> None:
    call_runner, call_control, call_adapter, slept, _, _ = _build(
        tmp_path / "call",
        [_call()],
        adapter=FakeAdapter(elapsed=(40.0,)),
        attempt_script={(CALL_ID, 1): "call_not_active"},
    )
    call_runner.run()
    assert len(call_adapter.sent) == 1
    assert slept == []
    assert call_control.request_ids("record_call_attempt") == [f"call-attempt-{CALL_ID}-1"]

    day_runner, day_control, day_adapter, _, _, _ = _build(
        tmp_path / "day",
        [_opening(), _closing()],
        adapter=FakeAdapter(elapsed=(40.0, 0.0)),
    )
    assert day_runner.run() == "completed"
    assert [kind for _, kind, _ in day_adapter.sent] == ["opening", "closing"]
    assert day_control.request_ids("fail_run") == []


def test_acceptance_37_a_refused_fail_run_is_retried_with_a_fresh_ordinal(
    tmp_path: Path,
) -> None:
    runner, control, _, _, _, _ = _build(
        tmp_path,
        [_opening()],
        adapter=FakeAdapter(script=("raise", "raise", "raise")),
    )
    control.fail_script = ["request_id_conflict"]

    with pytest.raises(HarnessRunFailed) as caught:
        runner.run()

    assert not isinstance(caught.value, HarnessRunNotTerminated)
    assert control.request_ids("fail_run") == ["fail-run-1", "fail-run-2"]


def test_acceptance_37_a_run_that_cannot_be_failed_raises_a_distinct_error(
    tmp_path: Path,
) -> None:
    runner, control, _, _, _, engine = _build(
        tmp_path,
        [_opening()],
        adapter=FakeAdapter(script=("raise", "raise", "raise")),
    )
    control.fail_script = ["request_id_conflict"] * 4

    with pytest.raises(HarnessRunNotTerminated):
        runner.run()

    assert control.request_ids("fail_run") == ["fail-run-1", "fail-run-2", "fail-run-3"]
    assert engine.stops == 1


def test_resume_state_restores_the_fail_run_ordinal(tmp_path: Path) -> None:
    runner, control, _, _, run_dir, _ = _build(
        tmp_path,
        [_opening()],
        adapter=FakeAdapter(script=("raise", "raise", "raise")),
    )
    control.fail_script = ["request_id_conflict"]
    with pytest.raises(HarnessRunFailed):
        runner.run()

    state = resume_state(run_dir, SERVICE_DATE)
    assert state.fail_run_ordinal == 2

    resumed_adapter = FakeAdapter(script=("raise", "raise", "raise"))
    resumed_control = FakeControl([_opening()])
    resumed = ControlledRunner(
        client=resumed_control,
        adapter=resumed_adapter,
        trajectory=trajectory.TrajectoryWriter(run_dir, "run-under-test"),
        workspace=tmp_path / "workspace",
        limits=LIMITS,
        sleep=lambda seconds: None,
        monotonic=resumed_adapter.monotonic,
    )
    with pytest.raises(HarnessRunFailed):
        resumed.run(resume=state)

    assert resumed_control.request_ids("fail_run") == ["fail-run-3"]


def test_acceptance_37_an_adapter_raised_timeout_counts_as_one_incomplete_attempt(
    tmp_path: Path,
) -> None:
    runner, control, adapter, slept, run_dir, _ = _build(
        tmp_path,
        [_call()],
        adapter=FakeAdapter(script=("timeout",)),
        attempt_script={(CALL_ID, 1): "call_not_active"},
    )

    runner.run()

    assert len(adapter.sent) == 1
    assert slept == []
    assert control.request_ids("record_call_attempt") == [f"call-attempt-{CALL_ID}-1"]
    turns = _kinds(_records(run_dir), "model_turn")
    assert [record["stop_reason"] for record in turns] == ["invocation_timeout"]
    assert [record["ended_normally"] for record in turns] == [False]


def test_acceptance_37_an_adapter_raised_timeout_completes_the_day(tmp_path: Path) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_opening(), _closing()],
        adapter=FakeAdapter(script=("timeout", "ok")),
    )

    assert runner.run() == "completed"
    assert [kind for _, kind, _ in adapter.sent] == ["opening", "closing"]
    assert control.request_ids("fail_run") == []


def test_unknown_invocation_kind_fails_the_run(tmp_path: Path) -> None:
    invented = {**_opening(), "kind": "midday_bonus"}
    runner, control, _, _, _, _ = _build(tmp_path, [invented])

    with pytest.raises(HarnessRunFailed):
        runner.run()

    assert control.request_ids("fail_run") == ["fail-run-1"]


def test_resume_state_drops_the_interrupted_prompt(tmp_path: Path) -> None:
    records = [
        {"kind": "prompt_issued", "text": "opening prompt"},
        {"kind": "model_turn", "text": "opening reply"},
        {"kind": "prompt_issued", "text": "call prompt"},
        {"kind": "model_turn", "text": "call reply"},
        {"kind": "attempt_recorded", "call_id": CALL_ID, "attempt_number": 1},
        {"kind": "prompt_issued", "text": "reminder prompt"},
    ]
    index = [
        {
            "kind": "control_request",
            "operation": "next_invocation",
            "body": {"request_id": "next-invocation-2"},
        }
    ]

    state = resume_state_from_records(records, service_date=SERVICE_DATE, index_records=index)

    assert state.next_invocation_ordinal == 2
    assert state.attempt_counts == {CALL_ID: 1}
    assert state.transcript[-1] == ("turn", "call reply")
    assert all(text != "reminder prompt" for _, text in state.transcript)


def test_resume_rebuilds_the_day_and_restores_the_attempt_counter(tmp_path: Path) -> None:
    first, _, _, _, run_dir, _ = _build(
        tmp_path,
        [_opening(), _call()],
        adapter=FakeAdapter(script=("ok", "ok", "raise")),
        limits=HarnessLimits(
            invocation_timeout_seconds=30,
            model_retry_limit=0,
            model_retry_backoff_seconds=0,
        ),
    )
    with pytest.raises(HarnessRunFailed):
        first.run()

    state = resume_state(run_dir, SERVICE_DATE)
    assert state.attempt_counts == {CALL_ID: 1}
    assert state.next_invocation_ordinal == 2
    assert state.interrupted is True

    resumed_adapter = FakeAdapter()
    resumed = ControlledRunner(
        client=FakeControl([_call()]),
        adapter=resumed_adapter,
        trajectory=trajectory.TrajectoryWriter(run_dir, "run-under-test"),
        workspace=tmp_path / "workspace",
        limits=LIMITS,
        monotonic=resumed_adapter.monotonic,
    )

    assert resumed.run(resume=state) == "completed"

    assert resumed_adapter.days_started == [date.fromisoformat(SERVICE_DATE)]
    assert resumed_adapter.restored == state.transcript
    assert resumed_adapter.sent[0][0] == prompts.render_call(_call()["payload"])
    assert [kind for _, kind, _ in resumed_adapter.sent] == ["call", "reminder"]
    assert resumed.client.request_ids("record_call_attempt") == [f"call-attempt-{CALL_ID}-2"]
    assert resumed.client.request_ids("record_final_unanswered_attempt") == [
        f"call-final-{CALL_ID}"
    ]
    assert resumed.client.request_ids("next_invocation") == [
        "next-invocation-2",
        "next-invocation-3",
    ]


def test_a_call_settled_during_a_failed_turn_is_never_re_sent(tmp_path: Path) -> None:
    runner, control, adapter, slept, run_dir, _ = _build(
        tmp_path,
        [_opening(), _call(), _closing()],
        adapter=FakeAdapter(script=("ok", "raise", "ok")),
        status_script=(False,),
    )

    assert runner.run() == "completed"

    assert [kind for _, kind, _ in adapter.sent] == ["opening", "call", "closing"]
    assert slept == []
    assert control.request_ids("call_status") == [f"call-status-{CALL_ID}"]
    assert control.request_ids("record_call_attempt") == []
    assert control.request_ids("record_final_unanswered_attempt") == []
    assert _kinds(_records(run_dir), "reminder_issued") == []
    assert f"complete-invocation-{_call()['invocation_id']}" in control.request_ids(
        "complete_invocation"
    )
    assert control.request_ids("fail_run") == []


def test_a_reminder_settled_during_an_abnormal_turn_is_never_re_sent(tmp_path: Path) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_call()],
        adapter=FakeAdapter(script=("ok", "incomplete")),
        status_script=(False,),
    )

    assert runner.run() == "completed"

    assert [kind for _, kind, _ in adapter.sent] == ["call", "reminder"]
    assert control.request_ids("record_call_attempt") == [f"call-attempt-{CALL_ID}-1"]
    assert control.request_ids("record_final_unanswered_attempt") == []


def test_a_failed_turn_is_retried_while_the_call_is_still_active(tmp_path: Path) -> None:
    runner, control, adapter, slept, _, _ = _build(
        tmp_path,
        [_call()],
        adapter=FakeAdapter(script=("raise", "ok")),
        attempt_script={(CALL_ID, 1): "call_not_active"},
        status_script=(True,),
    )

    assert runner.run() == "completed"

    assert [kind for _, kind, _ in adapter.sent] == ["call", "call"]
    assert adapter.sent[0][0] == adapter.sent[1][0]
    assert slept == [5.0]
    assert control.request_ids("call_status") == [f"call-status-{CALL_ID}"]


def test_a_call_settled_on_the_last_failed_retry_does_not_fail_the_run(tmp_path: Path) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_call()],
        adapter=FakeAdapter(script=("raise", "raise", "raise")),
        status_script=(True, True, False),
    )

    assert runner.run() == "completed"

    assert len(adapter.sent) == 3
    assert control.request_ids("fail_run") == []
    assert control.request_ids("record_call_attempt") == []


def test_opening_retries_never_ask_for_a_call_status(tmp_path: Path) -> None:
    runner, control, adapter, _, _, _ = _build(
        tmp_path,
        [_opening()],
        adapter=FakeAdapter(script=("raise", "ok")),
    )

    runner.run()

    assert len(adapter.sent) == 2
    assert control.request_ids("call_status") == []


def test_an_adapter_integrity_failure_fails_the_run_without_retrying(tmp_path: Path) -> None:
    runner, control, adapter, slept, run_dir, _ = _build(
        tmp_path,
        [_opening()],
        adapter=FakeAdapter(script=("integrity", "ok")),
    )

    with pytest.raises(HarnessRunFailed) as caught:
        runner.run()

    assert len(adapter.sent) == 1
    assert slept == []
    assert "integrity" in caught.value.reason
    assert control.request_ids("fail_run") == ["fail-run-1"]
    assert adapter.ended_status == "failed"
    assert _kinds(_records(run_dir), "run_outcome")[0]["run_status"] == "failed"


class _DayIntegrityAdapter(FakeAdapter):

    def end_day(self, service_date: date) -> None:
        super().end_day(service_date)
        raise AdapterIntegrityError("pi sent a system preamble that differs")


def test_an_integrity_failure_at_day_end_fails_the_run(tmp_path: Path) -> None:
    runner, control, _, _, _, _ = _build(
        tmp_path,
        [_opening(), _closing()],
        adapter=_DayIntegrityAdapter(),
    )

    with pytest.raises(HarnessRunFailed) as caught:
        runner.run()

    assert "preamble" in caught.value.reason
    assert control.request_ids("fail_run") == ["fail-run-1"]
