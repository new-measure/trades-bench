from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from trades_bench.errors import EngineError, ErrorCode
from trades_bench.rng import (
    Rng,
    bounded_int,
    chance,
    choice,
    keyed_draw,
    randint,
    sequential_draw,
    stream_seed,
    triangular,
    uniform01,
)

MASTER = 0x0123456789ABCDEF
SPEC = "1.0.0"


def _lp(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(2, "big") + b


def _u8(n: int) -> bytes:
    return n.to_bytes(8, "big")


def test_stream_seed_formula_pinned_exactly():
    name = "acceptance"
    expected = hashlib.sha256(MASTER.to_bytes(8, "big") + _lp(SPEC) + _lp(name)).digest()
    assert stream_seed(MASTER, SPEC, name) == expected


def test_stream_seed_changes_with_each_input():
    a = stream_seed(MASTER, SPEC, "demand")
    b = stream_seed(MASTER, SPEC, "acceptance")
    c = stream_seed(MASTER + 1, SPEC, "acceptance")
    d = stream_seed(MASTER, "1.0.1", "acceptance")
    assert a != b != c != d
    assert len(a) == 32


def test_keyed_draw_formula_pinned_exactly():
    name = "weather_forecast"
    seed = stream_seed(MASTER, SPEC, name)
    et, eid, scp, kind, di = "appointment", 7, "2026-10-01", "work_completed", 0
    body = seed + _lp(et) + _u8(eid) + _lp(scp) + _lp("work_completed") + _u8(di)
    expected = int.from_bytes(hashlib.sha256(body).digest()[:8], "big")
    assert keyed_draw(seed, et, eid, scp, kind, di) == expected
    assert 0 <= expected < (1 << 64)


def test_sequential_draw_formula_pinned():
    seed = stream_seed(MASTER, SPEC, "warm_start")
    for c in (0, 1, 42):
        expected = int.from_bytes(hashlib.sha256(seed + _u8(c)).digest()[:8], "big")
        assert sequential_draw(seed, c) == expected


def test_keyed_draw_is_pure_and_order_independent():
    seed = stream_seed(MASTER, SPEC, "acceptance")
    a = keyed_draw(seed, "appointment", 7, "2026-10-01", "work_completed", 0)
    for _ in range(5):
        keyed_draw(seed, "appointment", 999, "2026-10-02", "work_completed", 3)
        keyed_draw(seed, "technician", 7, "2026-10-01", "work_completed", 0)
    assert keyed_draw(seed, "appointment", 7, "2026-10-01", "work_completed", 0) == a


def test_each_key_component_changes_the_outcome():
    seed = stream_seed(MASTER, SPEC, "acceptance")
    base = keyed_draw(seed, "A", 1, "s", "k", 0)
    assert keyed_draw(seed, "B", 1, "s", "k", 0) != base
    assert keyed_draw(seed, "A", 2, "s", "k", 0) != base
    assert keyed_draw(seed, "A", 1, "t", "k", 0) != base
    assert keyed_draw(seed, "A", 1, "s", "l", 0) != base
    assert keyed_draw(seed, "A", 1, "s", "k", 1) != base


def test_sequential_draw_advances_counter():
    seed = stream_seed(MASTER, SPEC, "warm_start")
    a = sequential_draw(seed, 0)
    b = sequential_draw(seed, 1)
    c = sequential_draw(seed, 2)
    vals = {a, b, c}
    assert len(vals) == 3
    assert sequential_draw(seed, 2) == c


def test_chance_is_integer_comparison():
    assert chance(0, 1 << 63) is True
    assert chance((1 << 64) - 1, 0) is False
    assert chance(1 << 63, 1 << 63) is False
    assert chance(2**62, 2**63) is True


def test_choice_and_randint_and_bounded():
    assert choice(10, 3) == 1
    assert choice(0, 7) == 0
    assert randint(10, 5, 10) == 5 + (10 % 6)
    assert bounded_int(10, 1, 1) == 1


def test_triangular_range_and_mode_at_zero():
    seen = set()
    for r1 in range(16):
        for r2 in range(16):
            seen.add(triangular(r1, r2))
    assert seen == set(range(-15, 16))
    counts = dict.fromkeys(range(-15, 16), 0)
    for r1 in range(16):
        for r2 in range(16):
            counts[triangular(r1, r2)] += 1
    assert counts[0] == max(counts.values())
    assert counts[-15] == 1 and counts[15] == 1


def test_triangular_deterministic():
    assert triangular(123, 456) == triangular(123, 456)


def test_uniform01_is_decimal_in_unit_half_open():
    u = keyed_draw(stream_seed(MASTER, SPEC, "demand"), "A", 1, "s", "k", 0)
    d = uniform01(u)
    assert isinstance(d, Decimal)
    assert Decimal(0) <= d < Decimal(1)


def test_draw_requires_open_txn():
    r = Rng(MASTER, SPEC)
    with pytest.raises(EngineError) as ei:
        r.draw("acceptance", "appointment", 1, "2026-10-01", "k", 0)
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN


def test_draw_works_inside_txn_and_is_deterministic():
    r = Rng(MASTER, SPEC)
    r.enter_txn()
    u1 = r.draw("acceptance", "appointment", 1, "2026-10-01", "k", 0)
    u2 = r.draw("acceptance", "appointment", 1, "2026-10-01", "k", 0)
    assert (
        u1
        == u2
        == keyed_draw(
            stream_seed(MASTER, SPEC, "acceptance"), "appointment", 1, "2026-10-01", "k", 0
        )
    )
    r.exit_txn()


def test_keyed_draw_rejects_sequential_stream_name_and_vice_versa():
    r = Rng(MASTER, SPEC)
    r.enter_txn()
    with pytest.raises(EngineError) as ei:
        r.draw("warm_start", "A", 1, "s", "k", 0)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    with pytest.raises(EngineError) as ei:
        r.stream("acceptance")
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    r.exit_txn()


def test_audit_records_key_not_secret():
    recs: list[dict] = []
    r = Rng(MASTER, SPEC, audit_recorder=lambda **kw: recs.append(kw))
    r.enter_txn()
    r.draw("acceptance", "appointment", 7, "2026-10-01", "k", 0, outcome_id="call_42")
    rec = recs[-1]
    assert rec["stream_name"] == "acceptance"
    assert rec["entity_id"] == 7
    assert rec["outcome_id"] == "call_42"
    assert "u" not in rec and "value" not in rec
    assert "seed" not in rec and "stream_seed" not in rec
    r2 = Rng(MASTER, SPEC)
    r2.enter_txn()
    r2.draw("reviews", "A", 1, "s", "k", 0)
    r2.exit_txn()


def test_sequential_stream_audit_and_counter_advance():
    recs: list[dict] = []
    r = Rng(MASTER, SPEC, audit_recorder=lambda **kw: recs.append(kw))
    r.enter_txn()
    st = r.stream("warm_start")
    u0 = st.next()
    u1 = st.next()
    assert u0 == sequential_draw(st.seed, 0)
    assert u1 == sequential_draw(st.seed, 1)
    assert st.counter == 2
    assert recs[0]["event_kind"] == "sequential"
    assert recs[0]["draw_index"] == 0
    assert recs[1]["draw_index"] == 1
    assert recs[0]["stream_name"] == "warm_start"
    r.exit_txn()


def test_seq_state_snapshot_and_restore_reproduces_stream():
    r = Rng(MASTER, SPEC)
    r.enter_txn()
    st = r.stream("warm_start")
    st.next()
    st.next()
    snap = r.seq_state()
    assert snap["warm_start"][1] == 2
    r2 = Rng(MASTER, SPEC)
    r2.enter_txn()
    r2.restore_seq_state(snap)
    st2 = r2.stream("warm_start")
    assert st2.next() == sequential_draw(st2.seed, 2)
    r.exit_txn()
    r2.exit_txn()


def test_master_seed_validation():
    with pytest.raises(ValueError):
        Rng(-1, SPEC)
    with pytest.raises(ValueError):
        Rng(1 << 64, SPEC)
    Rng(0, SPEC)


def test_keyed_draw_rejects_bad_entity_id_and_draw_index():
    seed = stream_seed(MASTER, SPEC, "demand")
    with pytest.raises(EngineError) as ei:
        keyed_draw(seed, "A", 0, "s", "k", 0)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(EngineError) as ei:
        keyed_draw(seed, "A", -1, "s", "k", 0)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(EngineError) as ei:
        keyed_draw(seed, "A", 1, "s", "k", -1)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(EngineError) as ei:
        keyed_draw(seed, "A", 1 << 63, "s", "k", 0)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_enter_exit_txn_guarded():
    r = Rng(MASTER, SPEC)
    r.enter_txn()
    with pytest.raises(EngineError):
        r.enter_txn()
    r.exit_txn()
    with pytest.raises(EngineError):
        r.exit_txn()
