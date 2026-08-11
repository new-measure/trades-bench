from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import clock, constants, db
from .config import RunConfig
from .enums import LedgerCategory, RunStatus
from .errors import EngineError, ErrorCode
from .ledger import Ledger, LedgerEntry

BOUNDARY_MS = clock.phoenix_instant(2026, 10, 1, 6, 45)

RECENT_HIRE_JOINED_ON = "2026-08-03"

HIRING_LEAD_DAYS = 33

RECRUITING_COST_WAGE_MULTIPLE = 10

_ROSTER: dict[int, tuple[str, str, str, str, str, float, float, str]] = {
    1: ("Marcus Vega", "2218 N 16th St", "", "Phoenix", "85006", 33.5019, -112.0441, "2022-05-02"),
    2: ("Diana Chen", "4520 N 72nd St", "", "Scottsdale", "85251", 33.5021, -111.9275, RECENT_HIRE_JOINED_ON),
    3: ("Roberto Salas", "1638 S Country Club Dr", "", "Mesa", "85210", 33.4078, -111.8354, "2023-08-14"),
    4: ("Katherine Osei", "5940 W Glendale Ave", "", "Glendale", "85301", 33.5387, -112.1889, "2025-02-10"),
}

_HOME_ZONES: dict[int, int] = {1: 1, 2: 3, 3: 5, 4: 8}

_QUALIFICATIONS: dict[int, tuple[str, ...]] = {
    1: ("hvac_service",),
    2: ("hvac_service", "hvac_install"),
    3: ("plumbing",),
    4: ("electrical",),
}

_ROTATIONS: dict[int, int] = {1: 1, 2: 3, 3: 4}

_TRUE_SKILL: dict[tuple[int, str], float] = {
    (1, "hvac_service"): 0.72,
    (2, "hvac_service"): 0.81,
    (2, "hvac_install"): 0.68,
    (3, "plumbing"): 0.77,
    (4, "electrical"): 0.74,
}


def _date_instant(iso_date: str) -> int:
    d = date.fromisoformat(iso_date)
    return clock.phoenix_instant(d.year, d.month, d.day, 0, 0)


def _day_instant(d: date, hour: int, minute: int) -> int:
    return clock.phoenix_instant(d.year, d.month, d.day, hour, minute)


def _calendar_dates(start: str, end: str) -> list[date]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    out: list[date] = []
    d = d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def _insert_shifts(conn: sqlite3.Connection) -> None:
    holder_rows = sorted(_ROTATIONS.items(), key=lambda item: item[1])
    holders: dict[int, int] = {tech_id: rotation_id for rotation_id, tech_id in holder_rows}
    rows: list[tuple] = []
    for d in _calendar_dates(constants.RUN_START, constants.RUN_END):
        iso = d.isoformat()
        if d.weekday() < 5:
            for tech_id in (1, 2, 3, 4):
                rows.append(
                    (tech_id, "office", _day_instant(d, 7, 0), _day_instant(d, 17, 0),
                     iso, 1, None, BOUNDARY_MS)
                )
            for tech_id, rotation_id in holders.items():
                rows.append(
                    (tech_id, "after_hours", _day_instant(d, 17, 0), _day_instant(d, 19, 0),
                     iso, 1, rotation_id, BOUNDARY_MS)
                )
        else:
            for tech_id, rotation_id in holders.items():
                rows.append(
                    (tech_id, "weekend", _day_instant(d, 7, 0), _day_instant(d, 19, 0),
                     iso, 1, rotation_id, BOUNDARY_MS)
                )
    conn.executemany(
        """INSERT INTO _technician_shifts
           (technician_id, shift_type, start_at, end_at, service_date, active,
            on_call_rotation_id, created_at, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'warm_start')""",
        rows,
    )


