from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .errors import ErrorCode

SPEC_VERSION = "1.0.0"

_incident_logger = logging.getLogger("trades_bench.engine.incidents")


class WireCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    UNKNOWN_OPERATION = "unknown_operation"
    NO_ACTIVE_SESSION = "no_active_session"
    ENGINE_BUSY = "engine_busy"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INVALID_REQUEST = "invalid_request"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INSUFFICIENT_CASH = "insufficient_cash"
    REQUEST_ID_CONFLICT = "request_id_conflict"
    SQL_NOT_PERMITTED = "sql_not_permitted"
    UNKNOWN_RELATION = "unknown_relation"
    UNKNOWN_COLUMN = "unknown_column"
    SQL_TIMEOUT = "sql_timeout"
    RUN_NOT_ACTIVE = "run_not_active"
    INTERNAL_ERROR = "internal_error"


WIRE_STATUS: Mapping[WireCode, int] = {
    WireCode.UNAUTHORIZED: 401,
    WireCode.UNKNOWN_OPERATION: 404,
    WireCode.NO_ACTIVE_SESSION: 409,
    WireCode.ENGINE_BUSY: 503,
    WireCode.DEADLINE_EXCEEDED: 504,
    WireCode.INVALID_REQUEST: 400,
    WireCode.VALIDATION_ERROR: 422,
    WireCode.NOT_FOUND: 404,
    WireCode.CONFLICT: 409,
    WireCode.INSUFFICIENT_CASH: 422,
    WireCode.REQUEST_ID_CONFLICT: 409,
    WireCode.SQL_NOT_PERMITTED: 422,
    WireCode.UNKNOWN_RELATION: 422,
    WireCode.UNKNOWN_COLUMN: 422,
    WireCode.SQL_TIMEOUT: 504,
    WireCode.RUN_NOT_ACTIVE: 409,
    WireCode.INTERNAL_ERROR: 500,
}

WIRE_MESSAGE_TEMPLATES: Mapping[WireCode, str] = {
    WireCode.UNAUTHORIZED: "unauthorized",
    WireCode.UNKNOWN_OPERATION: "unknown operation",
    WireCode.NO_ACTIVE_SESSION: "no active session",
    WireCode.ENGINE_BUSY: "engine busy",
    WireCode.DEADLINE_EXCEEDED: "deadline exceeded",
    WireCode.INVALID_REQUEST: "invalid request",
    WireCode.VALIDATION_ERROR: "validation failed",
    WireCode.NOT_FOUND: "not found",
    WireCode.CONFLICT: "conflict",
    WireCode.INSUFFICIENT_CASH: "insufficient cash",
    WireCode.REQUEST_ID_CONFLICT: "request_id already used with different arguments",
    WireCode.SQL_NOT_PERMITTED: "statement not permitted",
    WireCode.UNKNOWN_RELATION: "unknown relation",
    WireCode.UNKNOWN_COLUMN: "unknown column",
    WireCode.SQL_TIMEOUT: "statement timed out",
    WireCode.RUN_NOT_ACTIVE: "run not active",
    WireCode.INTERNAL_ERROR: "internal error",
}

