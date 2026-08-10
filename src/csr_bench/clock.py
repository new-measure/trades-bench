"""Phoenix-aware simulated clock (spec 01 §Run identity and time, §Canonical
primitive types, §Scheduling and execution).

All engine timestamps are **simulated time** stored as UTC epoch milliseconds.
Wall clock never enters the database. The controller owns ``current_time``;
agent actions never advance it and the number of tool calls or reasoning turns
never changes world time (00-decisions §Daily interaction loop). ``now()`` is
the frozen instant at which the active invocation (or the next world event) is
being processed; it advances only when the orchestrator pops the next event
from the chronological queue (spec 07).

``America/Phoenix`` observes no daylight-saving transition, but the timezone
must still be explicit in storage and in API output.

The grid / opening / call-window values are the single source of truth from
:mod:`csr_bench.constants`; re-exported here for convenience and so callers
that already import the clock keep a stable surface.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from enum import IntEnum
from zoneinfo import ZoneInfo

from .constants import (
    AFTER_HOURS_BLOCK_INDEX,
    BLOCK_GRID_MINUTES,
    CALL_WINDOW_END_MIN,
    CALL_WINDOW_START_MIN,
    OPENING_HOUR,
    OPENING_MINUTE,
)
from .errors import EngineError, ErrorCode

#: The run calendar timezone (00-decisions §Daily interaction loop / §Warm start).
PHOENIX_TZ = ZoneInfo("America/Phoenix")

#: Call-window aliases for clock callers (sourced from constants).
CALL_WINDOW_START = CALL_WINDOW_START_MIN
CALL_WINDOW_END = CALL_WINDOW_END_MIN

#: The contiguous office block grid plus after-hours, as minute pairs. The fifth
#: entry (index ``AFTER_HOURS_BLOCK_INDEX``) is the 17:00–19:00 after-hours block.
OFFICE_BLOCKS: tuple[tuple[int, int], ...] = tuple(
    BLOCK_GRID_MINUTES[:AFTER_HOURS_BLOCK_INDEX]  # five office blocks
)
AFTER_HOURS: tuple[int, int] = BLOCK_GRID_MINUTES[AFTER_HOURS_BLOCK_INDEX]


class Block(IntEnum):
    """The seven service-day block grid. Office blocks 1–5 plus after-hours."""

    BLOCK_1 = 1
    BLOCK_2 = 2
    BLOCK_3 = 3
    BLOCK_4 = 4
    BLOCK_5 = 5
    AFTER_HOURS = 6

    @classmethod
    def office_blocks(cls) -> tuple[Block, ...]:
        return (cls.BLOCK_1, cls.BLOCK_2, cls.BLOCK_3, cls.BLOCK_4, cls.BLOCK_5)

    @classmethod
    def from_minutes(cls, minutes_from_midnight: int) -> Block | None:
        """Classify a Phoenix local minute-of-day into a block.

        Installs never start after 13:00 (decision 3); HVAC installation reserves
        two consecutive office blocks and never starts after 13:00. That install
        rule is enforced by scheduling (CSR-006); this classification only names
        which block a timestamp falls in (``None`` outside the grid).
        """
        for idx, (start, end) in enumerate(OFFICE_BLOCKS, start=1):
            if start <= minutes_from_midnight < end:
                return cls(idx)  # type: ignore[call-arg]
        if AFTER_HOURS[0] <= minutes_from_midnight < AFTER_HOURS[1]:
            return cls.AFTER_HOURS
        return None


def phoenix_local(ts_epoch_ms: int) -> datetime:
    """Convert a UTC epoch-millisecond timestamp to Phoenix local wall-clock.

    Returns an aware ``datetime``; never uses wall-clock time.
    """
    return datetime.fromtimestamp(ts_epoch_ms / 1000, tz=PHOENIX_TZ)


def phoenix_instant(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """Build a UTC epoch-millisecond timestamp from Phoenix-local components.

    The canonical way to build a simulated timestamp: never from wall clock.
    """
    dt = datetime(year, month, day, hour, minute, tzinfo=PHOENIX_TZ)
    return int(dt.timestamp() * 1000)


def to_iso8601(ts_epoch_ms: int) -> str:
    """Render a timestamp as ISO 8601 with the explicit Phoenix offset.

    America/Phoenix is -07:00 year-round (no DST), so the offset is stable.
    """
    dt = phoenix_local(ts_epoch_ms)
    return dt.isoformat(timespec="seconds")


def service_date_for_ms(ts_epoch_ms: int) -> str:
    """The Phoenix local ``YYYY-MM-DD`` service date of a timestamp.

    Calendar grouping is Phoenix-local (spec 01 §Canonical primitive types:
    timestamps are stored as UTC epoch milliseconds *plus* the Phoenix local
    date wherever calendar grouping is required), so the date is computed from
    the local wall clock, never from UTC.
    """
    return phoenix_local(ts_epoch_ms).date().isoformat()


# --- DB-backed current time (CSR-002 decision 3) ----------------------------
# `_clock(singleton, current_time_ms, service_date)` is the single time source:
# `_run` keeps only its immutable boundary fields. These helpers read/write the
# clock row; the in-memory Clock class below remains the orchestrator's per-day
# object and is untouched.


def now(conn: sqlite3.Connection) -> int:
    """The run's current simulated time, as UTC epoch milliseconds."""
    row = conn.execute(
        "SELECT current_time_ms FROM _clock WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            "clock singleton row not found — insert_run seeds it at run creation",
        )
    return int(row["current_time_ms"])


