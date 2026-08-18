from __future__ import annotations

from datetime import date

from .errors import EngineError, ErrorCode

__all__ = ["require_id", "require_service_date", "require_str"]


def _bad_argument(message: str) -> EngineError:
    return EngineError(ErrorCode.INVALID_ARGUMENT, message)


def _state_illegal(reason: str, message: str, **details: object) -> EngineError:
    return EngineError(ErrorCode.STATE_ILLEGAL, message, details={"reason": reason, **details})


def require_id(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad_argument(f"{name} must be an int, got {value!r}")
    return value


def require_str(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise _bad_argument(f"{name} must be a str, got {value!r}")
    return value


def require_service_date(name: str, value: object) -> str:
    value = require_str(name, value)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _state_illegal(
            "invalid_service_date", f"{name} {value!r} is not a valid ISO date"
        ) from None
    if parsed.isoformat() != value:
        raise _state_illegal(
            "invalid_service_date", f"{name} {value!r} is not in YYYY-MM-DD form"
        )
    return value
