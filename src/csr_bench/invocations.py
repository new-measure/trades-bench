"""Invocation exclusivity + lifecycle (spec 01 §Invocation contract, §Invariants
#2; acceptance #10 invocation exclusivity, #11 closing restriction).

At most one invocation is active at any time — opening a second fails — and world
events apply only while no invocation is active (enforced in
:mod:`csr_bench.events`). Each invocation record has an ID, timestamp, sequence
number, status, prompt version, and attempt count.

The engine owns only the terminal transitions (spec 01 §Invocation contract):
opening/closing invocations finish on a completed model response (the controller
calls :meth:`Invocations.complete`); a call invocation finishes through a
terminal call action (the domain owns that in CSR-008) or through the
controller-privileged :meth:`Invocations.mark_call_unanswered` after the third
incomplete attempt. Attempt counting belongs to the controller/adapter layer
(spec 08); this module exposes :meth:`record_attempt` as a privileged counter so
the three-attempt terminal can be exercised deterministically.

World time is frozen at the invocation timestamp while it is active; every
command validates against that frozen time (``ctx.now_ms``), and the active
invocation row is available on :class:`csr_bench.txn.TxnContext` so a command can
enforce the closing-day restriction (acceptance #11).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock
from .enums import InvocationStatus, InvocationType
from .errors import EngineError, ErrorCode
from .txn import privileged, privileged_request_id


class Invocations:
    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def active(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM _invocations WHERE status='active' "
            "ORDER BY sequence DESC LIMIT 1",
        ).fetchone()

    def get(self, invocation_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM _invocations WHERE invocation_id=?", (invocation_id,)
        ).fetchone()

    def _next_sequence(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM _invocations",
        ).fetchone()
        return int(row["s"]) + 1

    def _pre_state_key(self, invocation_id: int) -> str:
        """A pure function of the committed ``_invocations`` row, read once
        before the transaction opens. Repeated privileged calls against the same
        invocation (``complete``, ``record_attempt``, ``mark_call_unanswered``)
        must key their request id on the pre-call state, not on
        ``invocation_id`` alone, or a later call with a different expected
        outcome replays the first call's stored response instead of being
        re-evaluated. Reading only committed rows (no wall-clock, no in-memory
        counters) keeps this deterministic across a crash-retry (docs/CSR-001.md
        decision 6 / DET-02)."""
        row = self.get(invocation_id)
        if row is None:
            return f"{invocation_id}:missing"
        return f"{invocation_id}:{row['status']}:{row['attempt_count']}"

    def open(
        self,
        kind: InvocationType,
        ts_ms: int,
        *,
        prompt_version: str | None = None,
        services: Any,
    ) -> int | bytes:
        """Open an invocation; fails if one is already active (exclusivity).

        Returns the new invocation id on success, or the raw structured-error
        response bytes if opening was refused (e.g. ``invocation_conflict``).
        """

        # Computed once, outside the transaction: the request id must key on the
        # invocation being created (derived from committed rows), not a constant,
        # or every open() in a run collides in the idempotency cache. A
        # crash-retry re-derives the same next sequence from committed rows, so
        # this stays correctly deduped (DET-02 / decision 6).
        seq = self._next_sequence()

        def inner(ctx) -> dict:
            if services.invocations.active() is not None:
                raise EngineError(
                    ErrorCode.INVOCATION_CONFLICT,
                    "an invocation is already active; cannot open a second",
                )
            cur = ctx.conn.execute(
                """INSERT INTO _invocations
                   (type, ts_ms, sequence, status, prompt_version, attempt_count)
                   VALUES (?, ?, ?, 'active', ?, 0)""",
                (kind.value, int(ts_ms), seq, prompt_version),
            )
            # freeze world time at the invocation timestamp — the clock row is
            # the single time source (CSR-002 decision 3), so the freeze is a
            # clock.advance inside this same transaction.
            clock.advance(ctx.conn, int(ts_ms))
            return {"invocation_id": int(cur.lastrowid), "sequence": seq}

        resp = privileged(
            services,
            privileged_request_id("open_inv", seq),
            inner,
            require_active_invocation=False,
        )
        body = json.loads(resp)
        if not body.get("ok"):
            return resp
        return int(body["result"]["invocation_id"])

    def complete(self, invocation_id: int, *, services: Any) -> bytes:
        """Terminal transition: ``active -> completed`` for an open/close/call
        invocation that finished through a completed response."""

        key = self._pre_state_key(invocation_id)

        def inner(ctx) -> dict:
            row = self.get(invocation_id)
            if row is None:
                raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"invocation {invocation_id} not found")
            if row["status"] != InvocationStatus.ACTIVE.value:
                raise EngineError(
                    ErrorCode.STATE_ILLEGAL,
                    f"invocation {invocation_id} not active (status={row['status']})",
                )
            if self.active() is None or self.active()["invocation_id"] != invocation_id:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED, "a different invocation is active"
                )
            ctx.conn.execute(
                "UPDATE _invocations SET status='completed' WHERE invocation_id=?",
                (invocation_id,),
            )
            return {"invocation_id": invocation_id, "status": "completed"}

        return privileged(
            services,
            privileged_request_id("complete_inv", key),
            inner,
            require_active_invocation=False,
        )

    def record_attempt(self, invocation_id: int, *, services: Any) -> bytes:
        """Increment an invocation's attempt count (controller/adapter side)."""

        key = self._pre_state_key(invocation_id)

        def inner(ctx) -> dict:
            row = self.get(invocation_id)
            if row is None:
                raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"invocation {invocation_id} not found")
            if row["status"] != InvocationStatus.ACTIVE.value:
                raise EngineError(
                    ErrorCode.STATE_ILLEGAL, f"invocation {invocation_id} not active"
                )
            ctx.conn.execute(
                "UPDATE _invocations SET attempt_count = attempt_count + 1 "
                "WHERE invocation_id=?",
                (invocation_id,),
            )
            new = ctx.conn.execute(
                "SELECT attempt_count FROM _invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            return {"invocation_id": invocation_id, "attempt_count": int(new["attempt_count"])}

        return privileged(
            services,
            privileged_request_id("record_attempt", key),
            inner,
            require_active_invocation=False,
        )

    def mark_call_unanswered(self, invocation_id: int, *, services: Any) -> bytes:
        """The three-attempt terminal: complete a call after the third incomplete
        attempt. The engine owns only this terminal transition; the consequence
        branch (new-job/callback vs cancellation carve-out) belongs to the domain
        (spec 07/CSR-008).

        The *invocation* status this persists and returns is ``completed`` —
        :class:`csr_bench.enums.InvocationStatus` is a closed two-member enum
        (``active``/``completed``; decision 5) and ``unanswered`` is not a
        member of it. ``unanswered`` is the *call's* terminal status/
        ``terminal_reason`` (specs/02-data-and-initial-state.md §4.2,
        specs/05-customers-calls-and-demand.md §8.4) on the richer
        ``calls`` record that CSR-005/CSR-008 own, not this ticket's
        invocation record. The response therefore carries the enum-valid
        ``status`` plus a separate ``outcome`` field so a caller can still see
        which terminal path fired; the richer call-outcome record (the
        ``calls`` row, ``terminal_reason``, consequence branch) is deferred to
        CSR-008."""

        key = self._pre_state_key(invocation_id)

        def inner(ctx) -> dict:
            row = self.get(invocation_id)
            if row is None:
                raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"invocation {invocation_id} not found")
            if row["type"] != InvocationType.CALL.value:
                raise EngineError(
                    ErrorCode.STATE_ILLEGAL, "mark_call_unanswered requires a call invocation"
                )
            if row["status"] != InvocationStatus.ACTIVE.value:
                raise EngineError(
                    ErrorCode.STATE_ILLEGAL, f"invocation {invocation_id} not active"
                )
            if int(row["attempt_count"]) < 3:
                raise EngineError(
                    ErrorCode.STATE_ILLEGAL,
                    f"call {invocation_id} has only {row['attempt_count']} attempts "
                    "(three-attempt rule not reached)",
                )
            ctx.conn.execute(
                "UPDATE _invocations SET status='completed' WHERE invocation_id=?",
                (invocation_id,),
            )
            # status is the closed InvocationStatus value actually persisted;
            # outcome is the call-level terminal reason this transition
            # represents (not itself a member of InvocationStatus).
            return {"invocation_id": invocation_id, "status": "completed", "outcome": "unanswered"}

        return privileged(
            services,
            privileged_request_id("mark_unanswered", key),
            inner,
            require_active_invocation=False,
        )


__all__ = ["Invocations"]