def now_service_date(conn: sqlite3.Connection) -> str:
    """The run's current Phoenix-local ``YYYY-MM-DD`` service date."""
    row = conn.execute(
        "SELECT service_date FROM _clock WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            "clock singleton row not found — insert_run seeds it at run creation",
        )
    return str(row["service_date"])


def advance(conn: sqlite3.Connection, millis: int, service_date: str | None = None) -> None:
    """Advance the clock row to ``millis``; time never decreases.

    ``service_date`` is stored alongside the timestamp (spec 01: UTC epoch
    milliseconds *plus* the Phoenix local date); when omitted it is derived
    from ``millis`` via :func:`service_date_for_ms`.

    The update happens inside the caller's open transaction — guarded like
    every write helper in ``db.py``, so a rollback discards the time change —
    and refuses to move backwards (spec 01 invariants #3, mirroring
    :meth:`Clock.advance_to`'s monotonicity) with a structured error rather
    than an assertion.
    """
    if not conn.in_transaction:
        raise EngineError(
            ErrorCode.DRAW_OUTSIDE_TXN,
            "clock.advance is callable only inside an open transaction",
        )
    current = now(conn)
    millis = int(millis)
    if millis < current:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"clock must not go backwards: {millis} < {current}",
        )
    if service_date is None:
        service_date = service_date_for_ms(millis)
    conn.execute(
        "UPDATE _clock SET current_time_ms = ?, service_date = ? WHERE singleton = 1",
        (millis, service_date),
    )


class Clock:
    """The simulated engine clock, advanced only by the orchestrator path.

    Domain modules receive a clock and read ``now()``; only the orchestrator
    calls :meth:`advance_to`. ``current_time`` is monotonically non-decreasing
    (spec 01 invariants #3) and frozen while an invocation is active.
    """

    __slots__ = ("current_millis",)

    def __init__(self, start_millis: int) -> None:
        self.current_millis: int = int(start_millis)

    # --- read API (domain-facing) ------------------------------------------
    def now(self) -> int:
        """The frozen current simulated time as UTC epoch milliseconds."""
        return self.current_millis

    def now_iso(self) -> str:
        return to_iso8601(self.current_millis)

    def local_date(self, ts: int | None = None) -> date:
        """The Phoenix local calendar date of ``ts`` (default: now)."""
        ts = self.current_millis if ts is None else int(ts)
        return phoenix_local(ts).date()

    def is_weekend(self, d: date | None = None) -> bool:
        """True for Saturday / Sunday in the Phoenix calendar."""
        d = self.local_date() if d is None else d
        return d.weekday() >= 5  # Mon=0 .. Sun=6

    def block_of(self, ts: int | None = None) -> Block | None:
        """Which office/after-hours block ``ts`` falls in, or ``None``."""
        ts = self.current_millis if ts is None else int(ts)
        dt = phoenix_local(ts)
        minutes = dt.hour * 60 + dt.minute
        return Block.from_minutes(minutes)

    def day_start_millis(self, d: date | None = None) -> int:
        """Epoch millis of midnight (Phoenix local) starting service day ``d``."""
        d = self.local_date() if d is None else d
        return phoenix_instant(d.year, d.month, d.day, 0, 0)

    # --- orchestrator-only advance -----------------------------------------
    def advance_to(self, ts_epoch_ms: int) -> None:
        """Advance the clock to ``ts``; time never decreases (invariant #3)."""
        ts = int(ts_epoch_ms)
        if ts < self.current_millis:
            raise AssertionError(f"clock must not go backwards: {ts} < {self.current_millis}")
        self.current_millis = ts

    def copy(self) -> Clock:
        return Clock(self.current_millis)


__all__ = [
    "AFTER_HOURS",
    "Block",
    "CALL_WINDOW_END",
    "CALL_WINDOW_START",
    "Clock",
    "OFFICE_BLOCKS",
    "OPENING_HOUR",
    "OPENING_MINUTE",
    "PHOENIX_TZ",
    "advance",
    "now",
    "now_service_date",
    "phoenix_instant",
    "phoenix_local",
    "service_date_for_ms",
    "to_iso8601",
]
