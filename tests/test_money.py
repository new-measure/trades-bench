from __future__ import annotations

import pytest

from trades_bench.money import (
    BASIS_POINTS_SCALE,
    FloatMoneyError,
    Money,
    basis_point_amount,
)


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


def test_basis_point_amount_scale_is_ten_thousand():
    assert BASIS_POINTS_SCALE == 10_000


@pytest.mark.parametrize(
    ("amount_cents", "basis_points", "expected"),
    [
        (0, 2500, 0),
        (100_000, 0, 0),
        (100_000, 2500, 25_000),
        (45_000, 1500, 6_750),
        (333, 1500, 50),
        (1, 5000, 1),
        (3, 5000, 2),
        (1, 4999, 0),
        (750_000, 4500, 337_500),
    ],
)
def test_basis_point_amount_rounds_half_up_on_exact_integer_cents(
    amount_cents, basis_points, expected
):
    assert basis_point_amount(amount_cents, basis_points) == expected


def test_basis_point_amount_never_exceeds_the_base_amount():
    for basis_points in range(0, BASIS_POINTS_SCALE, 137):
        assert basis_point_amount(50_000, basis_points) <= 50_000


@pytest.mark.parametrize("bad", [1.0, 0.5, True, "100"])
def test_basis_point_amount_rejects_non_integer_inputs(bad):
    with pytest.raises(FloatMoneyError):
        basis_point_amount(bad, 1500)
    with pytest.raises(FloatMoneyError):
        basis_point_amount(1500, bad)


def test_basis_point_amount_rejects_negative_and_out_of_scale_values():
    with pytest.raises(ValueError):
        basis_point_amount(-1, 1500)
    with pytest.raises(ValueError):
        basis_point_amount(1000, -1)
    with pytest.raises(ValueError):
        basis_point_amount(1000, BASIS_POINTS_SCALE)
