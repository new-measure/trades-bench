from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from . import clock, constants
from .money import basis_point_amount

MINUTE_MS = 60_000
MINUTES_PER_HOUR = 60

Interval = tuple[int, int]


@dataclass(frozen=True)
class OvertimePay:

    technician_id: int
    job_id: int
    minutes: int
    amount_cents: int


@dataclass(frozen=True)
class WorkedVisit:

    appointment_id: int
    job_id: int
    technician_id: int
    scheduled_start: int
    departed_at: int
    ended_at: int


def overtime_minutes(visit_span: Interval, paid_until: int) -> int:
    start = max(visit_span[0], paid_until)
    return max(0, visit_span[1] - start) // MINUTE_MS


def office_shift_end(service_date: str) -> int:
    end_minute = constants.BLOCK_GRID_MINUTES[constants.OFFICE_BLOCK_COUNT - 1][1]
    d = date.fromisoformat(service_date)
    return clock.phoenix_instant(d.year, d.month, d.day, end_minute // 60, end_minute % 60)


def paid_until(conn: sqlite3.Connection, technician_id: int, service_date: str) -> int:
    row = conn.execute(
        "SELECT MAX(end_at) AS end_at FROM _technician_shifts "
        "WHERE technician_id = ? AND service_date = ? AND active = 1",
        (technician_id, service_date),
    ).fetchone()
    if row["end_at"] is None:
        return office_shift_end(service_date)
    return int(row["end_at"])


def overtime_cents(minutes: int, hourly_wage_cents: int) -> int:
    return (minutes * hourly_wage_cents + MINUTES_PER_HOUR // 2) // MINUTES_PER_HOUR


def paid_invoice_subtotal_cents(conn: sqlite3.Connection, appointment_id: int) -> int | None:
    row = conn.execute(
        "SELECT i.subtotal_cents AS subtotal_cents FROM _appointments a "
        "JOIN _invoices i ON i.job_id = a.job_id "
        "WHERE a.id = ? AND a.status = 'completed' AND i.status = 'paid'",
        (appointment_id,),
    ).fetchone()
    return None if row is None else int(row["subtotal_cents"])


def capped_overtime_cents(
    hourly_cents: int, paid_subtotal_cents: int | None, cap_basis_points: int
) -> int:
    if paid_subtotal_cents is None:
        return hourly_cents
    return min(hourly_cents, basis_point_amount(paid_subtotal_cents, cap_basis_points))


def _worked_visits(conn: sqlite3.Connection, service_date: str) -> list[WorkedVisit]:
    rows = conn.execute(
        "SELECT t.appointment_id AS appointment_id, a.job_id AS job_id, "
        "a.scheduled_start AS scheduled_start, "
        "t.technician_id AS technician_id, t.occurred_at AS ended_at, "
        "(SELECT MAX(d.occurred_at) FROM _appointment_events d "
        " WHERE d.appointment_id = t.appointment_id AND d.event_type = 'dispatched' "
        " AND d.technician_id = t.technician_id) AS departed_at "
        "FROM _appointment_events t JOIN _appointments a ON a.id = t.appointment_id "
        "WHERE t.service_date = ? AND t.event_type IN ('work_completed', 'work_failed') "
        "AND t.technician_id IS NOT NULL ORDER BY t.technician_id, t.appointment_id",
        (service_date,),
    ).fetchall()
    visits = [
        WorkedVisit(
            appointment_id=int(row["appointment_id"]),
            job_id=int(row["job_id"]),
            technician_id=int(row["technician_id"]),
            scheduled_start=int(row["scheduled_start"]),
            departed_at=int(row["departed_at"]),
            ended_at=int(row["ended_at"]),
        )
        for row in rows
        if row["departed_at"] is not None
    ]
    return sorted(
        visits, key=lambda v: (v.technician_id, v.departed_at, v.ended_at, v.appointment_id)
    )


def overtime_pay(
    conn: sqlite3.Connection,
    service_date: str,
    *,
    cap_basis_points: int,
) -> list[OvertimePay]:
    pay: list[OvertimePay] = []
    for visit in _worked_visits(conn, service_date):
        minutes = overtime_minutes(
            (visit.departed_at, visit.ended_at),
            paid_until(conn, visit.technician_id, service_date),
        )
        wage = conn.execute(
            "SELECT hourly_wage_cents FROM _technicians WHERE id = ?", (visit.technician_id,)
        ).fetchone()
        amount = capped_overtime_cents(
            overtime_cents(minutes, int(wage["hourly_wage_cents"])),
            paid_invoice_subtotal_cents(conn, visit.appointment_id),
            cap_basis_points,
        )
        if amount > 0:
            pay.append(OvertimePay(visit.technician_id, visit.job_id, minutes, amount))
    return pay


__all__ = [
    "OvertimePay",
    "WorkedVisit",
    "capped_overtime_cents",
    "office_shift_end",
    "overtime_cents",
    "overtime_minutes",
    "overtime_pay",
    "paid_invoice_subtotal_cents",
    "paid_until",
]
