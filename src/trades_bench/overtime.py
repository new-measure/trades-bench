from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from . import clock, constants
from .enums import DayType, EventKind

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


def _overlap_ms(span: Interval, paid: list[Interval]) -> int:
    start, end = span
    covered = 0
    cursor = start
    for paid_start, paid_end in sorted(paid):
        lo = max(cursor, paid_start)
        hi = min(end, paid_end)
        if hi > lo:
            covered += hi - lo
            cursor = hi
    return covered


def overtime_minutes(visit_span: Interval, eligible_from: int, paid: list[Interval]) -> int:
    start = max(visit_span[0], eligible_from)
    end = visit_span[1]
    if end <= start:
        return 0
    return (end - start - _overlap_ms((start, end), paid)) // MINUTE_MS


def office_shift_end(service_date: str) -> int:
    end_minute = constants.BLOCK_GRID_MINUTES[constants.OFFICE_BLOCK_COUNT - 1][1]
    d = date.fromisoformat(service_date)
    return clock.phoenix_instant(d.year, d.month, d.day, end_minute // 60, end_minute % 60)


def visit_overtime_minutes(
    *,
    service_date: str,
    scheduled_start: int,
    departed_at: int,
    ended_at: int,
    paid_off_hours: list[Interval],
) -> int:
    weekday = (
        DayType.from_iso_weekday(date.fromisoformat(service_date).isoweekday())
        is DayType.WEEKDAY
    )
    eligible_from = office_shift_end(service_date) if weekday else scheduled_start
    return overtime_minutes((departed_at, ended_at), eligible_from, paid_off_hours)


def overtime_cents(minutes: int, hourly_wage_cents: int) -> int:
    return (minutes * hourly_wage_cents + MINUTES_PER_HOUR // 2) // MINUTES_PER_HOUR


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


def _paid_off_hours_windows(
    conn: sqlite3.Connection, day_start: int, day_end: int
) -> dict[int, list[Interval]]:
    windows: dict[int, list[Interval]] = {}
    marks = conn.execute(
        "SELECT payload FROM _events WHERE kind = ? AND status = 'applied' "
        "AND scheduled_at_ms >= ? AND scheduled_at_ms < ? ORDER BY order_key, event_id",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, day_start, day_end),
    ).fetchall()
    for mark in marks:
        payload = json.loads(mark["payload"]) if mark["payload"] else {}
        if "technician_id" not in payload:
            continue
        appointment = conn.execute(
            "SELECT scheduled_start, scheduled_end FROM _appointments WHERE id = ?",
            (int(payload["appointment_id"]),),
        ).fetchone()
        windows.setdefault(int(payload["technician_id"]), []).append(
            (int(appointment["scheduled_start"]), int(appointment["scheduled_end"]))
        )
    return windows


def overtime_pay(
    conn: sqlite3.Connection, service_date: str, *, day_start: int, day_end: int
) -> list[OvertimePay]:
    off_hours = _paid_off_hours_windows(conn, day_start, day_end)
    pay: list[OvertimePay] = []
    for visit in _worked_visits(conn, service_date):
        minutes = visit_overtime_minutes(
            service_date=service_date,
            scheduled_start=visit.scheduled_start,
            departed_at=visit.departed_at,
            ended_at=visit.ended_at,
            paid_off_hours=off_hours.get(visit.technician_id, []),
        )
        wage = conn.execute(
            "SELECT hourly_wage_cents FROM _technicians WHERE id = ?", (visit.technician_id,)
        ).fetchone()
        amount = overtime_cents(minutes, int(wage["hourly_wage_cents"]))
        if amount > 0:
            pay.append(OvertimePay(visit.technician_id, visit.job_id, minutes, amount))
    return pay


__all__ = [
    "OvertimePay",
    "WorkedVisit",
    "office_shift_end",
    "overtime_cents",
    "overtime_minutes",
    "overtime_pay",
    "visit_overtime_minutes",
]
