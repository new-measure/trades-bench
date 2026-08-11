from __future__ import annotations

import sqlite3
from collections.abc import Callable
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


def _existing_response(
    conn: sqlite3.Connection, request_id: str, namespace: str
) -> bytes | None:
    row = conn.execute(
        "SELECT response FROM _idempotency WHERE namespace=? AND request_id=?",
        (namespace, request_id),
    ).fetchone()
    if row is None:
        return None
    return bytes(row["response"])


def _store_response(
    conn: sqlite3.Connection,
    request_id: str,
    namespace: str,
    response: bytes,
    created_at_ms: int,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO _idempotency "
        "(namespace, request_id, response, created_at_ms) VALUES (?, ?, ?, ?)",
        (namespace, request_id, response, created_at_ms),
    )


Fn = Callable[[TxnContext], dict | None]


def _execute(
    services: Services,
    request_id: str,
    namespace: str,
    fn: Fn,
    *,
    require_status: RunStatus,
    require_active_invocation: bool,
    privileged: bool,
) -> bytes:
    conn = services.conn
    rng = services.rng

    existing = _existing_response(conn, request_id, namespace)
    if existing is not None:
        return existing

    now = clock.now(conn)
    ctx = TxnContext(conn, rng, now, services, privileged=privileged)

    begin(conn)
    rng.enter_txn()
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
                    ErrorCode.INVOCATION_CONFLICT, "command requires an active invocation"
                )
            ctx.invocation = active
        payload = fn(ctx)
        response = ok_response(payload)
        _store_response(conn, request_id, namespace, response, now)
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
        if conn.in_transaction:
            rollback(conn)
        response = err.to_response()
        if err.code not in NON_CACHEABLE_ERRORS:
            begin(conn)
            _store_response(conn, request_id, namespace, response, now)
            commit(conn)
        return response

    assert response is not None
    return response


def command(services: Services, request_id: str, fn: Fn) -> bytes:
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
    )


def privileged(
    services: Services,
    request_id: str,
    fn: Fn,
    *,
    require_status: RunStatus = RunStatus.ACTIVE,
    require_active_invocation: bool = False,
) -> bytes:
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
    )


__all__ = [
    "NON_CACHEABLE_ERRORS",
    "PRIVILEGED_NAMESPACE",
    "PRIV_PREFIX",
    "PUBLIC_NAMESPACE",
    "Services",
    "TxnContext",
    "command",
    "is_reserved",
    "privileged",
    "privileged_request_id",
]
