from __future__ import annotations

import sqlite3

import pytest

from _helpers import make_services, register_conn
from csr_bench.db import begin, commit, connect, insert_run, new_run_id, rollback
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.rng import Rng, sequential_draw, stream_seed
from csr_bench.txn import command

MASTER = 0x0123456789ABCDEF
SPEC = "1.0.0"
STREAM = "weather_process"
SEED = stream_seed(MASTER, SPEC, STREAM)


def _new_run(path) -> tuple[sqlite3.Connection, str]:
    conn = _open(path)
    rid = new_run_id()
    begin(conn)
    insert_run(
        conn,
        run_id=rid,
        spec_version=SPEC,
        config_version="1.0.0",
        config_hash="h",
        master_seed=MASTER,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
    )
    commit(conn)
    return conn, rid


def _open(path) -> sqlite3.Connection:
    conn = connect(path)
    register_conn(conn)
    migrate(conn)
    return conn


def _state_row(conn, stream=STREAM):
    return conn.execute(
        "SELECT seed, counter FROM _rng_state WHERE stream_name=?", (stream,)
    ).fetchone()


def _committed_draws(conn, rng, n: int) -> list[int]:
    out = []
    for _ in range(n):
        begin(conn)
        rng.enter_txn()
        out.append(rng.stream(STREAM).next())
        rng.exit_txn()
        commit(conn)
    return out


def test_committed_advance_persists_seed_and_counter(tmp_path):
    conn, rid = _new_run(tmp_path / "a.db")
    rng = Rng(MASTER, SPEC, conn=conn)
    assert _state_row(conn) is None

    for i in range(2):
        begin(conn)
        rng.enter_txn()
        u = rng.stream(STREAM).next()
        rng.exit_txn()
        commit(conn)
        assert u == sequential_draw(SEED, i)
        row = _state_row(conn)
        assert row["seed"] == SEED.hex()
        assert row["counter"] == i + 1

    begin(conn)
    rng.enter_txn()
    st = rng.stream(STREAM)
    assert [st.next(), st.next()] == [sequential_draw(SEED, 2), sequential_draw(SEED, 3)]
    rng.exit_txn()
    commit(conn)
    assert _state_row(conn)["counter"] == 4


def test_only_the_advanced_stream_gets_a_row(tmp_path):
    conn, rid = _new_run(tmp_path / "one.db")
    rng = Rng(MASTER, SPEC, conn=conn)
    _committed_draws(conn, rng, 1)
    names = [
        r[0]
        for r in conn.execute(
            "SELECT stream_name FROM _rng_state ORDER BY stream_name"
        )
    ]
    assert names == [STREAM]


def test_rollback_discards_the_counter_advance(tmp_path):
    conn, rid = _new_run(tmp_path / "b.db")
    rng = Rng(MASTER, SPEC, conn=conn)
    _committed_draws(conn, rng, 1)
    assert _state_row(conn)["counter"] == 1

    begin(conn)
    rng.enter_txn()
    rolled_back = rng.stream(STREAM).next()
    assert _state_row(conn)["counter"] == 2
    rng.exit_txn()
    rollback(conn)

    assert rolled_back == sequential_draw(SEED, 1)
    assert _state_row(conn)["counter"] == 1
    fresh = Rng(MASTER, SPEC, conn=conn)
    assert fresh.seq_state()[STREAM] == (SEED.hex(), 1)

    assert _committed_draws(conn, rng, 1) == [rolled_back]
    assert _state_row(conn)["counter"] == 2


def test_fresh_connection_and_rng_resume_the_stream(tmp_path):
    path = tmp_path / "resume.db"
    conn, rid = _new_run(path)
    interrupted = _committed_draws(conn, Rng(MASTER, SPEC, conn=conn), 3)
    conn.close()

    conn2 = _open(path)
    rng2 = Rng(MASTER, SPEC, conn=conn2)
    assert rng2.seq_state()[STREAM] == (SEED.hex(), 3)
    interrupted += _committed_draws(conn2, rng2, 4)

    conn_b, rid_b = _new_run(tmp_path / "straight.db")
    straight = _committed_draws(conn_b, Rng(MASTER, SPEC, conn=conn_b), 7)

    assert interrupted == straight
    assert straight == [sequential_draw(SEED, c) for c in range(7)]
    assert _state_row(conn2)["counter"] == _state_row(conn_b)["counter"] == 7


