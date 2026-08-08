"""Command transactions and idempotency (spec 01 §Command transactions and
idempotency, §Failure and transaction semantics).

``command(request_id, fn)`` — one public mutation. ``privileged(...)`` — a
controller-privileged transaction (world-event application, settlement,
checkpointing, run finalization, ``mark_call_unanswered``) using a reserved
``request_id`` namespace the public surface cannot produce. Probing a privileged
operation through the public path returns the same uniform authorization error
as any other unauthorized request.

Validation order (spec 01): run status → active-invocation context →
permission/visibility → referenced entities → state-machine legality → cash
sufficiency, **before any write or random draw**. The engine runs the engine-level
checks (namespace, run status, invocation context) inside the transaction and
before the caller's ``fn`` does anything; domain checks (entities, state machine,
cash) live in ``fn`` and raise :class:`EngineError` on failure.

Failure / retry contract:
- A replayed ``request_id`` returns the stored response **byte-for-byte** and runs
  ``fn`` again not at all → no duplicate rows, draws, or ledger entries
  (invariant #4, #5; acceptance #4).
- Any failure rolls back the whole transaction; the structured error response is
  stored in a fresh mini-transaction so the next retry is byte-identical and
  re-runs nothing — *unless* the error is in :data:`NON_CACHEABLE_ERRORS`. A
  failure is memoized only when it is a deterministic function of the request
  itself (bad input, illegal state transition, unknown entity: retrying the
  exact same request must always fail the exact same way). A transient
  world-state precondition is not a property of the request, so it is
  re-evaluated on every attempt instead of being cached forever.
- A non-:class:`~csr_bench.errors.EngineError` exception raised by ``fn``
  (``sqlite3.Error``, ``TypeError``, any other adapter/handler bug) is caught
  the same way: the transaction rolls back and
  :data:`~csr_bench.errors.ErrorCode.UNEXPECTED_FAILURE` is returned as a
  stable structured response instead of propagating raw with the database
  transaction left open. It is always in :data:`NON_CACHEABLE_ERRORS` — an
  unexpected crash is not a deterministic property of the request (the same
  retry may or may not crash again), so it must never be memoized.
- After a committed transaction with a financial effect, ``cash < 0`` flips the
  run to ``bankrupt`` inside the same transaction (atomic).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .db import begin, commit, rollback
from .enums import RunStatus
from .errors import EngineError, ErrorCode, ok_response
from .rng import Rng

#: Reserved prefix for controller-privileged request IDs. The public surface
#: cannot produce a request ID that begins with this prefix; probing one through
#: the public ``command`` path returns the uniform authorization error.
PRIV_PREFIX = "__control__"

PUBLIC_NAMESPACE = "public"
PRIVILEGED_NAMESPACE = "privileged"

#: Error codes that must never be memoized under a request id. These are
#: transient world-state preconditions, not a deterministic function of the
#: request: e.g. INVOCATION_CONFLICT from Events.apply (keyed
#: ``__control__:apply_event:<eid>``) is refused only while an invocation
#: happens to be active right now — caching it would make that event
#: permanently inapplicable once the invocation ends. UNEXPECTED_FAILURE is
#: the same shape: a crash inside ``fn`` is not a deterministic property of
#: the request (same reasoning; see the module docstring's memoization rule).
#: Domain/handler errors (INVALID_RUN_STATUS, STATE_ILLEGAL, ...) stay cached.
#: INVALID_RUN_STATUS reads transient world state too (``initializing ->
#: active``), but unlike INVOCATION_CONFLICT that transition happens exactly
#: once per run and moves one way, and request ids are never replayed across
#: it (the public command surface this rejection guards is unreachable while
#: `initializing` — see `command`'s `require_status`), so treating it as a
#: request-deterministic failure is safe here even though, in the abstract,
#: it is a precondition rather than a property of the request itself.
NON_CACHEABLE_ERRORS = frozenset({ErrorCode.INVOCATION_CONFLICT, ErrorCode.UNEXPECTED_FAILURE})


def is_reserved(request_id: str) -> bool:
    return isinstance(request_id, str) and request_id.startswith(PRIV_PREFIX)


def privileged_request_id(reason: str, key: str | int) -> str:
    """A deterministic reserved request ID, stable across a resume (DET-02)."""
    return f"{PRIV_PREFIX}:{reason}:{key}"


@dataclass
class Services:
    """Engine services the orchestrator wires into the transaction layer.

    Kept dependency-light (``Any`` fields) so :mod:`csr_bench.txn` does not import
    the ledger / events / invocations modules and there is no cycle. The
    orchestrator constructs this once per run with the real instances.
    """

    conn: sqlite3.Connection
    run_id: str
    rng: Rng
    ledger: Any
    invocations: Any
    clock: Any = None
    events: Any = None


@dataclass
class TxnContext:
    """Passed to every command/privileged ``fn``: the open transaction's handles.

    ``now_ms`` is the frozen current simulated time (world time is frozen while
    an invocation is active; the controller owns it). ``invocation`` is the
    active invocation row (or ``None`` for between-event privileged ops) so a
    command can enforce the closing-day restriction (acceptance #11).
    """

    conn: sqlite3.Connection
    run_id: str
    rng: Rng
    now_ms: int
    services: Services
    invocation: Any = None  # sqlite3.Row of the active invocation, or None
    privileged: bool = False

    def now(self) -> int:
        return self.now_ms


def _current_time(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute("SELECT current_time_ms FROM run WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"run {run_id!r} not found")
    return int(row["current_time_ms"])


def _run_status(conn: sqlite3.Connection, run_id: str) -> RunStatus:
    row = conn.execute("SELECT status FROM run WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"run {run_id!r} not found")
    return RunStatus(row["status"])


def _existing_response(
    conn: sqlite3.Connection, run_id: str, request_id: str, namespace: str
) -> bytes | None:
    row = conn.execute(
        "SELECT response FROM _command_responses WHERE run_id=? AND request_id=? AND namespace=?",
        (run_id, request_id, namespace),
    ).fetchone()
    if row is None:
        return None
    return bytes(row["response"])


def _store_response(
    conn: sqlite3.Connection,
    run_id: str,
    request_id: str,
    namespace: str,
    response: bytes,
    created_at_ms: int,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO _command_responses "
        "(run_id, request_id, namespace, response, created_at_ms) VALUES (?, ?, ?, ?, ?)",
        (run_id, request_id, namespace, response, created_at_ms),
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
    run_id = services.run_id
    rng = services.rng

    # 1. idempotency short-circuit (read-only) before opening any transaction.
    existing = _existing_response(conn, run_id, request_id, namespace)
    if existing is not None:
        return existing

    now = _current_time(conn, run_id)
    # `invocation` is always resolved fresh inside the transaction below (and
    # overwrites this field before `fn` runs whenever `require_active_invocation`
    # is set); when it is not set the field stays None regardless. There is no
    # path where a pre-transaction read would be observed.
    ctx = TxnContext(conn, run_id, rng, now, services, invocation=None, privileged=privileged)

    begin(conn)
    rng.enter_txn()
    payload: dict | None = None
    response: bytes | None = None
    err: EngineError | None = None
    try:
        # engine-level validation, in order: run status, then invocation context.
        status = _run_status(conn, run_id)
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
        # domain checks (entities, state machine, cash) happen inside fn before
        # any write/draw — a failure raising here rolls the whole txn back.
        payload = fn(ctx)
        # success tail — response storage, the bankruptcy hook (atomic with the
        # financial effect that produced it), and the commit itself — stays
        # inside this same try so a bug here (e.g. a non-JSON-serializable
        # payload) funnels into the same rollback/UNEXPECTED_FAILURE path as a
        # bug in `fn`, instead of leaving the transaction open with writes
        # pending (spec 01 §Failure and transaction semantics,
        # specs/01-run-contracts.md:222).
        response = ok_response(payload)
        _store_response(conn, run_id, request_id, namespace, response, now)
        if services.ledger is not None:
            services.ledger.bankruptcy_check()
        commit(conn)
    except EngineError as exc:
        err = exc
    except Exception as exc:
        # A non-EngineError bug (sqlite3.Error, TypeError, an adapter crash,
        # ...) must not propagate raw with the database transaction left open
        # and its writes uncommitted-but-visible (spec 01 §Failure and
        # transaction semantics, specs/01-run-contracts.md:222). Folding it
        # into the same `err` path as EngineError means the rollback/response
        # logic below — which already runs on every non-success path — handles
        # this one too, with no separate code path to keep in sync.
        err = EngineError(
            ErrorCode.UNEXPECTED_FAILURE,
            "an unexpected internal error occurred",
            details={"exception_type": type(exc).__name__},
        )
    finally:
        # Always release the RNG txn guard so a rollbacking failure path can open
        # the follow-up mini-transaction cleanly.
        rng.exit_txn()

    if err is not None:
        # Discard every write / draw / scheduled event this attempt produced.
        # Guarded: an `fn` that commits/rolls back `ctx.conn` itself (or an
        # sqlite error class that auto-rolls-back) can leave no transaction
        # open by the time the error surfaces here, and an unconditional
        # ROLLBACK would raise its own raw OperationalError in that case.
        if conn.in_transaction:
            rollback(conn)
        response = err.to_response()
        if err.code not in NON_CACHEABLE_ERRORS:
            # store the structured error so a retry is byte-identical and
            # re-runs nothing.
            begin(conn)
            _store_response(conn, run_id, request_id, namespace, response, now)
            commit(conn)
        return response

    return response


def command(services: Services, request_id: str, fn: Fn) -> bytes:
    """A public command mutation: idempotent by ``(run_id, request_id)``.

    Requires ``run.status == active`` and an active invocation. A reserved
    (controller-privileged) ``request_id`` is rejected with the uniform
    authorization error before any transaction opens.
    """
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
    """A controller-privileged transaction (world-event apply, settlement,
    checkpoint, run finalization, ``mark_call_unanswered``).

    ``request_id`` MUST be in the reserved namespace (use
    :func:`privileged_request_id`); a non-reserved id is a controller misuse.
    Controller-privileged ops are unreachable from the public surface; probing
    them via :func:`command` returns the uniform authorization error.
    """
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
