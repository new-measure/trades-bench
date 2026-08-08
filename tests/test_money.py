"""Tests for integer-cent money (spec 01 §Canonical primitive types).

Goal: floats can never become Money, and money arithmetic never produces a
float.
"""

from __future__ import annotations

import pytest

from csr_bench.money import FloatMoneyError, Money


def test_construct_int_cents():
    m = Money(12345)
    assert m.cents == 12345
    assert int(m) == 12345


@pytest.mark.parametrize("bad", [1.0, 1.5, -0.25, True, False, 0.0])
def test_float_and_bool_rejected_in_construction(bad):
    with pytest.raises(FloatMoneyError):
        Money(bad)


def test_multiply_rejects_float_and_bool():
    with pytest.raises(FloatMoneyError):
        Money(100) * 1.0
    with pytest.raises(FloatMoneyError):
        Money(100) * True
    assert (Money(100) * 3).cents == 300
    assert (3 * Money(100)).cents == 300


def test_arithmetic_stays_int_and_exact():
    assert (Money(5) + Money(7)).cents == 12
    assert (Money(5) - Money(9)).cents == -4
    assert (-Money(7)).cents == -7
    assert (Money(-3) + Money(3)).cents == 0


def test_money_rejects_adding_non_money():
    with pytest.raises(TypeError):
        Money(100) + 100


def test_comparisons_and_hash():
    assert Money(10) < Money(11)
    assert Money(11) <= Money(11)
    assert Money(12) > Money(11)
    assert Money(5) == Money(5)
    assert hash(Money(5)) == hash(Money(5))
    assert Money(5) != "5"


def test_signed_64bit_range_enforced():
    within = 2**63 - 1
    assert Money(within).cents == within
    with pytest.raises(ValueError):
        Money(2**63)


def test_dollar_format_and_parse_roundtrip():
    m = Money.from_dollars("12.34")
    assert m.cents == 1234
    assert m.format_dollars() == "$12.34"
    assert Money.from_dollars("-3.05").format_dollars() == "-$3.05"
    assert Money(0).format_dollars() == "$0.00"


def test_from_dollars_rejects_float_inputs_and_bad_strings():
    with pytest.raises(FloatMoneyError):
        Money.from_dollars(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Money.from_dollars("1.234")
    with pytest.raises(ValueError):
        Money.from_dollars("1e2")
    with pytest.raises(ValueError):
        Money.from_dollars("abc")
