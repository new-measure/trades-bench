from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import clock
from .db import begin, commit, rollback
from .enums import RunStatus
from .errors import EngineError, ErrorCode, ok_response
from .rng import Rng

PRIV_PREFIX = "__control__"

PUBLIC_NAMESPACE = "public"
PRIVILEGED_NAMESPACE = "privileged"

NON_CACHEABLE_ERRORS = frozenset({ErrorCode.INVOCATION_CONFLICT, ErrorCode.UNEXPECTED_FAILURE})


def is_reserved(request_id: str) -> bool:
    return isinstance(request_id, str) and request_id.startswith(PRIV_PREFIX)


def privileged_request_id(reason: str, key: str | int) -> str:
    return f"{PRIV_PREFIX}:{reason}:{key}"


@dataclass
class Services:

    conn: sqlite3.Connection
    rng: Rng
    ledger: Any
    invocations: Any
    clock: Any = None
    events: Any = None


@dataclass
class TxnContext:

    conn: sqlite3.Connection
    rng: Rng
    now_ms: int
    services: Services
    invocation: Any = None
    privileged: bool = False

    def now(self) -> int:
        return self.now_ms


def _run_status(conn: sqlite3.Connection) -> RunStatus:
    row = conn.execute("SELECT status FROM _run").fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "run record not found")
    return RunStatus(row["status"])


def run_status(conn: sqlite3.Connection) -> RunStatus:
    return _run_status(conn)


def fingerprint(operation: str, args: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "args": args}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_entry(
    conn: sqlite3.Connection, request_id: str, namespace: str
) -> tuple[str, bytes] | None:
    row = conn.execute(
        "SELECT fingerprint, response FROM _idempotency WHERE namespace=? AND request_id=?",
        (namespace, request_id),
    ).fetchone()
    if row is None:
        return None
    return str(row["fingerprint"]), bytes(row["response"])


def _store_response(
    conn: sqlite3.Connection,
    request_id: str,
    namespace: str,
    response: bytes,
    created_at_ms: int,
    fingerprint_value: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO _idempotency "
        "(namespace, request_id, response, fingerprint, created_at_ms) VALUES (?, ?, ?, ?, ?)",
        (namespace, request_id, response, fingerprint_value, created_at_ms),
    )


Fn = Callable[[TxnContext], dict | None]


def _recover_from_sqlite_fault(conn: sqlite3.Connection, exc: sqlite3.Error) -> EngineError:
    conn.set_progress_handler(None, 0)
    try:
        if conn.in_transaction:
            rollback(conn)
    except sqlite3.Error:
        pass
    return EngineError(
        ErrorCode.UNEXPECTED_FAILURE,
        "an unexpected internal error occurred",
        details={"exception_type": type(exc).__name__},
    )


def _execute(
    services: Services,
    request_id: str,
    namespace: str,
    fn: Fn,
    *,
    require_status: RunStatus,
    require_active_invocation: bool,
    privileged: bool,
    fingerprint_value: str,
) -> tuple[bytes, bool]:
    conn = services.conn
    rng = services.rng

    try:
        existing = _existing_entry(conn, request_id, namespace)
    except sqlite3.Error as exc:
        return _recover_from_sqlite_fault(conn, exc).to_response(), False

    if existing is not None:
        existing_fingerprint, existing_response = existing
        if existing_fingerprint == fingerprint_value:
            return existing_response, True
        return (
            EngineError(
                ErrorCode.REQUEST_ID_CONFLICT,
                f"request_id {request_id!r} was already used with different arguments",
            ).to_response(),
            False,
        )

    try:
        now = clock.now(conn)
        ctx = TxnContext(conn, rng, now, services, privileged=privileged)
        begin(conn)
        rng.enter_txn()
    except sqlite3.Error as exc:
        return _recover_from_sqlite_fault(conn, exc).to_response(), False

    payload: dict | None = None
    response: bytes | None = None
    err: EngineError | None = None
    try:
        status = _run_status(conn)
        if status != require_status:
            raise EngineError(
                ErrorCode.INVALID_RUN_STATUS,
                f"run is {status.value!r}, requires {require_status.value!r}",
            )
        if require_active_invocation:
            active = services.invocations.active() if services.invocations is not None else None
            if active is None:
                raise EngineError(
                    ErrorCode.INVOCATION_CONFLICT,
                    "command requires an active invocation",
                    details={"reason": "no_active_invocation"},
                )
            ctx.invocation = active
        payload = fn(ctx)
        response = ok_response(payload)
        _store_response(conn, request_id, namespace, response, now, fingerprint_value)
        if services.ledger is not None:
            services.ledger.bankruptcy_check()
        commit(conn)
    except EngineError as exc:
        err = exc
    except Exception as exc:
        err = EngineError(
            ErrorCode.UNEXPECTED_FAILURE,
            "an unexpected internal error occurred",
            details={"exception_type": type(exc).__name__},
        )
    finally:
        rng.exit_txn()

    if err is not None:
        try:
            if conn.in_transaction:
                rollback(conn)
            response = err.to_response()
            if not privileged and err.code not in NON_CACHEABLE_ERRORS:
                begin(conn)
                _store_response(conn, request_id, namespace, response, now, fingerprint_value)
                commit(conn)
        except sqlite3.Error as exc:
            response = _recover_from_sqlite_fault(conn, exc).to_response()
        return response, False

    assert response is not None
    return response, False


def command(
    services: Services,
    request_id: str,
    fn: Fn,
    *,
    operation: str,
    args: Mapping[str, object],
) -> bytes:
    response, _replayed = command_with_replay(
        services, request_id, fn, operation=operation, args=args
    )
    return response


def command_with_replay(
    services: Services,
    request_id: str,
    fn: Fn,
    *,
    operation: str,
    args: Mapping[str, object],
) -> tuple[bytes, bool]:
    if is_reserved(request_id):
        raise EngineError.unauthorized()
    return _execute(
        services,
        request_id,
        PUBLIC_NAMESPACE,
        fn,
        require_status=RunStatus.ACTIVE,
        require_active_invocation=True,
        privileged=False,
        fingerprint_value=fingerprint(operation, args),
    )


def privileged_with_replay(
    services: Services,
    request_id: str,
    fn: Fn,
    *,
    require_status: RunStatus = RunStatus.ACTIVE,
    require_active_invocation: bool = False,
    fingerprint_value: str = "",
) -> tuple[bytes, bool]:
    if not is_reserved(request_id):
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            "privileged request_id must be in the reserved namespace",
        )
    return _execute(
        services,
        request_id,
        PRIVILEGED_NAMESPACE,
        fn,
        require_status=require_status,
        require_active_invocation=require_active_invocation,
        privileged=True,
        fingerprint_value=fingerprint_value,
    )


def privileged(
    services: Services,
    request_id: str,
    fn: Fn,
    *,
    require_status: RunStatus = RunStatus.ACTIVE,
    require_active_invocation: bool = False,
    fingerprint_value: str = "",
) -> bytes:
    response, _replayed = privileged_with_replay(
        services,
        request_id,
        fn,
        require_status=require_status,
        require_active_invocation=require_active_invocation,
        fingerprint_value=fingerprint_value,
    )
    return response


__all__ = [
    "NON_CACHEABLE_ERRORS",
    "PRIVILEGED_NAMESPACE",
    "PRIV_PREFIX",
    "PUBLIC_NAMESPACE",
    "Services",
    "TxnContext",
    "command",
    "command_with_replay",
    "fingerprint",
    "is_reserved",
    "privileged",
    "privileged_request_id",
    "privileged_with_replay",
    "run_status",
]
