from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from trades_bench import clock, run_init
from trades_bench.adapter import HarnessAdapter
from trades_bench.config import RunConfig
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.harness import prompts, trajectory
from trades_bench.harness.client import ControlClient, ControlResult, ControlTransportError
from trades_bench.harness.launcher import EngineProcess
from trades_bench.harness.trajectory import TrajectoryWriter
from trades_bench.harness.workspace import workspace_digest
from trades_bench.http_engine import ORACLE_BYPASS_ENV_NAMES

TRACK = "controlled"
CALL_ATTEMPT_LIMIT = 3
CALL_NOT_ACTIVE = "call_not_active"
ATTEMPT_NUMBER_MISMATCH = "attempt_number_mismatch"

OPENING = "opening"
CALL = "call"
REMINDER = "reminder"
CLOSING = "closing"

_INVOCATION_EVENT_KINDS = ("opening_invocation", "call_invocation", "closing_invocation")
_TIMEOUT_STOP_REASON = "invocation_timeout"
_FAIL_RUN_ATTEMPTS = 3
_TERMINAL_RUN_ERROR_CODES = ("run_not_active",)


class HarnessRunFailed(RuntimeError):

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HarnessRunNotTerminated(HarnessRunFailed):
    pass


@dataclass(frozen=True)
class HarnessLimits:

    invocation_timeout_seconds: int = 900
    model_retry_limit: int = 3
    model_retry_backoff_seconds: int = 10

    @classmethod
    def from_config(cls, config: RunConfig) -> HarnessLimits:
        return cls(
            invocation_timeout_seconds=config.get_int("harness.invocation_timeout_seconds"),
            model_retry_limit=config.get_int("harness.model_retry_limit"),
            model_retry_backoff_seconds=config.get_int("harness.model_retry_backoff_seconds"),
        )


@dataclass(frozen=True)
class RunFinished:

    run_status: str


@dataclass(frozen=True)
class Invocation:

    invocation_id: int
    kind: str
    ts_ms: int
    payload: Mapping[str, Any]
    prompt_version: str
    applied_events: tuple[Mapping[str, Any], ...] = ()

    @property
    def simulated_ts(self) -> str:
        return clock.to_iso8601(self.ts_ms)

    @property
    def origin_event_id(self) -> int | None:
        for event in reversed(self.applied_events):
            if str(event.get("kind")) in _INVOCATION_EVENT_KINDS:
                return int(event["event_id"])
        if self.applied_events:
            return int(self.applied_events[-1]["event_id"])
        return None


def parse_invocation(data: Mapping[str, Any]) -> Invocation | RunFinished:
    if data.get("run_finished"):
        return RunFinished(str(data.get("run_status", "")))
    events = data.get("applied_events") or ()
    return Invocation(
        invocation_id=int(data["invocation_id"]),
        kind=str(data["kind"]),
        ts_ms=int(data["ts_ms"]),
        payload=dict(data.get("payload") or {}),
        prompt_version=str(data.get("prompt_version", prompts.PROMPT_TEMPLATE_VERSION)),
        applied_events=tuple(dict(event) for event in events),
    )


@dataclass(frozen=True)
class ResumeState:

    service_date: str | None = None
    next_invocation_ordinal: int = 0
    attempt_counts: Mapping[int, int] = field(default_factory=dict)
    transcript: tuple[tuple[str, str], ...] = ()
    interrupted: bool = False
    fail_run_ordinal: int = 0


def _request_ordinal(record: Mapping[str, Any], operation: str) -> int:
    if record.get("kind") != "control_request" or record.get("operation") != operation:
        return 0
    body = record.get("body")
    request_id = str(body.get("request_id", "")) if isinstance(body, Mapping) else ""
    _, _, suffix = request_id.rpartition("-")
    return int(suffix) if suffix.isdigit() else 0


def _next_invocation_ordinal(record: Mapping[str, Any]) -> int:
    return _request_ordinal(record, "next_invocation")


def _fail_run_ordinal(record: Mapping[str, Any]) -> int:
    return _request_ordinal(record, "fail_run")


def _opened_invocation_id(record: Mapping[str, Any]) -> int | None:
    if record.get("kind") != "control_response" or record.get("operation") != "next_invocation":
        return None
    body = record.get("body")
    data = body.get("data") if isinstance(body, Mapping) else None
    value = data.get("invocation_id") if isinstance(data, Mapping) else None
    return int(value) if isinstance(value, int) else None


