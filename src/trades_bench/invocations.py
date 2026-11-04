from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock
from .enums import InvocationStatus, InvocationType
from .errors import EngineError, ErrorCode
from .txn import fingerprint, privileged_request_id, privileged_with_replay

PROMPT_TEMPLATE_VERSION = "1.1.0"


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

    def open(
        self,
        kind: InvocationType,
        ts_ms: int,
        *,
        request_id: str,
        prompt_version: str | None = None,
        origin_event_id: int | None = None,
        services: Any,
    ) -> int | bytes:
        result, _replayed = self.open_with_replay(
            kind,
            ts_ms,
            request_id=request_id,
            prompt_version=prompt_version,
            origin_event_id=origin_event_id,
            services=services,
        )
        return result

    def open_with_replay(
        self,
        kind: InvocationType,
        ts_ms: int,
        *,
        request_id: str,
        prompt_version: str | None = None,
        origin_event_id: int | None = None,
        services: Any,
    ) -> tuple[int | bytes, bool]:
        origin = None if origin_event_id is None else int(origin_event_id)

        def inner(ctx) -> dict:
            if services.invocations.active() is not None:
                raise EngineError(
                    ErrorCode.INVOCATION_CONFLICT,
                    "an invocation is already active; cannot open a second",
                    details={"reason": "invocation_already_active"},
                )
            seq = self._next_sequence()
            cur = ctx.conn.execute(
                """INSERT INTO _invocations
                   (type, ts_ms, sequence, status, prompt_version, origin_event_id)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (kind.value, int(ts_ms), seq, prompt_version, origin),
            )
            clock.advance(ctx.conn, int(ts_ms))
            return {"invocation_id": int(cur.lastrowid), "sequence": seq}

        args = {
            "kind": kind.value,
            "ts_ms": int(ts_ms),
            "prompt_version": prompt_version,
            "origin_event_id": origin,
        }
        resp, replayed = privileged_with_replay(
            services,
            privileged_request_id("open_inv", request_id),
            inner,
            require_active_invocation=False,
            fingerprint_value=fingerprint("invocations.open", args),
        )
        body = json.loads(resp)
        if not body.get("ok"):
            return resp, replayed
        return int(body["result"]["invocation_id"]), replayed

    def complete(self, invocation_id: int, *, request_id: str, services: Any) -> bytes:
        response, _replayed = self.complete_with_replay(
            invocation_id, request_id=request_id, services=services
        )
        return response

    def complete_with_replay(
        self, invocation_id: int, *, request_id: str, services: Any
    ) -> tuple[bytes, bool]:

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

        args = {"invocation_id": int(invocation_id)}
        return privileged_with_replay(
            services,
            privileged_request_id("complete_inv", request_id),
            inner,
            require_active_invocation=False,
            fingerprint_value=fingerprint("invocations.complete", args),
        )


__all__ = ["PROMPT_TEMPLATE_VERSION", "Invocations"]
