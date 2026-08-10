"""The append-only integer-cent ledger, running balances, and bankruptcy
(spec 02 §4.7; spec 06 §9.1, §9.4; CSR-002 decision 9).

``_ledger_entries`` is the spec-02 shape: each row is ``(occurred_at,
service_date, entry_type, amount_cents, balance_after_cents)`` plus the six
nullable source links (``job_id``, ``invoice_id``, ``payment_id``,
``technician_id``, ``hiring_request_id``, ``training_reservation_id``), a
description, ``created_at``, and the private ``origin``. Money is signed
integer cents. **Cash is the ledger**: ``cash_balance = Σ amount_cents`` over
all committed rows — the opening adjustment is an ordinary committed row, so
it is included — and ``balance_after_cents`` is the running total at each row
and Must equal that sum (spec 02 §4.7).

``entry_type`` is the closed 9-member set of spec 06 §9.1 (copied verbatim in
spec 02 §4.7) and mirrored by :class:`LedgerCategory`. Signs follow the spec
table and are enforced at entry construction: ``revenue`` Must be positive,
every cost type Must be negative (zero is rejected for both — it is neither
sign), and ``opening_balance_adjustment`` may be either sign or zero.

The table is append-only at the storage layer: the migration ships permanent
UPDATE/DELETE guard triggers (CSR-002 decision 15), so a ledger row can never
be rewritten or removed — only grown. ACC-13's structural form (the table,
the closed enum, the six link columns, and exact running balances, with
ACC-12's single opening-adjustment pinning row) ships in this ticket; the
full reconciliation property — every real cash movement maps to one typed,
linked entry — is CSR-011 (decision 9).

:meth:`Ledger.require_cash` is the cash-sufficiency guard a discretionary
command with an immediate cost calls in its validation stage, before any write
or random draw (spec 01 §Command transactions validation order,
specs/01-run-contracts.md:217): it raises ``INSUFFICIENT_CASH`` rather than
letting the command's ``fn`` write, so the whole transaction rolls back with
no ledger row, draw, or other trace.

Bankruptcy rule (spec 01 invariants #1, §Money and bankruptcy; spec 06 §9.4):
after every committed transaction containing a financial effect,
``cash_balance < 0`` flips run status to ``bankrupt`` and the run halts.
Because discretionary commands are cash-guarded *before* they write
(:meth:`Ledger.require_cash`), bankruptcy can arise only from world events
applied between invocations or from the daily settlement — both of which run
as controller-privileged transactions; :meth:`Ledger.bankruptcy_check` is
called inside that same transaction so the status flip is atomic with the
effect that caused it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import clock
from .db import set_run_status
from .enums import LedgerCategory, RunStatus
from .errors import EngineError, ErrorCode
from .money import Money

#: The legal provenance values of a ledger row (spec 02 §1.3).
_LEGAL_ORIGINS = frozenset({"run", "warm_start"})

#: The enforced sign rules of the spec 06 §9.1 / spec 02 §4.7 table:
#: ``revenue`` is the only positive member; the seven cost types are negative;
#: ``opening_balance_adjustment`` (not listed here) may be either sign.
_POSITIVE_AMOUNT_TYPES = frozenset({LedgerCategory.REVENUE})
_NEGATIVE_AMOUNT_TYPES = frozenset(
    {
        LedgerCategory.MATERIAL_COST,
        LedgerCategory.PAYROLL_BASE,
        LedgerCategory.PAYROLL_OFF_HOURS,
        LedgerCategory.PAYROLL_OFF_HOURS_BONUS,
        LedgerCategory.RECRUITING_COST,
        LedgerCategory.TRAINING_COST,
        LedgerCategory.OPERATING_OVERHEAD,
    }
)


@dataclass(frozen=True)
class LedgerEntry:
    """One append-only ledger row (spec 02 §4.7)."""

    entry_type: LedgerCategory
    amount_cents: int
    occurred_at_ms: int
    #: Phoenix-local ``YYYY-MM-DD``; derived from ``occurred_at_ms`` at insert
    #: time when omitted (decision 9: a warm-start row may carry a date before
    #: the run clock starts, and the pure derivation honors that).
    service_date: str | None = None
    description: str = ""
    job_id: int | None = None
    invoice_id: int | None = None
    payment_id: int | None = None
    technician_id: int | None = None
    hiring_request_id: int | None = None
    training_reservation_id: int | None = None
    origin: str = "run"

    def __post_init__(self) -> None:
        if isinstance(self.amount_cents, bool) or not isinstance(self.amount_cents, int):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"amount_cents must be int, got {type(self.amount_cents).__name__}",
            )
        if not isinstance(self.entry_type, LedgerCategory):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL, f"ledger category not in enum: {self.entry_type!r}"
            )
        if self.entry_type in _POSITIVE_AMOUNT_TYPES and self.amount_cents <= 0:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"{self.entry_type.value} amount_cents must be positive, got {self.amount_cents}",
            )
        if self.entry_type in _NEGATIVE_AMOUNT_TYPES and self.amount_cents >= 0:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"{self.entry_type.value} amount_cents must be negative, got {self.amount_cents}",
            )
        if isinstance(self.occurred_at_ms, bool) or not isinstance(self.occurred_at_ms, int):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"occurred_at_ms must be int, got {type(self.occurred_at_ms).__name__}",
            )
        if not isinstance(self.description, str):
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"description must be str, got {type(self.description).__name__}",
            )
        if self.origin not in _LEGAL_ORIGINS:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"origin must be one of {sorted(_LEGAL_ORIGINS)}, got {self.origin!r}",
            )


class Ledger:
    """The run ledger — writes go inside the caller's open transaction."""

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        # Decision 4: one run per database, so there is no run_id — every
        # query is unfiltered.
        self.conn = conn

    def post(self, entries: list[LedgerEntry]) -> None:
        """Insert ledger rows inside the caller's transaction.

        ``balance_after_cents`` is computed here, in caller order, on top of
        the current sum: one writer and one transaction, so the stored running
        total can never disagree with the sum (decision 9; spec 02 §4.7).

        Called only while a transaction is open so a rollback discards the rows
        (spec 01 invariants #4/#5 — a rolled-back command leaves no trace).
        Guarded the same way as its siblings (``Events.schedule``, events.py:70;
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
        running = int(
            self.conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM _ledger_entries"
            ).fetchone()["s"]
        )
        rows = []
        for e in entries:
            running += e.amount_cents
            # created_at == occurred_at: the audit columns of spec 02 §1.3 exist
            # for mutable rows, and a ledger row is append-only — creation *is*
            # occurrence, and occurrence time is the only honest simulated-time
            # value a ledger row has (never wall clock).
            rows.append(
                (
                    e.occurred_at_ms,
                    e.service_date if e.service_date is not None
                    else clock.service_date_for_ms(e.occurred_at_ms),
                    e.entry_type.value,
                    e.amount_cents,
                    running,
                    e.job_id,
                    e.invoice_id,
                    e.payment_id,
                    e.technician_id,
                    e.hiring_request_id,
                    e.training_reservation_id,
                    e.description,
                    e.occurred_at_ms,
                    e.origin,
                )
            )
        self.conn.executemany(
            """INSERT INTO _ledger_entries
               (occurred_at, service_date, entry_type, amount_cents,
                balance_after_cents, job_id, invoice_id, payment_id,
                technician_id, hiring_request_id, training_reservation_id,
                description, created_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def cash_balance(self) -> int:
        """Opening adjustment + Σ committed entries (an exact integer).

        Unfiltered per decision 4 (one run per database); the opening
        adjustment is an ordinary committed row and is therefore included
        (spec 02 §4.7).
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM _ledger_entries"
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
        row = self.conn.execute("SELECT COUNT(*) AS c FROM _ledger_entries").fetchone()
        return int(row["c"])

    def bankruptcy_check(self) -> bool:
        """If cash is negative and the run is active, flip to ``bankrupt``.

        Returns ``True`` iff the run became bankrupt in this call (idempotent).
        Called inside the transaction that produced the financial effect so the
        status change commits atomically with the effect (no partial-day
        settle; spec 06 §9.4). Guarded like :meth:`post` for the same reason:
        the connection is autocommit, so outside a transaction a status flip
        would commit immediately and could never be rolled back with the
        effect that caused it.

        Only flips from an active run; once terminal the run is halted and this
        is a no-op (spec 01 invariant #9). Not parametrized on the "from"
        status — ``active`` is the only legal source of ``bankrupt``
        (``_LEGAL_RUN_TRANSITIONS``, db.py), and this is the sole call site.
        """
        if not self.conn.in_transaction:
            raise EngineError(
                ErrorCode.DRAW_OUTSIDE_TXN,
                "Ledger.bankruptcy_check is callable only inside an open transaction",
            )
        row = self.conn.execute("SELECT run_id, status FROM _run").fetchone()
        if row is None:
            raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "run record not found")
        status = RunStatus(row["status"])
        if status is not RunStatus.ACTIVE:
            return False
        if self.cash_balance() < 0:
            # Delegates the actual transition to db.set_run_status rather than
            # issuing its own raw UPDATE, so the legal-transition check lives
            # in exactly one place.
            set_run_status(self.conn, row["run_id"], RunStatus.BANKRUPT)
            return True
        return False


__all__ = ["Ledger", "LedgerEntry"]
