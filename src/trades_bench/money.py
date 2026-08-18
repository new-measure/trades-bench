from __future__ import annotations

from typing import Any, SupportsInt


class FloatMoneyError(TypeError):
    pass


class Money:

    __slots__ = ("cents",)

    def __init__(self, cents: int | SupportsInt) -> None:
        if isinstance(cents, bool) or isinstance(cents, float):
            raise FloatMoneyError(
                f"money must be int cents, got {type(cents).__name__!r}; "
                "floats are prohibited on the money path"
            )
        c = int(cents)
        if c != cents and not isinstance(cents, int):  # pragma: no cover - SupportsInt path
            raise FloatMoneyError("money must be an exact integer")
        self.cents: int = self._check(c)

    @staticmethod
    def _check(cents: int) -> int:
        if not isinstance(cents, int):
            raise FloatMoneyError("money must be int cents")
        if not -(2**63) <= cents <= 2**63 - 1:
            raise ValueError("money amount out of signed 64-bit range")
        return cents

    def __add__(self, other: Money) -> Money:
        _coerce(other)
        return Money(self.cents + other.cents)

    def __sub__(self, other: Money) -> Money:
        _coerce(other)
        return Money(self.cents - other.cents)

    def __neg__(self) -> Money:
        return Money(-self.cents)

    def __pos__(self) -> Money:
        return Money(self.cents)

    def __mul__(self, scalar: int) -> Money:
        if isinstance(scalar, bool) or isinstance(scalar, float) or not isinstance(scalar, int):
            raise FloatMoneyError("money can only be scaled by an exact int")
        return Money(self.cents * scalar)

    __rmul__ = __mul__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self.cents == other.cents
        return NotImplemented

    def __lt__(self, other: Money) -> bool:
        _coerce(other)
        return self.cents < other.cents

    def __le__(self, other: Money) -> bool:
        _coerce(other)
        return self.cents <= other.cents

    def __gt__(self, other: Money) -> bool:
        _coerce(other)
        return self.cents > other.cents

    def __ge__(self, other: Money) -> bool:
        _coerce(other)
        return self.cents >= other.cents

    def __hash__(self) -> int:
        return hash(("Money", self.cents))

    def __int__(self) -> int:
        return self.cents

    def __repr__(self) -> str:
        return f"Money(cents={self.cents})"

    def __str__(self) -> str:
        return self.format_dollars()

    def format_dollars(self) -> str:
        sign = "-" if self.cents < 0 else ""
        n = abs(self.cents)
        return f"{sign}${n // 100}.{n % 100:02d}"

    @classmethod
    def from_dollars(cls, dollars: str) -> Money:
        if isinstance(dollars, bool) or isinstance(dollars, float):
            raise FloatMoneyError("from_dollars takes a str, not a float")
        s = str(dollars).strip()
        neg = s.startswith("-")
        if neg:
            s = s[1:]
        if "e" in s.lower() or "n" in s.lower():
            raise ValueError("from_dollars rejects non-decimal notation")
        if "." in s:
            whole, frac = s.split(".", 1)
            if len(frac) != 2 or not frac.isdigit():
                raise ValueError("from_dollars requires exactly two cent digits")
        else:
            whole, frac = s, "00"
        if not whole.isdigit():
            raise ValueError("from_dollars requires a non-negative integer dollars part")
        cents = int(whole) * 100 + int(frac)
        return cls(-cents if neg else cents)


def _coerce(other: Any) -> None:
    if not isinstance(other, Money):
        raise TypeError(f"money arithmetic expects Money, got {type(other).__name__!r}")


__all__ = ["Money", "FloatMoneyError"]
