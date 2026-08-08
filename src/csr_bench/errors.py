"""Stable structured error codes (spec 01 §Failure and transaction semantics).

Every engine failure surfaces as a stable :class:`EngineError` carrying a
machine-readable ``code`` and a human ``message`` that must never leak the
existence or values of ``private_engine`` data (spec 01 §Visibility contract:

    "Errors for a hidden table and for a nonexistent table Must be
    indistinguishable."

and §Command transactions:

    "Probing [privileged operations] returns the same uniform authorization
    error as any other unauthorized request."

So :data:`ErrorCode.UNAUTHORIZED` always carries the same generic message and
no details, whether the caller is unauthorized for a real privileged operation
or probing a hidden/nonexistent target.
"""

from __future__ import annotations

import json
from enum import StrEnum


class ErrorCode(StrEnum):
    #: Uniform authorization denial. Generic message; never distinguish
    #: "forbidden" from "does not exist" (spec 01 §Visibility / §Command).
    UNAUTHORIZED = "unauthorized"
    #: The run is not in a status the operation allows.
    INVALID_RUN_STATUS = "invalid_run_status"
    #: At most one invocation active; a world event cannot commit during one;
    #: a second open is rejected (invocation contract).
    INVOCATION_CONFLICT = "invocation_conflict"
    #: Discretionary command with immediate cost would drop cash below zero.
    INSUFFICIENT_CASH = "insufficient_cash"
    #: A referenced entity does not exist or is not in the expected state.
    ENTITY_NOT_FOUND = "entity_not_found"
    #: A state-machine transition is illegal from the current state.
    STATE_ILLEGAL = "state_illegal"
    #: During a closing invocation a mutation targets the finished day
    #: (00-decisions Q33; spec 01 §Invocation contract, acceptance #11).
    CLOSING_RESTRICTED = "closing_restricted"
    #: An unknown / unregistered configuration key appeared (spec 01 §Config rule 2).
    UNKNOWN_CONFIG_KEY = "unknown_config_key"
    #: A registered config value failed validation (bounds, range, grid, ...).
    CONFIG_VALUE_INVALID = "config_value_invalid"
    #: A scenario attempted to override a user-decided constant.
    CONSTANT_OVERRIDE = "constant_override"
    #: A keyed draw or schedule was attempted outside an open transaction.
    DRAW_OUTSIDE_TXN = "draw_outside_txn"
    #: A malformed argument to an engine function — wrong type, out-of-range
    #: shape (not a legal entity id, not a u64, a string too long to encode) —
    #: as distinct from :data:`DRAW_OUTSIDE_TXN`, which is specifically the
    #: outside-a-transaction guard.
    INVALID_ARGUMENT = "invalid_argument"
    #: An applied event was asked to apply again (spec 01 invariants #4).
    EVENT_ALREADY_APPLIED = "event_already_applied"
    #: A cleanup / resume invariant was violated.
    INVARIANT_VIOLATED = "invariant_violated"
    #: A deferred-value manifest entry maps to no registered config key,
    #: or a registered key is still ``provisional`` at a strict (release) build
    #: (CAL-01 / CAL-04).
    DEFERRED_UNRESOLVED = "deferred_unresolved"
    #: A field has no entry in the visibility metadata registry — a
    #: registry-completeness / build failure, not a runtime maybe (spec 01
    #: §Visibility contract: "an unclassified field fails the build").
    UNCLASSIFIED_FIELD = "unclassified_field"
    #: A non-:class:`EngineError` exception escaped a command/privileged ``fn``
    #: (an adapter bug, a ``sqlite3.Error``, ...). The transaction is rolled
    #: back and this stable code is returned instead of letting the raw
    #: exception propagate with the database transaction left open (spec 01
    #: §Failure and transaction semantics, specs/01-run-contracts.md:222:
    #: "Unexpected failures Must return a stable structured error code and
    #: leave state unchanged"). Never memoized — see :mod:`csr_bench.txn`.
    UNEXPECTED_FAILURE = "unexpected_failure"


#: The indistinguishable generic message for every authorization denial.
_UNAUTHORIZED_MESSAGE = "unauthorized"


class EngineError(Exception):
    """An engine failure with a stable ``code`` plus a non-leaking ``message``.

    ``details`` is a small deterministic mapping for context the caller may pass
    to the public surface; it must never contain hidden-table names or values.
    """

    __slots__ = ("code", "message", "details")

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        # UNAUTHORIZED is always flattened to the same generic surface so a probe
        # cannot learn whether a privileged/hidden target exists.
        if code is ErrorCode.UNAUTHORIZED:
            message = _UNAUTHORIZED_MESSAGE
            details = None
        self.code: ErrorCode = code
        self.message: str = message if message is not None else code.value
        self.details: dict[str, object] = dict(details) if details else {}
        super().__init__(self.message)

    @classmethod
    def unauthorized(cls) -> EngineError:
        """The single, uniform authorization-denial error.

        Used for both real privileged operations and probes of hidden /
        nonexistent targets so the two are indistinguishable (spec 01 §Command
        transactions).
        """
        return cls(ErrorCode.UNAUTHORIZED)

    def to_response(self) -> bytes:
        """Canonical, sorted JSON serialization of the error for idempotent
        response storage (byte-for-byte identical on retry within a run).
        """
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
    """Canonical, sorted JSON serialization of a successful command response.

    The deterministic sorting guarantees that replaying an idempotent
    ``request_id`` returns the stored response byte-for-byte (spec 01 §Invariant
    4 / acceptance #4). Response payload keys must be strings: ``_canonicalize``
    stringifies a non-str key with ``str(k)`` (e.g. ``True`` -> ``"True"``, not
    JSON's ``"true"``); domain callers build these payloads and must not use
    bool/None/numeric keys.
    """
    body: dict[str, object] = {"ok": True}
    if payload:
        body["result"] = _canonicalize(payload)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonicalize(value: object) -> object:
    """Best-effort canonicalization of a response payload so its JSON is stable.

    Decimals are rendered as strings (no float on the transport path), enums as
    their string value, and everything else left to JSON's own sorting. Response
    payloads are built by domain callers; this keeps the path float-free.
    """
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
