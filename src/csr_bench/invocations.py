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
            return {"invocation_id": invocation_id, "status": "completed", "outcome": "unanswered"}

        return privileged(
            services,
            privileged_request_id("mark_unanswered", key),
            inner,
            require_active_invocation=False,
        )


__all__ = ["Invocations"]
