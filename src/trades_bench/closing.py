from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import date
from typing import Any

from . import clock, constants
from .enums import InvocationType
from .errors import EngineError, ErrorCode

RESTRICTED_MESSAGE = "the closing invocation accepts only changes that take effect after it"

_BLOCK_START_MINUTES_BY_LABEL = {
    f"{start // 60:02d}:{start % 60:02d}": start for start, _end in constants.BLOCK_GRID_MINUTES
}


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _booking_target_ms(conn: sqlite3.Connection, args: Mapping[str, object]) -> int | None:
    minutes = _BLOCK_START_MINUTES_BY_LABEL.get(str(args.get("block_start")))
    if minutes is None:
        return None
    try:
        day = date.fromisoformat(str(args.get("service_date")))
    except ValueError:
        return None
    return clock.phoenix_instant(day.year, day.month, day.day, minutes // 60, minutes % 60)


def _assignment_target_ms(conn: sqlite3.Connection, args: Mapping[str, object]) -> int | None:
    appointment_id = _as_int(args.get("appointment_id"))
    if appointment_id is None:
        return None
    row = conn.execute(
        "SELECT MIN(block_start) AS first_start FROM _appointment_blocks "
        "WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()
    if row is None or row["first_start"] is None:
        return None
    return int(row["first_start"])


FUTURE_TARGET_RESOLVERS = {
    "jobs.book": _booking_target_ms,
    "dispatch.assign": _assignment_target_ms,
}

PRESENT_TARGET_OPERATIONS = frozenset(
    {
        "appointments.acknowledge_cancellation",
        "calls.reject",
        "crm.create_customer",
        "crm.create_location",
    }
)


def enforce(ctx: Any, operation: str, args: Mapping[str, object]) -> None:
    invocation = ctx.invocation
    if invocation is None or invocation["type"] != InvocationType.CLOSING.value:
        return
    closing_at = int(invocation["ts_ms"])
    resolver = FUTURE_TARGET_RESOLVERS.get(operation)
    target_ms = closing_at if resolver is None else resolver(ctx.conn, args)
    if target_ms is None or int(target_ms) <= closing_at:
        raise EngineError(ErrorCode.CLOSING_RESTRICTED, RESTRICTED_MESSAGE)


__all__ = [
    "FUTURE_TARGET_RESOLVERS",
    "PRESENT_TARGET_OPERATIONS",
    "RESTRICTED_MESSAGE",
    "enforce",
]