def _build_fixture(conn: sqlite3.Connection, config: RunConfig) -> None:
    recent_hire_id = config.get_int("constants.recent_hire_technician_id")
    if _ROSTER[recent_hire_id][7] != RECENT_HIRE_JOINED_ON:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"roster entry for constants.recent_hire_technician_id={recent_hire_id} "
            f"must join inside the window on {RECENT_HIRE_JOINED_ON} (spec 02 §8.4)",
        )
    wages_by_trade = config.resolved["hiring.base_hourly_wage_by_trade"]
    starting_cash = config.get_int("finance.starting_cash")

    rows = []
    for tech_id in sorted(_ROSTER):
        name, street, unit, city, postal, lat, lon, joined_on = _ROSTER[tech_id]
        quals = _QUALIFICATIONS[tech_id]
        wage = max(wages_by_trade[q] for q in quals)
        rows.append(
            (
                tech_id, name, street, unit, city, "AZ", postal, lat, lon,
                _HOME_ZONES[tech_id], joined_on, wage,
                _date_instant(joined_on), BOUNDARY_MS,
            )
        )
    conn.executemany(
        """INSERT INTO _technicians
           (id, name, street, unit, city, state, postal_code, home_latitude,
            home_longitude, home_zone_id, joined_on, hourly_wage_cents,
            employment_status, availability_status, created_at, modified_at, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'available', ?, ?, 'warm_start')""",
        rows,
    )

    skill_rows = []
    skill_id = 0
    for tech_id in sorted(_QUALIFICATIONS):
        joined_ms = _date_instant(_ROSTER[tech_id][7])
        for code in _QUALIFICATIONS[tech_id]:
            skill_id += 1
            skill_rows.append((skill_id, tech_id, code, 1, _ROSTER[tech_id][7], joined_ms))
    conn.executemany(
        """INSERT INTO _technician_skills
           (id, technician_id, skill_code, active, granted_on, created_at, origin)
           VALUES (?, ?, ?, ?, ?, ?, 'warm_start')""",
        skill_rows,
    )

    rotation_rows = [
        (
            rotation_id, business_unit_id, tech_id, "2026-04-01", None, "active", None,
            _date_instant("2026-04-01"), BOUNDARY_MS,
        )
        for rotation_id, (business_unit_id, tech_id) in enumerate(
            sorted(_ROTATIONS.items()), start=1
        )
    ]
    conn.executemany(
        """INSERT INTO _on_call_rotations
           (id, business_unit_id, technician_id, effective_start_date,
            effective_end_date, status, vacated_at, created_at, modified_at, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'warm_start')""",
        rotation_rows,
    )

    _insert_shifts(conn)

    conn.executemany(
        """INSERT INTO _technician_locations
           (technician_id, latitude, longitude, coordinates_updated_at, location_state)
           VALUES (?, ?, ?, ?, 'home')""",
        [
            (tech_id, lat, lon, BOUNDARY_MS)
            for tech_id, (_, _, _, _, _, lat, lon, _) in sorted(_ROSTER.items())
        ],
    )

    conn.executemany(
        """INSERT INTO _technician_true_skill
           (technician_id, skill_code, base_skill, training_gain, updated_at)
           VALUES (?, ?, ?, 0, ?)""",
        [
            (tech_id, code, value, BOUNDARY_MS)
            for (tech_id, code), value in sorted(_TRUE_SKILL.items())
        ],
    )
    conn.executemany(
        """INSERT INTO _technician_strain (technician_id, strain_value, last_updated_date)
           VALUES (?, 0.0, '2026-09-30')""",
        [(tech_id,) for tech_id in sorted(_ROSTER)],
    )

    recent_quals = _QUALIFICATIONS[recent_hire_id]
    recent_wage = max(wages_by_trade[q] for q in recent_quals)
    recent_name = _ROSTER[recent_hire_id][0]
    requested_on = date.fromisoformat(RECENT_HIRE_JOINED_ON) - timedelta(days=HIRING_LEAD_DAYS)
    requested_at = _day_instant(requested_on, 9, 0)
    hiring_cur = conn.execute(
        """INSERT INTO _hiring_requests
           (requested_at, requested_skill_codes_json, minimum_experience_years,
            preferred_home_zone_id, status, recruiting_cost_cents,
            committed_hourly_wage_cents, committed_start_date, technician_id,
            created_at, modified_at)
           VALUES (?, ?, 3, ?, 'completed', ?, ?, ?, ?, ?, ?)""",
        (
            requested_at,
            json.dumps(list(recent_quals), separators=(",", ":")),
            _HOME_ZONES[recent_hire_id],
            recent_wage * RECRUITING_COST_WAGE_MULTIPLE,
            recent_wage,
            RECENT_HIRE_JOINED_ON,
            recent_hire_id,
            requested_at,
            _date_instant(RECENT_HIRE_JOINED_ON),
        ),
    )
    hiring_request_id = int(hiring_cur.lastrowid if hiring_cur.lastrowid is not None else 1)

    ledger = Ledger(conn)
    ledger.post(
        [
            LedgerEntry(
                LedgerCategory.RECRUITING_COST,
                -recent_wage * RECRUITING_COST_WAGE_MULTIPLE,
                requested_at,
                description=f"Recruiting cost — {recent_name}",
                technician_id=recent_hire_id,
                hiring_request_id=hiring_request_id,
                origin="warm_start",
            )
        ]
    )
    adjustment = starting_cash - ledger.cash_balance()
    opening_pin_ms = clock.phoenix_instant(2026, 9, 30, 23, 59) + 59_000
    ledger.post(
        [
            LedgerEntry(
                LedgerCategory.OPENING_BALANCE_ADJUSTMENT,
                adjustment,
                opening_pin_ms,
                service_date="2026-09-30",
                description="Opening balance adjustment",
                origin="warm_start",
            )
        ]
    )

    if ledger.cash_balance() != starting_cash:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"warm-start ledger pins cash to {ledger.cash_balance()}, "
            f"expected finance.starting_cash = {starting_cash}",
        )
    if ledger.entry_count() != 2:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            "warm-start fixture ledger must be exactly 2 rows "
            "(recruiting cost + opening balance adjustment), "
            f"got {ledger.entry_count()}",
        )


def run(
    conn: sqlite3.Connection,
    run_id: str,
    master_seed: int,
    config: RunConfig,
    dataset_version: str,
) -> None:
    row = db.get_run(conn, run_id)
    if row is None or RunStatus(row["status"]) is not RunStatus.INITIALIZING:
        raise EngineError(
            ErrorCode.INVALID_RUN_STATUS,
            "warm start requires run.status = 'initializing' (spec 02 §8.1: "
            "no invocation may occur during it)",
        )
    if conn.in_transaction:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            "warm_start.run requires no open transaction on the connection",
        )
    db.begin(conn)
    try:
        _build_fixture(conn, config)
        db.commit(conn)
    except EngineError:
        db.rollback(conn)
        raise
    except Exception as exc:
        db.rollback(conn)
        raise EngineError(
            ErrorCode.UNEXPECTED_FAILURE,
            "warm start failed",
        ) from exc


__all__ = ["BOUNDARY_MS", "run"]
