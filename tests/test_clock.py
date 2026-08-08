"""Tests for the Phoenix-aware simulated clock (spec 01 §Run identity and time,
§Canonical primitive types, §Scheduling and execution)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from csr_bench.clock import (
    AFTER_HOURS,
    CALL_WINDOW_END,
    CALL_WINDOW_START,
    OFFICE_BLOCKS,
    OPENING_HOUR,
    OPENING_MINUTE,
    PHOENIX_TZ,
    Block,
    Clock,
    phoenix_instant,
    phoenix_local,
    to_iso8601,
)
from csr_bench.clock import (
    AFTER_HOURS as AH,
)


def test_phoenix_is_fixed_offset_no_dst():
    # America/Phoenix is -07:00 year-round; no DST across a summer/winter check.
    summer = phoenix_local(phoenix_instant(2026, 7, 1, 12, 0))
    winter = phoenix_local(phoenix_instant(2027, 1, 1, 12, 0))
    assert summer.utcoffset() == winter.utcoffset()
    assert summer.utcoffset().total_seconds() == -7 * 3600


def test_iso8601_has_explicit_offset():
    ts = phoenix_instant(2026, 10, 1, 6, 45)
    assert to_iso8601(ts) == "2026-10-01T06:45:00-07:00"
    # noon is unambiguous with offset.
    assert to_iso8601(phoenix_instant(2026, 10, 1, 12, 0)) == "2026-10-01T12:00:00-07:00"


def test_opening_is_06_45():
    assert (OPENING_HOUR, OPENING_MINUTE) == (6, 45)


def test_block_grid_contiguous_five_plus_after_hours():
    # Five office blocks contiguous 07:00..17:00, after-hours 17:00..19:00.
    assert OFFICE_BLOCKS == (
        (7 * 60, 9 * 60),
        (9 * 60, 11 * 60),
        (11 * 60, 13 * 60),
        (13 * 60, 15 * 60),
        (15 * 60, 17 * 60),
    )
    # No gaps: each block starts where the previous ended (sliding window of
    # adjacent pairs, so the two iterables are intentionally unequal length).
    for prev, cur in zip(OFFICE_BLOCKS, OFFICE_BLOCKS[1:], strict=False):
        assert prev[1] == cur[0]
    assert OFFICE_BLOCKS[-1][1] == AFTER_HOURS[0]
    assert CALL_WINDOW_START == 7 * 60 and CALL_WINDOW_END == 19 * 60


def test_block_of_classifies_each_grid_minute():
    def blk(hh, mm):
        return Block.from_minutes(hh * 60 + mm)

    # Boundaries belong to the block that starts at that minute.
    assert blk(7, 0) is Block.BLOCK_1
    assert blk(8, 59) is Block.BLOCK_1
    assert blk(9, 0) is Block.BLOCK_2
    assert blk(11, 0) is Block.BLOCK_3
    assert blk(13, 0) is Block.BLOCK_4
    assert blk(15, 0) is Block.BLOCK_5
    assert blk(17, 0) is Block.AFTER_HOURS
    assert blk(18, 59) is Block.AFTER_HOURS
    # Outside the grid -> None.
    assert blk(6, 45) is None  # opening is before the first block
    assert blk(19, 0) is None
    assert blk(2, 0) is None
    assert Block.office_blocks() == tuple(range(1, 6))  # 1..5


def test_clock_now_frozen_and_monotonic():
    c = Clock(phoenix_instant(2026, 10, 1, 6, 45))
    assert c.now() == phoenix_instant(2026, 10, 1, 6, 45)
    # now() is stable across repeated calls (frozen, not wall-clock).
    assert c.now() == c.now()
    c.advance_to(phoenix_instant(2026, 10, 1, 7, 0))
    assert c.now() == phoenix_instant(2026, 10, 1, 7, 0)


def test_clock_refuses_to_go_backwards():
    c = Clock(phoenix_instant(2026, 10, 1, 7, 0))
    with pytest.raises(AssertionError):
        c.advance_to(phoenix_instant(2026, 10, 1, 6, 0))


def test_clock_local_date_and_weekend():
    # 2026-10-02 is a Friday, 2026-10-03 a Saturday (Phoenix). Advance forward.
    c = Clock(phoenix_instant(2026, 10, 2, 12, 0))
    assert c.local_date() == date(2026, 10, 2)
    assert c.is_weekend() is False
    c.advance_to(phoenix_instant(2026, 10, 3, 12, 0))
    assert c.is_weekend() is True
    c.advance_to(phoenix_instant(2026, 10, 5, 8, 0))
    assert c.is_weekend() is False  # Monday


def test_clock_block_of_now():
    c = Clock(phoenix_instant(2026, 10, 1, 10, 30))
    assert c.block_of() is Block.BLOCK_2


def test_day_start_millis_is_midnight_phoenix():
    c = Clock(phoenix_instant(2026, 10, 1, 14, 0))
    start = c.day_start_millis()
    local = phoenix_local(start)
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert local.date() == date(2026, 10, 1)


def test_clock_is_independent_of_wall_clock():
    # Building a timestamp from components must not depend on the system clock;
    # verifying the same instant round-trips to the same components.
    ts = phoenix_instant(2027, 2, 9, 17, 30)
    dt = phoenix_local(ts)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2027, 2, 9, 17, 30)
    assert dt.tzinfo is PHOENIX_TZ
    # And the epoch millis are deterministic (no wall clock).
    assert ts == int(datetime(2027, 2, 9, 17, 30, tzinfo=PHOENIX_TZ).timestamp() * 1000)
    # sanity: tzinfo is real (UTC comparison)
    assert phoenix_local(ts).astimezone(UTC).tzinfo is UTC


# alias to keep the unused AH import meaningful for lint
def test_alias_matches():
    assert AH is AFTER_HOURS