def resume_state_from_records(
    day_records: Sequence[Mapping[str, Any]],
    *,
    service_date: str | None = None,
    index_records: Sequence[Mapping[str, Any]] = (),
) -> ResumeState:
    ordinal = 0
    fail_ordinal = 0
    opened: int | None = None
    checkpointed: set[int] = set()
    for record in (*index_records, *day_records):
        ordinal = max(ordinal, _next_invocation_ordinal(record))
        fail_ordinal = max(fail_ordinal, _fail_run_ordinal(record))
        candidate = _opened_invocation_id(record)
        if candidate is not None:
            opened = candidate
        if record.get("kind") == "checkpoint" and record.get("invocation_id") is not None:
            checkpointed.add(int(record["invocation_id"]))
    interrupted = opened is not None and opened not in checkpointed
    attempts: dict[int, int] = {}
    transcript: list[tuple[str, str]] = []
    for record in day_records:
        kind = record.get("kind")
        if kind == "prompt_issued":
            transcript.append(("prompt", str(record.get("text", ""))))
        elif kind == "model_turn":
            transcript.append(("turn", str(record.get("text", ""))))
        elif kind == "attempt_recorded":
            attempts[int(record["call_id"])] = int(record["attempt_number"])
    while transcript and transcript[-1][0] == "prompt":
        transcript.pop()
    return ResumeState(
        service_date=service_date,
        next_invocation_ordinal=ordinal,
        attempt_counts=attempts,
        transcript=tuple(transcript),
        interrupted=interrupted,
        fail_run_ordinal=fail_ordinal,
    )


def resume_state(run_dir: str | Path, service_date: str) -> ResumeState:
    return resume_state_from_records(
        trajectory.read_day(run_dir, service_date),
        service_date=service_date,
        index_records=trajectory.read_index(run_dir),
    )


def _refuse_oracle_bypass_environment() -> None:
    for name in sorted(ORACLE_BYPASS_ENV_NAMES):
        if os.environ.get(name):
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"a scored run refuses to start while {name} is set",
            )


def initialize_controlled_run(
    db_path: str | Path,
    *,
    master_seed: int,
    config: RunConfig,
    adapter: HarnessAdapter,
    run_id: str | None = None,
    dataset_version: str = "",
    artifact_dir: str | Path | None = None,
) -> str:
    _refuse_oracle_bypass_environment()
    metadata = adapter.describe()
    return run_init.initialize_run(
        db_path,
        run_id,
        master_seed=master_seed,
        config=config,
        dataset_version=dataset_version,
        artifact_dir=artifact_dir,
        model_id=metadata.model_id,
        harness_name=metadata.name,
        harness_version=metadata.version,
        context_policy=metadata.context_policy,
        attempt_definition=metadata.attempt_definition,
        track=TRACK,
        system_prompt_hash=prompts.system_prompt_hash(),
    )


@dataclass(frozen=True)
class _TurnOutcome:

    text: str
    timed_out: bool


@dataclass(frozen=True)
class _AttemptOutcome:

    settled: bool = False
    finalized: bool = False
    recorded: int | None = None
    mismatch: bool = False
    expected: int | None = None