def test_advance_outside_an_open_txn_is_refused(tmp_path):
    conn, rid = _new_run(tmp_path / "d.db")
    rng = Rng(MASTER, SPEC, conn=conn)

    with pytest.raises(EngineError) as ei:
        rng.stream(STREAM).next()
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN

    rng.enter_txn()
    with pytest.raises(EngineError) as ei:
        rng.stream(STREAM).next()
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    rng.exit_txn()

    assert _state_row(conn) is None


def test_restore_outside_an_open_txn_is_refused(tmp_path):
    conn, rid = _new_run(tmp_path / "d2.db")
    rng = Rng(MASTER, SPEC, conn=conn)
    snapshot = {STREAM: (SEED.hex(), 5)}

    with pytest.raises(EngineError) as ei:
        rng.restore_seq_state(snapshot)
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    assert _state_row(conn) is None

    rng.enter_txn()
    with pytest.raises(EngineError) as ei:
        rng.restore_seq_state(snapshot)
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    rng.exit_txn()

    assert _state_row(conn) is None
    fresh = Rng(MASTER, SPEC, conn=conn)
    assert fresh.seq_state() == {}


def test_restore_rejects_a_seed_from_a_different_master_seed(tmp_path):
    conn, rid = _new_run(tmp_path / "d3.db")
    rng = Rng(MASTER, SPEC, conn=conn)
    foreign_seed = stream_seed(MASTER + 1, SPEC, STREAM)
    bad_snapshot = {STREAM: (foreign_seed.hex(), 3)}

    begin(conn)
    rng.enter_txn()
    with pytest.raises(EngineError) as ei:
        rng.restore_seq_state(bad_snapshot)
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED
    rng.exit_txn()
    rollback(conn)

    assert _state_row(conn) is None
    fresh = Rng(MASTER, SPEC, conn=conn)
    assert fresh.seq_state() == {}


def test_state_resyncs_after_rollback(tmp_path):
    conn, rid = _new_run(tmp_path / "d4.db")
    rng = Rng(MASTER, SPEC, conn=conn)
    _committed_draws(conn, rng, 1)

    begin(conn)
    rng.enter_txn()
    st = rng.stream(STREAM)
    st.next()
    rng.exit_txn()
    rollback(conn)

    assert st.state() == (SEED.hex(), 1)
    assert _state_row(conn)["counter"] == 1


def test_unbound_rng_needs_no_database(tmp_path):
    rng = Rng(MASTER, SPEC)
    rng.enter_txn()
    assert rng.stream(STREAM).next() == sequential_draw(SEED, 0)
    rng.exit_txn()
    conn, rid = _new_run(tmp_path / "half.db")
    bound = Rng(MASTER, SPEC, conn=conn)
    assert bound.seq_state() == {}
    assert Rng(MASTER, SPEC).seq_state() == {}


def test_counter_follows_the_command_transaction():
    conn, rid, sv, _, _, rng = make_services(active_invocation=True, master_seed=7)
    seed = stream_seed(7, "1.0.0", STREAM)

    drawn: list[int] = []

    def advance(ctx):
        drawn.append(ctx.rng.stream(STREAM).next())
        return {"ok": True}

    def advance_then_fail(ctx):
        drawn.append(ctx.rng.stream(STREAM).next())
        raise EngineError(ErrorCode.STATE_ILLEGAL, "domain check failed after the draw")

    first = command(sv, "req-1", advance)
    assert drawn == [sequential_draw(seed, 0)]
    assert _state_row(conn)["counter"] == 1

    command(sv, "req-2", advance_then_fail)
    assert drawn[-1] == sequential_draw(seed, 1)
    assert _state_row(conn)["counter"] == 1

    assert command(sv, "req-1", advance) == first
    assert len(drawn) == 2
    assert _state_row(conn)["counter"] == 1

    command(sv, "req-3", advance)
    assert drawn[-1] == sequential_draw(seed, 1)
    assert _state_row(conn)["counter"] == 2


def test_persisted_seed_must_match_the_runs_master_seed(tmp_path):
    conn, rid = _new_run(tmp_path / "e.db")
    _committed_draws(conn, Rng(MASTER, SPEC, conn=conn), 1)
    conn.execute("UPDATE _rng_state SET seed=? WHERE stream_name=?", ("00" * 32, STREAM))
    with pytest.raises(EngineError) as ei:
        Rng(MASTER, SPEC, conn=conn)
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED
