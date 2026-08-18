from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import clock
from .db import set_run_status
from .enums import LedgerCategory, RunStatus
from .errors import EngineError, ErrorCode
from .money import Money

_LEGAL_ORIGINS = frozenset({"run", "warm_start"})

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

    entry_type: LedgerCategory
    amount_cents: int
    occurred_at_ms: int
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

    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def post(self, entries: list[LedgerEntry]) -> None:
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
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM _ledger_entries"
        ).fetchone()
        return int(row["s"])

    def cash_balance_money(self) -> Money:
        return Money(self.cash_balance())

    def require_cash(self, amount_cents: int) -> None:
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
            set_run_status(self.conn, row["run_id"], RunStatus.BANKRUPT)
            return True
        return False


__all__ = ["Ledger", "LedgerEntry"]
