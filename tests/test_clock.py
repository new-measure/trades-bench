from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from trades_bench.clock import (
    AFTER_HOURS,
    CALL_WINDOW_END,
    CALL_WINDOW_START,
    OFFICE_BLOCKS,
    OPENING_HOUR,
    OPENING_MINUTE,
    PHOENIX_TZ,
    Block,
    Clock,
    advance,
    now,
    now_service_date,
    phoenix_instant,
    phoenix_local,
    service_date_for_ms,
    to_iso8601,
)
from trades_bench.clock import (
    AFTER_HOURS as AH,
)
from trades_bench.db import (
    begin,
    commit,
    in_memory,
    insert_run,
    new_run_id,
    rollback,
)
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.migrations import migrate


def test_phoenix_is_fixed_offset_no_dst():
    summer = phoenix_local(phoenix_instant(2026, 7, 1, 12, 0))
    winter = phoenix_local(phoenix_instant(2027, 1, 1, 12, 0))
    assert summer.utcoffset() == winter.utcoffset()
    assert summer.utcoffset().total_seconds() == -7 * 3600


def test_iso8601_has_explicit_offset():
    ts = phoenix_instant(2026, 10, 1, 6, 45)
    assert to_iso8601(ts) == "2026-10-01T06:45:00-07:00"
    assert to_iso8601(phoenix_instant(2026, 10, 1, 12, 0)) == "2026-10-01T12:00:00-07:00"


def test_opening_is_06_45():
    assert (OPENING_HOUR, OPENING_MINUTE) == (6, 45)


def test_block_grid_contiguous_five_plus_after_hours():
    assert OFFICE_BLOCKS == (
        (7 * 60, 9 * 60),
        (9 * 60, 11 * 60),
        (11 * 60, 13 * 60),
        (13 * 60, 15 * 60),
        (15 * 60, 17 * 60),
    )
    for prev, cur in zip(OFFICE_BLOCKS, OFFICE_BLOCKS[1:], strict=False):
        assert prev[1] == cur[0]
    assert OFFICE_BLOCKS[-1][1] == AFTER_HOURS[0]
    assert CALL_WINDOW_START == 7 * 60 and CALL_WINDOW_END == 19 * 60


def test_block_of_classifies_each_grid_minute():
    def blk(hh, mm):
        return Block.from_minutes(hh * 60 + mm)

    assert blk(7, 0) is Block.BLOCK_1
    assert blk(8, 59) is Block.BLOCK_1
    assert blk(9, 0) is Block.BLOCK_2
    assert blk(11, 0) is Block.BLOCK_3
    assert blk(13, 0) is Block.BLOCK_4
    assert blk(15, 0) is Block.BLOCK_5
    assert blk(17, 0) is Block.AFTER_HOURS
    assert blk(18, 59) is Block.AFTER_HOURS
    assert blk(6, 45) is None
    assert blk(19, 0) is None
    assert blk(2, 0) is None
    assert Block.office_blocks() == tuple(range(1, 6))


def test_clock_now_frozen_and_monotonic():
    c = Clock(phoenix_instant(2026, 10, 1, 6, 45))
    assert c.now() == phoenix_instant(2026, 10, 1, 6, 45)
    assert c.now() == c.now()
    c.advance_to(phoenix_instant(2026, 10, 1, 7, 0))
    assert c.now() == phoenix_instant(2026, 10, 1, 7, 0)


def test_clock_refuses_to_go_backwards():
    c = Clock(phoenix_instant(2026, 10, 1, 7, 0))
    with pytest.raises(AssertionError):
        c.advance_to(phoenix_instant(2026, 10, 1, 6, 0))


def test_clock_local_date_and_weekend():
    c = Clock(phoenix_instant(2026, 10, 2, 12, 0))
    assert c.local_date() == date(2026, 10, 2)
    assert c.is_weekend() is False
    c.advance_to(phoenix_instant(2026, 10, 3, 12, 0))
    assert c.is_weekend() is True
    c.advance_to(phoenix_instant(2026, 10, 5, 8, 0))
    assert c.is_weekend() is False


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
    ts = phoenix_instant(2027, 2, 9, 17, 30)
    dt = phoenix_local(ts)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2027, 2, 9, 17, 30)
    assert dt.tzinfo is PHOENIX_TZ
    assert ts == int(datetime(2027, 2, 9, 17, 30, tzinfo=PHOENIX_TZ).timestamp() * 1000)
    assert phoenix_local(ts).astimezone(UTC).tzinfo is UTC


def test_alias_matches():
    assert AH is AFTER_HOURS


OPENING = phoenix_instant(2026, 10, 1, 6, 45)


@pytest.fixture
def conn():
    c = in_memory()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, start_at_ms=OPENING) -> None:
    begin(conn)
    insert_run(
        conn,
        run_id=new_run_id(),
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=start_at_ms,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
    )
    commit(conn)


def test_service_date_for_ms_known_instant():
    assert service_date_for_ms(phoenix_instant(2026, 10, 1, 6, 45)) == "2026-10-01"
    assert service_date_for_ms(phoenix_instant(2026, 12, 25, 19, 0)) == "2026-12-25"


def test_service_date_for_ms_near_midnight():
    last_instant = phoenix_instant(2026, 10, 1, 23, 59)
    assert service_date_for_ms(last_instant) == "2026-10-01"
    next_instant = phoenix_instant(2026, 10, 2, 0, 1)
    assert service_date_for_ms(next_instant) == "2026-10-02"
    assert to_iso8601(next_instant) == "2026-10-02T00:01:00-07:00"


def test_clock_db_now_reads_the_seeded_singleton(conn):
    _seed(conn)
    assert now(conn) == OPENING
    assert now_service_date(conn) == "2026-10-01"


def test_clock_db_now_without_seeded_run_raises(conn):
    with pytest.raises(EngineError) as ei:
        now(conn)
    assert ei.value.code is ErrorCode.ENTITY_NOT_FOUND
    with pytest.raises(EngineError) as ei2:
        now_service_date(conn)
    assert ei2.value.code is ErrorCode.ENTITY_NOT_FOUND


def test_clock_db_advance_moves_forward_and_derives_service_date(conn):
    _seed(conn)
    later = phoenix_instant(2026, 10, 1, 7, 0)
    begin(conn)
    advance(conn, later)
    commit(conn)
    assert now(conn) == later
    assert now_service_date(conn) == "2026-10-01"

    next_day = phoenix_instant(2026, 10, 2, 8, 0)
    begin(conn)
    advance(conn, next_day)
    commit(conn)
    assert now_service_date(conn) == "2026-10-02"


def test_clock_db_advance_accepts_explicit_service_date(conn):
    _seed(conn)
    begin(conn)
    advance(conn, OPENING, service_date="2026-10-01")
    commit(conn)
    assert now(conn) == OPENING
    assert now_service_date(conn) == "2026-10-01"


def test_clock_db_advance_refuses_backwards(conn):
    _seed(conn)
    begin(conn)
    with pytest.raises(EngineError) as ei:
        advance(conn, OPENING - 1)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    assert now(conn) == OPENING
    commit(conn)


def test_clock_db_advance_requires_open_transaction(conn):
    _seed(conn)
    with pytest.raises(EngineError) as ei:
        advance(conn, OPENING + 1)
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    assert now(conn) == OPENING


def test_clock_db_rollback_discards_the_time_change(conn):
    _seed(conn)
    begin(conn)
    advance(conn, phoenix_instant(2026, 10, 1, 9, 0))
    rollback(conn)
    assert now(conn) == OPENING