class ControlledRunner:

    def __init__(
        self,
        *,
        client: ControlClient,
        adapter: HarnessAdapter,
        trajectory: TrajectoryWriter,
        workspace: str | Path,
        limits: HarnessLimits,
        agent_env: Mapping[str, str] | None = None,
        engine: EngineProcess | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.adapter = adapter
        self.trajectory = trajectory
        self.workspace = Path(workspace)
        self.limits = limits
        self.agent_env = dict(agent_env or {})
        self.engine = engine
        self.sleep = sleep
        self.monotonic = monotonic
        self._next_ordinal = 0
        self._fail_ordinal = 0
        self._day: str | None = None
        self._attempt_counts: dict[int, int] = {}
        self._invocation_id: int | None = None
        self._simulated_ts: str | None = None
        self.client.on_request = self._record_request

    def run(self, *, resume: ResumeState | None = None) -> str:
        self.adapter.start_run(self.workspace, prompts.SYSTEM_PROMPT, dict(self.agent_env))
        if resume is not None:
            self._restore(resume)
        while True:
            outcome = self._next_invocation()
            if isinstance(outcome, RunFinished):
                return self._finish(outcome.run_status)
            self._drive(outcome)
            self._complete(outcome)

    def _record_request(self, operation: str, body: Mapping[str, Any]) -> None:
        self.trajectory.control_request(
            operation=operation,
            body=dict(body),
            invocation_id=self._invocation_id,
            simulated_ts=self._simulated_ts,
        )

    def _record_response(self, result: ControlResult) -> ControlResult:
        self.trajectory.control_response(
            operation=result.operation,
            status=result.status,
            body=dict(result.body),
            invocation_id=self._invocation_id,
            simulated_ts=self._simulated_ts,
            replayed=result.replayed,
        )
        return result

    def _next_invocation(self) -> Invocation | RunFinished:
        self._invocation_id = None
        self._next_ordinal += 1
        result = self._record_response(self.client.next_invocation(self._next_ordinal))
        if not result.ok:
            self._fail(f"next_invocation refused: {result.describe_failure()}")
        outcome = parse_invocation(result.data)
        if isinstance(outcome, RunFinished):
            return outcome
        self._invocation_id = outcome.invocation_id
        self._simulated_ts = outcome.simulated_ts
        for event in outcome.applied_events:
            self.trajectory.world_event_applied(
                invocation_id=outcome.invocation_id,
                simulated_ts=outcome.simulated_ts,
                event_id=int(event["event_id"]),
                event_kind=str(event["kind"]),
                event_ts_ms=int(event["ts_ms"]),
            )
        return outcome

    def _drive(self, inv: Invocation) -> None:
        if inv.kind == OPENING:
            self._open_day(inv)
            self._send(inv, prompts.render_opening(inv.payload), OPENING)
        elif inv.kind == CALL:
            self._drive_call(inv)
        elif inv.kind == CLOSING:
            self._send(inv, prompts.render_closing(inv.payload), CLOSING)
        else:
            self._fail(f"engine returned an unknown invocation kind {inv.kind!r}")

    def _complete(self, inv: Invocation) -> None:
        result = self._record_response(self.client.complete_invocation(inv.invocation_id))
        if not result.ok:
            self._fail(f"complete_invocation refused: {result.describe_failure()}")
        self.trajectory.checkpoint(
            invocation_id=inv.invocation_id,
            simulated_time=inv.simulated_ts,
            kind=inv.kind,
            engine_checkpoint_id=inv.origin_event_id,
            workspace_digest=workspace_digest(self.workspace),
        )
        if inv.kind == CLOSING:
            self._close_day()

    def _open_day(self, inv: Invocation) -> None:
        service_date = str(inv.payload["service_date"])
        self.trajectory.open_day(service_date)
        self._day = service_date
        self.adapter.start_day(date.fromisoformat(service_date))

    def _close_day(self) -> None:
        if self._day is None:
            return
        self.adapter.end_day(date.fromisoformat(self._day))
        self.trajectory.close_day()
        self._day = None

    def _send(self, inv: Invocation, text: str, kind: str) -> _TurnOutcome:
        self.trajectory.prompt_issued(
            invocation_id=inv.invocation_id,
            simulated_ts=inv.simulated_ts,
            prompt_kind=kind,
            text=text,
            prompt_template_version=inv.prompt_version,
        )
        deadline_s = float(self.limits.invocation_timeout_seconds)
        allowed = int(self.limits.model_retry_limit) + 1
        failure = "no attempt was made"
        for attempt in range(allowed):
            if attempt:
                self.sleep(float(self.limits.model_retry_backoff_seconds))
            started = self.monotonic()
            try:
                turn = self.adapter.send(text, kind, deadline_s)
            except TimeoutError:
                self.trajectory.model_turn(
                    invocation_id=inv.invocation_id,
                    simulated_ts=inv.simulated_ts,
                    text="",
                    input_tokens=None,
                    output_tokens=None,
                    stop_reason=_TIMEOUT_STOP_REASON,
                    ended_normally=False,
                )
                return _TurnOutcome(text="", timed_out=True)
            except Exception as exc:
                failure = f"adapter raised {type(exc).__name__}"
                continue
            if self.monotonic() - started >= deadline_s:
                self.trajectory.model_turn(
                    invocation_id=inv.invocation_id,
                    simulated_ts=inv.simulated_ts,
                    text=turn.text,
                    input_tokens=turn.input_tokens,
                    output_tokens=turn.output_tokens,
                    stop_reason=_TIMEOUT_STOP_REASON,
                    ended_normally=False,
                )
                return _TurnOutcome(text=turn.text, timed_out=True)
            if not turn.ended_normally:
                failure = "model turn did not end normally"
                continue
            self.trajectory.model_turn(
                invocation_id=inv.invocation_id,
                simulated_ts=inv.simulated_ts,
                text=turn.text,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                ended_normally=True,
            )
            return _TurnOutcome(text=turn.text, timed_out=False)
        self._fail(
            f"model turn failed after {self.limits.model_retry_limit} retries: {failure}"
        )

    def _drive_call(self, inv: Invocation) -> None:
        call_id = int(inv.payload["call_id"])
        attempts = self._attempt_counts.get(call_id, 0)
        text = prompts.render_call(inv.payload)
        kind = CALL
        while True:
            self._send(inv, text, kind)
            outcome = self._record_attempt(inv, call_id, attempts + 1)
            if outcome.mismatch:
                outcome = self._resync_attempt(inv, call_id, attempts + 1, outcome.expected)
            if outcome.settled or outcome.finalized:
                return
            attempts = int(outcome.recorded or 0)
            self._attempt_counts[call_id] = attempts
            final = attempts == CALL_ATTEMPT_LIMIT - 1
            text = prompts.render_reminder(
                {
                    "call_id": call_id,
                    "time": inv.payload["time"],
                    "attempt": attempts,
                    "final": final,
                }
            )
            kind = REMINDER
            self.trajectory.reminder_issued(
                invocation_id=inv.invocation_id,
                simulated_ts=inv.simulated_ts,
                call_id=call_id,
                attempt_number=attempts,
                final=final,
                text=text,
            )

    def _record_attempt(
        self, inv: Invocation, call_id: int, attempt_number: int
    ) -> _AttemptOutcome:
        final = attempt_number >= CALL_ATTEMPT_LIMIT
        number = CALL_ATTEMPT_LIMIT if final else attempt_number
        if final:
            result = self._record_response(
                self.client.record_final_unanswered_attempt(call_id, number)
            )
        else:
            result = self._record_response(self.client.record_call_attempt(call_id, number))
        if result.ok:
            self.trajectory.attempt_recorded(
                invocation_id=inv.invocation_id,
                simulated_ts=inv.simulated_ts,
                call_id=call_id,
                attempt_number=number,
                final=final,
            )
            self._attempt_counts[call_id] = number
            return _AttemptOutcome(finalized=final, recorded=number)
        if result.error_rule == CALL_NOT_ACTIVE:
            return _AttemptOutcome(settled=True)
        if result.error_rule == ATTEMPT_NUMBER_MISMATCH:
            expected = result.error_details.get("expected")
            return _AttemptOutcome(
                mismatch=True,
                expected=int(expected) if isinstance(expected, int) else None,
            )
        self._fail(f"attempt bookkeeping refused: {result.describe_failure()}")

    def _resync_attempt(
        self, inv: Invocation, call_id: int, tried: int, expected: int | None
    ) -> _AttemptOutcome:
        if expected is not None:
            candidates = [expected]
        else:
            candidates = [n for n in range(1, CALL_ATTEMPT_LIMIT + 1) if n != tried]
        for candidate in candidates:
            outcome = self._record_attempt(inv, call_id, candidate)
            if not outcome.mismatch:
                return outcome
        self._fail(f"call {call_id} attempt counter could not be resynchronised")

    def _finish(self, run_status: str) -> str:
        self._close_day()
        self.trajectory.run_outcome(run_status=run_status)
        self.adapter.end_run(run_status)
        self._stop_engine()
        return run_status

    def _fail(self, reason: str) -> NoReturn:
        terminated = self._request_fail_run(reason)
        self.trajectory.run_outcome(run_status="failed", detail=reason)
        self.adapter.end_run("failed")
        self._stop_engine()
        if not terminated:
            raise HarnessRunNotTerminated(reason)
        raise HarnessRunFailed(reason)

    def _request_fail_run(self, reason: str) -> bool:
        for _ in range(_FAIL_RUN_ATTEMPTS):
            self._fail_ordinal += 1
            try:
                result = self._record_response(
                    self.client.fail_run(reason, self._fail_ordinal)
                )
            except ControlTransportError:
                return False
            if result.ok or result.error_code in _TERMINAL_RUN_ERROR_CODES:
                return True
        return False

    def _stop_engine(self) -> None:
        if self.engine is not None:
            self.engine.stop()

    def _restore(self, state: ResumeState) -> None:
        ordinal = int(state.next_invocation_ordinal)
        self._next_ordinal = ordinal - 1 if state.interrupted and ordinal else ordinal
        self._fail_ordinal = int(state.fail_run_ordinal)
        self._attempt_counts = {int(k): int(v) for k, v in state.attempt_counts.items()}
        if state.service_date is None:
            return
        self._day = state.service_date
        self.trajectory.open_day(state.service_date)
        self.adapter.start_day(date.fromisoformat(state.service_date))
        if not state.transcript:
            return
        restore_context = getattr(self.adapter, "restore_context", None)
        if callable(restore_context):
            restore_context(tuple(state.transcript))


__all__ = [
    "ATTEMPT_NUMBER_MISMATCH",
    "CALL_ATTEMPT_LIMIT",
    "CALL_NOT_ACTIVE",
    "ControlledRunner",
    "HarnessLimits",
    "HarnessRunFailed",
    "HarnessRunNotTerminated",
    "Invocation",
    "ResumeState",
    "RunFinished",
    "TRACK",
    "initialize_controlled_run",
    "parse_invocation",
    "resume_state",
    "resume_state_from_records",
]
