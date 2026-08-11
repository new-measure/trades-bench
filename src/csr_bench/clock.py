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

PHOENIX_TZ = ZoneInfo("America/Phoenix")

CALL_WINDOW_START = CALL_WINDOW_START_MIN
CALL_WINDOW_END = CALL_WINDOW_END_MIN

OFFICE_BLOCKS: tuple[tuple[int, int], ...] = tuple(
    BLOCK_GRID_MINUTES[:AFTER_HOURS_BLOCK_INDEX]
)
AFTER_HOURS: tuple[int, int] = BLOCK_GRID_MINUTES[AFTER_HOURS_BLOCK_INDEX]


class Block(IntEnum):

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
        for idx, (start, end) in enumerate(OFFICE_BLOCKS, start=1):
            if start <= minutes_from_midnight < end:
                return cls(idx)  # type: ignore[call-arg]
        if AFTER_HOURS[0] <= minutes_from_midnight < AFTER_HOURS[1]:
            return cls.AFTER_HOURS
        return None


def phoenix_local(ts_epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_epoch_ms / 1000, tz=PHOENIX_TZ)


def phoenix_instant(year: int, month: int, day: int, hour: int, minute: int) -> int:
    dt = datetime(year, month, day, hour, minute, tzinfo=PHOENIX_TZ)
    return int(dt.timestamp() * 1000)


def to_iso8601(ts_epoch_ms: int) -> str:
    dt = phoenix_local(ts_epoch_ms)
    return dt.isoformat(timespec="seconds")


def service_date_for_ms(ts_epoch_ms: int) -> str:
    return phoenix_local(ts_epoch_ms).date().isoformat()


def now(conn: sqlite3.Connection) -> int:
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

    __slots__ = ("current_millis",)

    def __init__(self, start_millis: int) -> None:
        self.current_millis: int = int(start_millis)

    def now(self) -> int:
        return self.current_millis

    def now_iso(self) -> str:
        return to_iso8601(self.current_millis)

    def local_date(self, ts: int | None = None) -> date:
        ts = self.current_millis if ts is None else int(ts)
        return phoenix_local(ts).date()

    def is_weekend(self, d: date | None = None) -> bool:
        d = self.local_date() if d is None else d
        return d.weekday() >= 5

    def block_of(self, ts: int | None = None) -> Block | None:
        ts = self.current_millis if ts is None else int(ts)
        dt = phoenix_local(ts)
        minutes = dt.hour * 60 + dt.minute
        return Block.from_minutes(minutes)

    def day_start_millis(self, d: date | None = None) -> int:
        d = self.local_date() if d is None else d
        return phoenix_instant(d.year, d.month, d.day, 0, 0)

    def advance_to(self, ts_epoch_ms: int) -> None:
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