ENGINE_ERROR_TRANSLATION: Mapping[ErrorCode, WireCode] = {
    ErrorCode.UNAUTHORIZED: WireCode.UNAUTHORIZED,
    ErrorCode.INVALID_RUN_STATUS: WireCode.RUN_NOT_ACTIVE,
    ErrorCode.INVOCATION_CONFLICT: WireCode.NO_ACTIVE_SESSION,
    ErrorCode.INSUFFICIENT_CASH: WireCode.INSUFFICIENT_CASH,
    ErrorCode.ENTITY_NOT_FOUND: WireCode.NOT_FOUND,
    ErrorCode.STATE_ILLEGAL: WireCode.CONFLICT,
    ErrorCode.CLOSING_RESTRICTED: WireCode.CONFLICT,
    ErrorCode.UNKNOWN_CONFIG_KEY: WireCode.INTERNAL_ERROR,
    ErrorCode.CONFIG_VALUE_INVALID: WireCode.INTERNAL_ERROR,
    ErrorCode.CONSTANT_OVERRIDE: WireCode.INTERNAL_ERROR,
    ErrorCode.DRAW_OUTSIDE_TXN: WireCode.INTERNAL_ERROR,
    ErrorCode.INVALID_ARGUMENT: WireCode.INVALID_REQUEST,
    ErrorCode.EVENT_ALREADY_APPLIED: WireCode.INTERNAL_ERROR,
    ErrorCode.INVARIANT_VIOLATED: WireCode.INTERNAL_ERROR,
    ErrorCode.DEFERRED_UNRESOLVED: WireCode.INTERNAL_ERROR,
    ErrorCode.UNCLASSIFIED_FIELD: WireCode.INTERNAL_ERROR,
    ErrorCode.UNEXPECTED_FAILURE: WireCode.INTERNAL_ERROR,
    ErrorCode.MIGRATION_FAILED: WireCode.INTERNAL_ERROR,
    ErrorCode.MIGRATION_CHECKSUM_MISMATCH: WireCode.INTERNAL_ERROR,
    ErrorCode.VIEW_REGISTRY_MISMATCH: WireCode.INTERNAL_ERROR,
    ErrorCode.ADDRESS_NORMALIZATION_AMBIGUOUS: WireCode.INTERNAL_ERROR,
    ErrorCode.ADDRESS_UNRESOLVABLE: WireCode.VALIDATION_ERROR,
    ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH: WireCode.INTERNAL_ERROR,
    ErrorCode.REQUEST_ID_CONFLICT: WireCode.REQUEST_ID_CONFLICT,
}

_CONFLICT_RULE_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.STATE_ILLEGAL, ErrorCode.CLOSING_RESTRICTED}
)

_VALIDATION_RULE_BY_ENGINE_CODE: Mapping[ErrorCode, str] = {
    ErrorCode.ADDRESS_UNRESOLVABLE: "address_not_resolvable",
}

_INVOCATION_ALREADY_ACTIVE_REASON = "invocation_already_active"


def incident_id_for(request_id: str) -> str:
    digest = hashlib.sha256(f"internal_error:{request_id}".encode()).hexdigest()
    return digest[:32]


def log_incident(incident_id: str, engine_code: ErrorCode, engine_details: Mapping[str, Any]) -> None:
    _incident_logger.error(
        "incident_id=%s engine_code=%s details=%r", incident_id, engine_code.value, dict(engine_details)
    )


@dataclass(frozen=True)
class TranslatedError:

    wire_code: WireCode
    status: int
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


def translate_error(
    engine_code: ErrorCode,
    engine_details: Mapping[str, Any] | None,
    *,
    request_id: str,
    operation: str | None = None,
) -> TranslatedError:
    engine_details = engine_details or {}
    wire_code = ENGINE_ERROR_TRANSLATION.get(engine_code, WireCode.INTERNAL_ERROR)
    if (
        engine_code is ErrorCode.INVOCATION_CONFLICT
        and engine_details.get("reason") == _INVOCATION_ALREADY_ACTIVE_REASON
    ):
        wire_code = WireCode.CONFLICT
    details: dict[str, Any] = {}
    if wire_code is WireCode.INTERNAL_ERROR:
        incident_id = incident_id_for(request_id)
        log_incident(incident_id, engine_code, engine_details)
        details = {"incident_id": incident_id}
    elif engine_code in _CONFLICT_RULE_CODES:
        reason = engine_details.get("reason")
        details = {"rule": str(reason) if reason else engine_code.value}
    elif engine_code is ErrorCode.INVOCATION_CONFLICT and wire_code is WireCode.CONFLICT:
        details = {"rule": _INVOCATION_ALREADY_ACTIVE_REASON}
    elif engine_code in _VALIDATION_RULE_BY_ENGINE_CODE:
        details = {"rule": _VALIDATION_RULE_BY_ENGINE_CODE[engine_code]}
    elif engine_code is ErrorCode.INVALID_ARGUMENT:
        rule = engine_details.get("rule")
        if rule:
            wire_code = WireCode.VALIDATION_ERROR
            details = {"rule": str(rule)}
            field = engine_details.get("field")
            if field:
                details["field"] = str(field)
        elif engine_details.get("field"):
            details = {"field": str(engine_details["field"])}
    elif wire_code is WireCode.INSUFFICIENT_CASH and operation:
        details = {"operation": operation}
    if wire_code is WireCode.UNAUTHORIZED:
        details = {}
    return TranslatedError(
        wire_code=wire_code,
        status=WIRE_STATUS[wire_code],
        message=WIRE_MESSAGE_TEMPLATES[wire_code],
        details=details,
    )


