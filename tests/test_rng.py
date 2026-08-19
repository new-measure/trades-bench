from __future__ import annotations

import hashlib
from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trades_bench.errors import EngineError, ErrorCode
from trades_bench.rng import (
    U64_MOD,
    Rng,
    bounded_int,
    chance,
    choice,
    keyed_draw,
    randint,
    sequential_draw,
    stream_seed,
    triangular,
    triangular_cents,
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


_TRIANGLE = {"low": 0, "mode": 5000, "high": 10000}


def test_triangular_cents_pins_the_distribution_endpoints():
    assert triangular_cents(0, **_TRIANGLE) == 0
    assert triangular_cents(U64_MOD - 1, **_TRIANGLE) == 10000


def test_triangular_cents_returns_the_mode_at_the_cumulative_mode_probability():
    u_at_mode = U64_MOD // 2
    assert triangular_cents(u_at_mode, **_TRIANGLE) == 5000
    assert triangular_cents(u_at_mode - 1, **_TRIANGLE) == 5000
    assert triangular_cents(u_at_mode + 1, **_TRIANGLE) == 5000


def test_triangular_cents_hand_computed_interior_literal():
    assert triangular_cents(1 << 62, **_TRIANGLE) == 3536


def _reference_triangular_cents(u: int, *, low: int, mode: int, high: int) -> int:
    with localcontext() as ctx:
        ctx.prec = 60
        share = Decimal(u) / Decimal(U64_MOD)
        left = Decimal(mode - low) / Decimal(high - low)
        if share < left:
            exact = Decimal(low) + (share * Decimal((high - low) * (mode - low))).sqrt()
        else:
            exact = Decimal(high) - (
                (Decimal(1) - share) * Decimal((high - low) * (high - mode))
            ).sqrt()
        return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def test_triangular_cents_rounds_half_up_on_both_branches():
    left_u = 1 << 62
    right_u = U64_MOD - (1 << 62)
    assert triangular_cents(left_u, **_TRIANGLE) == 3536
    assert _reference_triangular_cents(left_u, **_TRIANGLE) == 3536
    assert triangular_cents(right_u, **_TRIANGLE) == 6464
    assert _reference_triangular_cents(right_u, **_TRIANGLE) == 6464


@given(st.integers(min_value=0, max_value=U64_MOD - 1))
@settings(max_examples=400, deadline=None)
def test_triangular_cents_matches_the_half_up_reference_within_one_cent(u):
    triangle = {"low": 20000, "mode": 38000, "high": 85000}
    assert abs(triangular_cents(u, **triangle) - _reference_triangular_cents(u, **triangle)) <= 1


def test_triangular_cents_handles_an_asymmetric_mode():
    triangle = {"low": 20000, "mode": 38000, "high": 85000}
    assert triangular_cents(0, **triangle) == 20000
    assert triangular_cents(U64_MOD - 1, **triangle) == 85000
    at_mode = triangular_cents((18000 * U64_MOD) // 65000, **triangle)
    assert abs(at_mode - 38000) <= 1


def test_triangular_cents_collapses_to_a_point_distribution():
    assert triangular_cents(12345, low=7000, mode=7000, high=7000) == 7000


def test_triangular_cents_rejects_bad_arguments():
    with pytest.raises(EngineError) as ei:
        triangular_cents(U64_MOD, **_TRIANGLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(EngineError) as ei:
        triangular_cents(-1, **_TRIANGLE)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(EngineError) as ei:
        triangular_cents(0, low=100, mode=50, high=200)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    with pytest.raises(EngineError) as ei:
        triangular_cents(0, low=-1, mode=50, high=200)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    with pytest.raises(EngineError) as ei:
        triangular_cents(0, low=0, mode=5000.0, high=10000)  # type: ignore[arg-type]
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


@given(st.integers(min_value=0, max_value=U64_MOD - 1))
@settings(max_examples=300, deadline=None)
def test_triangular_cents_output_always_inside_the_configured_bounds(u):
    assert 20000 <= triangular_cents(u, low=20000, mode=38000, high=85000) <= 85000


@given(
    st.integers(min_value=0, max_value=U64_MOD - 2),
    st.integers(min_value=1, max_value=1 << 40),
)
@settings(max_examples=300, deadline=None)
def test_triangular_cents_is_monotone_nondecreasing_in_u(u, step):
    higher = min(u + step, U64_MOD - 1)
    assert triangular_cents(u, low=20000, mode=38000, high=85000) <= triangular_cents(
        higher, low=20000, mode=38000, high=85000
    )


def test_triangular_cents_draw_is_keyed_and_unaffected_by_unrelated_keys():
    r = Rng(MASTER, SPEC)
    r.enter_txn()
    first = r.triangular_cents("job_finance", "job", 41, "2026-10-01", "invoice_amount", 0, 5000, 10000)
    other_entity = r.triangular_cents(
        "job_finance", "job", 42, "2026-10-01", "invoice_amount", 0, 5000, 10000
    )
    repeat = r.triangular_cents("job_finance", "job", 41, "2026-10-01", "invoice_amount", 0, 5000, 10000)
    assert first == repeat
    assert 0 <= other_entity <= 10000
