from __future__ import annotations

import json
from enum import StrEnum


class ErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    INVALID_RUN_STATUS = "invalid_run_status"
    INVOCATION_CONFLICT = "invocation_conflict"
    INSUFFICIENT_CASH = "insufficient_cash"
    ENTITY_NOT_FOUND = "entity_not_found"
    STATE_ILLEGAL = "state_illegal"
    CLOSING_RESTRICTED = "closing_restricted"
    UNKNOWN_CONFIG_KEY = "unknown_config_key"
    CONFIG_VALUE_INVALID = "config_value_invalid"
    CONSTANT_OVERRIDE = "constant_override"
    DRAW_OUTSIDE_TXN = "draw_outside_txn"
    INVALID_ARGUMENT = "invalid_argument"
    EVENT_ALREADY_APPLIED = "event_already_applied"
    INVARIANT_VIOLATED = "invariant_violated"
    DEFERRED_UNRESOLVED = "deferred_unresolved"
    UNCLASSIFIED_FIELD = "unclassified_field"
    UNEXPECTED_FAILURE = "unexpected_failure"
    MIGRATION_FAILED = "migration_failed"
    MIGRATION_CHECKSUM_MISMATCH = "migration_checksum_mismatch"
    VIEW_REGISTRY_MISMATCH = "view_registry_mismatch"
    ADDRESS_NORMALIZATION_AMBIGUOUS = "address_normalization_ambiguous"


_UNAUTHORIZED_MESSAGE = "unauthorized"


class EngineError(Exception):

    __slots__ = ("code", "message", "details")

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        if code is ErrorCode.UNAUTHORIZED:
            message = _UNAUTHORIZED_MESSAGE
            details = None
        self.code: ErrorCode = code
        self.message: str = message if message is not None else code.value
        self.details: dict[str, object] = dict(details) if details else {}
        super().__init__(self.message)

    @classmethod
    def unauthorized(cls) -> EngineError:
        return cls(ErrorCode.UNAUTHORIZED)

    def to_response(self) -> bytes:
        payload: dict[str, object] = {
            "ok": False,
            "error": {
                "code": str(self.code),
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = dict(sorted(self.details.items()))
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"EngineError(code={self.code.value!r}, message={self.message!r})"


def ok_response(payload: dict[str, object] | None = None) -> bytes:
    body: dict[str, object] = {"ok": True}
    if payload:
        body["result"] = _canonicalize(payload)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonicalize(value: object) -> object:
    from decimal import Decimal
    from enum import Enum

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value