def native_failure(wire_code: WireCode, *, details: Mapping[str, Any] | None = None) -> TranslatedError:
    if wire_code is WireCode.UNAUTHORIZED:
        details = None
    return TranslatedError(
        wire_code=wire_code,
        status=WIRE_STATUS[wire_code],
        message=WIRE_MESSAGE_TEMPLATES[wire_code],
        details=dict(details) if details else {},
    )


def _coerce_non_finite_floats(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _coerce_non_finite_floats(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_non_finite_floats(item) for item in value]
    return value


def build_success_envelope(
    data: Any,
    *,
    as_of: str,
    invocation_id: int | None,
    request_id: str | None = None,
    next_cursor: str | None = None,
) -> bytes:
    meta: dict[str, Any] = {
        "as_of": as_of,
        "invocation_id": invocation_id,
        "spec_version": SPEC_VERSION,
    }
    if request_id is not None:
        meta["request_id"] = request_id
    if next_cursor is not None:
        meta["next_cursor"] = next_cursor
    body = {
        "data": data,
        "meta": meta,
    }
    return json.dumps(_coerce_non_finite_floats(body), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def build_failure_envelope(translated: TranslatedError, *, as_of: str) -> bytes:
    error: dict[str, Any] = {"code": translated.wire_code.value, "message": translated.message}
    if translated.details:
        error["details"] = dict(sorted(translated.details.items()))
    body = {"error": error, "meta": {"as_of": as_of}}
    return json.dumps(_coerce_non_finite_floats(body), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def parse_engine_response(response: bytes) -> tuple[bool, Any]:
    payload = json.loads(response)
    if payload.get("ok"):
        return True, payload.get("result", {})
    error_payload = payload["error"]
    engine_code = ErrorCode(error_payload["code"])
    engine_details = error_payload.get("details") or {}
    return False, (engine_code, engine_details)


def wire_response_for(
    response: bytes,
    *,
    as_of: str,
    invocation_id: int | None,
    request_id: str,
    operation: str | None = None,
) -> tuple[bytes, int]:
    ok, payload = parse_engine_response(response)
    if ok:
        envelope = build_success_envelope(
            payload, as_of=as_of, invocation_id=invocation_id, request_id=request_id
        )
        return envelope, 200
    engine_code, engine_details = payload
    translated = translate_error(
        engine_code, engine_details, request_id=request_id, operation=operation
    )
    return build_failure_envelope(translated, as_of=as_of), translated.status


__all__ = [
    "ENGINE_ERROR_TRANSLATION",
    "SPEC_VERSION",
    "TranslatedError",
    "WIRE_MESSAGE_TEMPLATES",
    "WIRE_STATUS",
    "WireCode",
    "build_failure_envelope",
    "build_success_envelope",
    "incident_id_for",
    "log_incident",
    "native_failure",
    "parse_engine_response",
    "translate_error",
    "wire_response_for",
]
