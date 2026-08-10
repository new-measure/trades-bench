"""Event envelope + ordering + atomic apply (spec 01 §Event envelope and ordering,
§Invariants #3/#4; §Command transactions for the transactional apply).

Every world event uses one envelope ordered strictly by
``(scheduled_at, priority, event_id)`` (``event_id`` is monotonically assigned
within the run, so it is a deterministic tiebreaker). Event kinds come from the
central registry (:mod:`csr_bench.enums`); priorities are stubs now — their
meaningful table lands with CSR-005/011 (owned by spec 07).

Event application is atomic together with every resulting record, ledger entry,
notification row, newly scheduled event, and random-state change, and an applied
event is never reapplied. Events apply only while no invocation is active. A
cancelled event leaves no world effect other than its own status row.

``Events.schedule`` is callable only inside an open transaction so a rollback
discards the scheduled event together with any draw that produced it
(spec 01 §Interfaces).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from . import clock
from .enums import EventKind, EventStatus
from .errors import EngineError, ErrorCode
from .txn import TxnContext, privileged, privileged_request_id

EventHandler = Callable[[TxnContext, sqlite3.Row], dict | None]


class Events:
    """The world-event envelope table owner. Construct with the shared rng so
    :meth:`schedule` can enforce the open-transaction guard."""

    __slots__ = ("conn", "rng")

    def __init__(self, conn: sqlite3.Connection, rng) -> None:
        self.conn = conn
        self.rng = rng

    # ------------------------------------------------------------------
    def _require_txn(self, what: str) -> None:
        if not self.rng.is_txn_open():
            raise EngineError(
                ErrorCode.DRAW_OUTSIDE_TXN,
                f"{what} is callable only inside an open transaction",
            )

    def schedule(
        self,
        kind: EventKind,
        scheduled_at: int,
        priority: int,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        payload: dict | None = None,
        payload_version: int = 1,
    ) -> int:
        """Insert a ``pending`` event and return its monotonically assigned id."""
        self._require_txn("Events.schedule")
        if not isinstance(kind, EventKind):
            raise EngineError(ErrorCode.STATE_ILLEGAL, f"unknown event kind {kind!r}")
        if not isinstance(scheduled_at, int) or scheduled_at < 0:
            raise EngineError(ErrorCode.STATE_ILLEGAL, "scheduled_at_ms must be a non-negative int")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise EngineError(ErrorCode.STATE_ILLEGAL, "priority must be an int")
        payload_json = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if payload is not None
            else None
        )
        now = clock.now(self.conn)
        cur = self.conn.execute(
            """INSERT INTO _events
               (kind, scheduled_at_ms, priority, entity_type, entity_id,
                payload_version, payload, status, created_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                kind.value,
                scheduled_at,
                priority,
                entity_type,
                entity_id,
                payload_version,
                payload_json,
                now,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def cancel(self, event_id: int) -> None:
        """Transition ``pending -> cancelled``; a cancelled event leaves no effect."""
        self._require_txn("Events.cancel")
        cur = self.conn.execute(
            "UPDATE _events SET status = 'cancelled' WHERE event_id=? "
            "AND status = 'pending'",
            (event_id,),
        )
        if cur.rowcount == 0:
            row = self.get(event_id)
            if row is None:
                raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"event {event_id} not found")
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"event {event_id} not pending (status={row['status']})",
            )

    def get(self, event_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM _events WHERE event_id=?", (event_id,)
        ).fetchone()

    def status(self, event_id: int) -> EventStatus | None:
        row = self.get(event_id)
        return EventStatus(row["status"]) if row is not None else None

    def next_pending(self) -> sqlite3.Row | None:
        """Earliest pending event by ``(scheduled_at, priority, event_id)``."""
        return self.conn.execute(
            "SELECT * FROM _events WHERE status='pending' "
            "ORDER BY scheduled_at_ms, priority, event_id LIMIT 1",
        ).fetchone()

    def pending_in_order(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM _events WHERE status='pending' "
            "ORDER BY scheduled_at_ms, priority, event_id"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql).fetchall())

    def applied_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM _events WHERE status='applied'",
        ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    def apply(self, event_id: int, handler: EventHandler, *, services: Any) -> bytes:
        """Atomically apply event ``event_id`` via ``handler(ctx, event)``.

        Guarantees (spec 01 §Event envelope, §Invariants #3/#4):
        - world events apply only while no invocation is active;
        - an applied event is never reapplied (resume is idempotent via the
          privileged ``apply_event:<id>`` request id);
        - all resulting records / ledger / nested events / draws commit together
          and roll back together; after a committed financial effect the run
          may flip to ``bankrupt`` (handled by the privileged txn layer).
        """

        def inner(ctx: TxnContext) -> dict:
            if services.invocations is not None and services.invocations.active() is not None:
                raise EngineError(
                    ErrorCode.INVOCATION_CONFLICT,
                    "world event cannot apply while an invocation is active",
                )
            row = ctx.conn.execute(
                "SELECT * FROM _events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"event {event_id} not found")
            cur = ctx.conn.execute(
                "UPDATE _events SET status='processing' WHERE event_id=? "
                "AND status='pending'",
                (event_id,),
            )
            if cur.rowcount == 0:
                status = row["status"]
                raise EngineError(
                    ErrorCode.EVENT_ALREADY_APPLIED
                    if status == "applied"
                    else ErrorCode.STATE_ILLEGAL,
                    f"event {event_id} not re-applicable (status={status})",
                )
            handler(ctx, row)
            now = clock.now(ctx.conn)
            ctx.conn.execute(
                "UPDATE _events SET status='applied', applied_at_ms=? "
                "WHERE event_id=?",
                (now, event_id),
            )
            return {"event_id": event_id, "status": "applied"}

        return privileged(
            services,
            privileged_request_id("apply_event", event_id),
            inner,
            require_active_invocation=False,
        )


__all__ = ["Events", "EventHandler"]
