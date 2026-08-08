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
    "phoenix_instant",
    "phoenix_local",
    "to_iso8601",
]
