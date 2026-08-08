"""Append-only integer-cent ledger + cash balance + bankruptcy (spec 01
§Money and bankruptcy; decision 4).

Money is signed integer cents everywhere. The ledger is append-only: each entry
is ``(amount_cents, category, entity link, simulated timestamp)``.
``cash_balance()`` is the opening-balance adjustment plus the sum of all
committed entries — i.e. the sum of every ``_ledger`` row for the run, since the
opening adjustment is itself a committed entry.

:meth:`Ledger.require_cash` is the cash-sufficiency guard a discretionary
command with an immediate cost calls in its validation stage, before any write
or random draw (spec 01 §Command transactions validation order,
specs/01-run-contracts.md:217): it raises ``INSUFFICIENT_CASH`` rather than
letting the command's ``fn`` write, so the whole transaction rolls back with
no ledger row, draw, or other trace.

Bankruptcy rule (spec 01 invariants #1, §Money and bankruptcy): after every
committed transaction containing a financial effect, ``cash_balance < 0`` flips
run status to ``bankrupt`` and the run halts. Because discretionary commands are
cash-guarded *before* they write (:meth:`Ledger.require_cash`), bankruptcy can
arise only from world events applied between invocations or from the daily
settlement — both of which run as controller-privileged transactions;
:meth:`Ledger.bankruptcy_check` is called inside that same transaction so the
status flip is atomic with the effect that caused it.

Ledger categories start tiny (``opening_balance_adjustment`` plus the
contract-test driver categories); CSR-010 extends the enum.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import set_run_status
from .enums import LedgerCategory, RunStatus
from .errors import EngineError, ErrorCode
from .money import Money


@dataclass(frozen=True)
class LedgerEntry:
    """One append-only ledger row."""

    amount_cents: int
    category: LedgerCategory
    simulated_ts_ms: int
    entity_type: str | None = None
    entity_id: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.amount_cents, bool) or not isinstance(self.amount_cents, int):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"amount_cents must be int, got {type(self.amount_cents).__name__}",
            )
        if not isinstance(self.category, LedgerCategory):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL, f"ledger category not in enum: {self.category!r}"
            )


class Ledger:
    """The run ledger — writes go inside the caller's open transaction."""

    __slots__ = ("conn", "run_id")

    def __init__(self, conn: sqlite3.Connection, run_id: str) -> None:
        self.conn = conn
        self.run_id = run_id

    def post(self, entries: list[LedgerEntry]) -> None:
        """Insert ledger rows inside the caller's transaction.

        Called only while a transaction is open so a rollback discards the rows
        (spec 01 invariants #4/#5 — a rolled-back command leaves no trace). Guarded
        the same way as its siblings (``Events.schedule``, events.py:70;
        ``Rng.draw``, rng.py:383): the run connection is autocommit
        (``isolation_level=None``, db.py:164), so a post() outside an open
        transaction would commit immediately and no rollback could undo it.
        """
        if not self.conn.in_transaction:
            raise EngineError(
                ErrorCode.DRAW_OUTSIDE_TXN,
                "Ledger.post is callable only inside an open transaction",
            )
        if not entries:
            return
        self.conn.executemany(
            """INSERT INTO _ledger
               (run_id, amount_cents, category, entity_type, entity_id, simulated_ts_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    self.run_id,
                    e.amount_cents,
                    e.category.value,
                    e.entity_type.value if hasattr(e.entity_type, "value") else e.entity_type,
                    e.entity_id,
                    e.simulated_ts_ms,
                )
                for e in entries
            ],
        )

    def cash_balance(self) -> int:
        """Opening adjustment + Σ committed entries (an exact integer)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM _ledger WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        return int(row["s"])

    def cash_balance_money(self) -> Money:
        return Money(self.cash_balance())

    def require_cash(self, amount_cents: int) -> None:
        """Cash-sufficiency guard for a discretionary command's immediate cost
        (spec 01 §Money and bankruptcy — decision 4; §Command transactions
        validation order, specs/01-run-contracts.md:217).

        A discretionary command with an immediate cost calls this last in its
        validation stage, before any write or random draw: raising here rolls
        back the whole transaction, so a rejected spend leaves no ledger row,
        no draw, and no other trace (acceptance #7 / ACC-01 shape).
        """
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"amount_cents must be int, got {type(amount_cents).__name__}",
            )
        if self.cash_balance() < amount_cents:
            raise EngineError(
                ErrorCode.INSUFFICIENT_CASH, "discretionary spend exceeds available cash"
            )

    def entry_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM _ledger WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        return int(row["c"])

    def bankruptcy_check(self) -> bool:
        """If cash is negative and the run is active, flip to ``bankrupt``.

        Returns ``True`` iff the run became bankrupt in this call (idempotent).
        Called inside the transaction that produced the financial effect so the
        status change commits atomically with the effect (no partial-day settle).

        Only flips from an active run; once terminal the run is halted and this
        is a no-op (spec 01 invariant #9). Not parametrized on the "from"
        status — ``active`` is the only legal source of ``bankrupt``
        (``_LEGAL_RUN_TRANSITIONS``, db.py), and this is the sole call site.
        """
        row = self.conn.execute(
            "SELECT status FROM run WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        if row is None:
            raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"run {self.run_id!r} not found")
        status = RunStatus(row["status"])
        if status is not RunStatus.ACTIVE:
            return False
        if self.cash_balance() < 0:
            # Delegates the actual transition to db.set_run_status rather than
            # issuing its own raw UPDATE, so the legal-transition check lives
            # in exactly one place.
            set_run_status(self.conn, self.run_id, RunStatus.BANKRUPT)
            return True
        return False


__all__ = ["Ledger", "LedgerEntry"]
