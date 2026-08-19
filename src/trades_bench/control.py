from __future__ import annotations

import sqlite3

from . import appointments, calls, surface
from .config import RunConfig
from .enums import InvocationType
from .errors import EngineError, ErrorCode
from .geo_reader import GeoArtifact
from .txn import Services, TxnContext, privileged, privileged_request_id

_UNANSWERED_ATTEMPT_THRESHOLD = 3


def _state_illegal(reason: str, message: str, **details: object) -> EngineError:
    return EngineError(ErrorCode.STATE_ILLEGAL, message, details={"reason": reason, **details})


def _call_row(conn: sqlite3.Connection, call_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"call {call_id} not found")
    return row


def _require_current_call(ctx: TxnContext, call: sqlite3.Row) -> None:
    call_id = int(call["id"])
    if ctx.invocation is not None and ctx.invocation["type"] != InvocationType.CALL.value:
        raise _state_illegal(
            "call_not_active",
            f"call {call_id} is reachable only inside an active call invocation",
        )
    if call["status"] != "active":
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not active (status {call['status']})"
        )
    if calls.current_active_call_id(ctx.conn) != call_id:
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not the currently active call"
        )


def _incomplete_attempt_count(conn: sqlite3.Connection, call_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM _call_attempts WHERE call_id = ? AND outcome = 'incomplete'",
        (call_id,),
    ).fetchone()
    return int(row["c"])


def record_call_attempt(
    ctx: TxnContext, config: RunConfig, *, call_id: int, attempt_number: int
) -> dict:
    surface.require_id("call_id", call_id)
    surface.require_id("attempt_number", attempt_number)

    call = _call_row(ctx.conn, call_id)
    _require_current_call(ctx, call)

    incomplete_count = _incomplete_attempt_count(ctx.conn, call_id)
    if attempt_number != incomplete_count + 1:
        raise _state_illegal(
            "attempt_number_mismatch",
            f"call {call_id} has {incomplete_count} incomplete attempts recorded; "
            f"expected attempt_number {incomplete_count + 1}, got {attempt_number}",
            call_id=call_id,
            expected=incomplete_count + 1,
            supplied=attempt_number,
        )
    if attempt_number > _UNANSWERED_ATTEMPT_THRESHOLD:
        raise _state_illegal(
            "three_attempt_limit",
            f"call {call_id} already has {incomplete_count} incomplete attempts recorded",
            call_id=call_id,
            recorded=incomplete_count,
        )

    ctx.conn.execute(
        "INSERT INTO _call_attempts (call_id, attempt_number, prompted_at, outcome, created_at) "
        "VALUES (?, ?, ?, 'incomplete', ?)",
        (call_id, attempt_number, ctx.now(), ctx.now()),
    )
    return {"call_id": call_id, "attempt_number": attempt_number}


def record_call_attempt_op(
    services: Services,
    config: RunConfig,
    *,
    call_id: int,
    attempt_number: int,
) -> bytes:
    try:
        surface.require_id("call_id", call_id)
        surface.require_id("attempt_number", attempt_number)
    except EngineError as exc:
        return exc.to_response()
    request_id = privileged_request_id("record_call_attempt", f"{call_id}:{attempt_number}")
    return privileged(
        services,
        request_id,
        lambda ctx: record_call_attempt(
            ctx, config, call_id=call_id, attempt_number=attempt_number
        ),
        require_active_invocation=True,
    )


def mark_call_unanswered(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    artifact: GeoArtifact | None = None,
) -> dict:
    surface.require_id("call_id", call_id)

    call = _call_row(ctx.conn, call_id)
    _require_current_call(ctx, call)

    incomplete_count = _incomplete_attempt_count(ctx.conn, call_id)
    if incomplete_count < _UNANSWERED_ATTEMPT_THRESHOLD:
        raise _state_illegal(
            "three_attempt_limit",
            f"call {call_id} has {incomplete_count} incomplete attempts recorded; "
            f"finalization requires {_UNANSWERED_ATTEMPT_THRESHOLD}",
            call_id=call_id,
            recorded=incomplete_count,
        )

    if call["call_kind"] == "new_job":
        calls.ordinary_rejection(ctx, config, call, terminal_reason="unanswered")
        return {"call_id": call_id, "outcome": "unanswered"}

    appointment_id = calls._required_int(
        call, "target_appointment_id", f"cancellation call {call_id}"
    )
    result = appointments.cancel_appointment(
        ctx, config,
        appointment_id=appointment_id, effective_at=int(call["arrived_at"]), artifact=artifact,
    )
    ctx.conn.execute(
        "UPDATE _calls SET status = 'unanswered', terminal_reason = 'unanswered', "
        "terminal_at = ? WHERE id = ?",
        (ctx.now(), call_id),
    )
    return {
        "call_id": call_id,
        "outcome": "unanswered",
        "appointment_id": appointment_id,
        "released_blocks": result["released_blocks"],
    }


def mark_call_unanswered_op(
    services: Services,
    config: RunConfig,
    *,
    call_id: int,
    artifact: GeoArtifact | None = None,
) -> bytes:
    try:
        surface.require_id("call_id", call_id)
    except EngineError as exc:
        return exc.to_response()
    request_id = privileged_request_id("mark_call_unanswered", str(call_id))
    return privileged(
        services,
        request_id,
        lambda ctx: mark_call_unanswered(ctx, config, call_id=call_id, artifact=artifact),
        require_active_invocation=True,
    )


def record_final_unanswered_attempt(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    attempt_number: int,
    artifact: GeoArtifact | None = None,
) -> dict:
    attempt = record_call_attempt(
        ctx, config, call_id=call_id, attempt_number=attempt_number
    )
    unanswered = mark_call_unanswered(ctx, config, call_id=call_id, artifact=artifact)
    return {**attempt, **unanswered}


def record_final_unanswered_attempt_op(
    services: Services,
    config: RunConfig,
    *,
    call_id: int,
    attempt_number: int,
    artifact: GeoArtifact | None = None,
) -> bytes:
    try:
        surface.require_id("call_id", call_id)
        surface.require_id("attempt_number", attempt_number)
    except EngineError as exc:
        return exc.to_response()
    request_id = privileged_request_id(
        "record_final_unanswered_attempt", f"{call_id}:{attempt_number}"
    )
    return privileged(
        services,
        request_id,
        lambda ctx: record_final_unanswered_attempt(
            ctx,
            config,
            call_id=call_id,
            attempt_number=attempt_number,
            artifact=artifact,
        ),
        require_active_invocation=True,
    )


__all__ = [
    "mark_call_unanswered",
    "mark_call_unanswered_op",
    "record_call_attempt",
    "record_call_attempt_op",
    "record_final_unanswered_attempt",
    "record_final_unanswered_attempt_op",
]
