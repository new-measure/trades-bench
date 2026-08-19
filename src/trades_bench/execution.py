from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import appointments, clock, constants, geography, travel
from .config import RunConfig
from .enums import (
    ConditionCode,
    DayType,
    EntityType,
    EventKind,
    KeyedStreamName,
    LedgerCategory,
)
from .errors import EngineError, ErrorCode
from .events import EventHandler, Events
from .geo_reader import GeoArtifact
from .ledger import LedgerEntry
from .money import basis_point_amount
from .rng import Rng

BLOCK_CODES = appointments.BLOCK_CODES

_TERMINAL = appointments._TERMINAL_APPOINTMENT_STATUSES
_TERMINAL_PLACEHOLDERS = ", ".join("?" for _ in _TERMINAL)

_LATENESS_GRACE_MS = 10 * 60000


def block_index(block_code: str) -> int:
    return BLOCK_CODES.index(block_code)


def block_code_at(index: int) -> str:
    return BLOCK_CODES[index]


def block_start_instant(service_date: str, block_code: str) -> int:
    return appointments._instant(service_date, appointments._BLOCK_START_MINUTES[block_code])


def block_end_instant(service_date: str, block_code: str) -> int:
    return appointments._instant(service_date, appointments._BLOCK_END_MINUTES[block_code])


def day_type_for(service_date: str) -> DayType:
    return DayType.from_iso_weekday(date.fromisoformat(service_date).isoweekday())


def execution_duration_multiplier(
    config: RunConfig,
    *,
    skill_input: Decimal = Decimal(0),
    strain_input: Decimal = Decimal(0),
    complexity_input: Decimal = Decimal(0),
) -> Decimal:
    skill_term = config.get_decimal("quality.duration_skill_coefficient") * skill_input
    strain_term = config.get_decimal("quality.duration_strain_coefficient") * strain_input
    complexity_term = (
        config.get_decimal("quality.duration_complexity_coefficient") * complexity_input
    )
    multiplier = Decimal(1) - skill_term + strain_term + complexity_term
    floor = config.get_decimal("appointments.min_duration_multiplier")
    return max(multiplier, floor)


@dataclass(frozen=True)
class RoutablePoint:

    catalog_point: geography.CatalogPoint
    effective_zone_id: int
    latitude: float
    longitude: float


def technician_home_point(conn: sqlite3.Connection, technician_id: int) -> RoutablePoint:
    row = conn.execute(
        "SELECT home_address_catalog_id, home_latitude, home_longitude "
        "FROM _technicians WHERE id = ?", (technician_id,)
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"technician {technician_id} not found"
        )
    catalog_id = row["home_address_catalog_id"]
    if catalog_id is None:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"technician {technician_id} has no home address catalog row",
        )
    point = geography.catalog_point(conn, int(catalog_id))
    zone_row = conn.execute(
        "SELECT effective_zone_id FROM _address_catalog WHERE id = ?", (catalog_id,)
    ).fetchone()
    return RoutablePoint(
        catalog_point=point,
        effective_zone_id=int(zone_row["effective_zone_id"]),
        latitude=float(row["home_latitude"]),
        longitude=float(row["home_longitude"]),
    )


def location_point(conn: sqlite3.Connection, location_id: int) -> RoutablePoint:
    row = conn.execute(
        "SELECT address_catalog_id, effective_zone_id, latitude, longitude "
        "FROM _locations WHERE id = ?",
        (location_id,),
    ).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"location {location_id} not found")
    catalog_id = row["address_catalog_id"]
    if catalog_id is None:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL, f"location {location_id} has no address catalog row"
        )
    point = geography.catalog_point(conn, int(catalog_id))
    return RoutablePoint(
        catalog_point=point,
        effective_zone_id=int(row["effective_zone_id"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
    )


def _destination_condition_code(
    conn: sqlite3.Connection, zone_id: int, service_date: str
) -> ConditionCode:
    row = conn.execute(
        "SELECT condition_code FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (zone_id, service_date),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"no weather realized for zone {zone_id} on {service_date}",
        )
    return ConditionCode(row["condition_code"])


def planned_travel_minutes(
    conn: sqlite3.Connection,
    artifact: GeoArtifact,
    config: RunConfig,
    *,
    origin: RoutablePoint,
    destination: RoutablePoint,
    service_date: str,
    minute_of_day: int,
) -> int:
    distance_km = geography.route_distance_km(
        origin.catalog_point.access_distance_km,
        origin.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km,
        destination.catalog_point.anchor_id,
        artifact,
        config,
    )
    day_type = day_type_for(service_date).value
    band = travel.band_for_minute(config, minute_of_day)
    speed = config.resolved["travel.speed_by_time_band"][day_type][band]
    condition_code = _destination_condition_code(
        conn, destination.effective_zone_id, service_date
    )
    weather_multiplier = config.resolved["travel.weather_travel_multipliers"][
        condition_code.value
    ]
    raw_minutes = Decimal(60) * Decimal(str(distance_km)) / speed * weather_multiplier
    minutes = int(raw_minutes.to_integral_value(rounding=ROUND_HALF_UP))
    return max(minutes, config.get_int("travel.min_travel_minutes"))


def start_variation_minutes(rng: Rng, appointment_id: int, service_date: str) -> int:
    return rng.inverse_cdf_variation(
        KeyedStreamName.JOB_EXECUTION.value,
        EntityType.APPOINTMENT.value,
        appointment_id,
        service_date,
        "start_variation",
        0,
    )


def duration_variation_minutes(rng: Rng, appointment_id: int, service_date: str) -> int:
    return rng.inverse_cdf_variation(
        KeyedStreamName.JOB_EXECUTION.value,
        EntityType.APPOINTMENT.value,
        appointment_id,
        service_date,
        "duration_variation",
        0,
    )


def _next_service_date(service_date: str) -> str:
    return (date.fromisoformat(service_date) + timedelta(days=1)).isoformat()


def _day_bounds_ms(service_date: str) -> tuple[int, int]:
    return appointments._instant(service_date, 0), appointments._instant(
        _next_service_date(service_date), 0
    )


def _minute_of_day(ms: int) -> int:
    local = clock.phoenix_local(ms)
    return local.hour * 60 + local.minute


def _payload(row: sqlite3.Row) -> dict:
    return json.loads(row["payload"]) if row["payload"] else {}


def _advance_to_event(ctx, row: sqlite3.Row) -> int:
    target = max(clock.now(ctx.conn), int(row["scheduled_at_ms"]))
    clock.advance(ctx.conn, target)
    return target


def _active_technician_id(conn: sqlite3.Connection, appointment_id: int) -> int | None:
    row = conn.execute(
        "SELECT technician_id FROM _appointment_assignments "
        "WHERE appointment_id = ? AND status = 'active'",
        (appointment_id,),
    ).fetchone()
    return int(row["technician_id"]) if row is not None else None


def _appointment_blocks(conn: sqlite3.Connection, appointment_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT ordinal, block_code, block_start, block_end FROM _appointment_blocks "
            "WHERE appointment_id = ? ORDER BY ordinal",
            (appointment_id,),
        ).fetchall()
    )


def _first_block_start(conn: sqlite3.Connection, appointment_id: int) -> int:
    row = conn.execute(
        "SELECT block_start FROM _appointment_blocks WHERE appointment_id = ? AND ordinal = 1",
        (appointment_id,),
    ).fetchone()
    return int(row["block_start"])


def _appointment_service_date(conn: sqlite3.Connection, appointment_id: int) -> str:
    row = conn.execute(
        "SELECT scheduled_start FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found")
    return clock.service_date_for_ms(int(row["scheduled_start"]))


def _appointment_job_location_id(conn: sqlite3.Connection, appointment_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT j.id AS job_id, j.location_id AS location_id "
        "FROM _appointments a JOIN _jobs j ON j.id = a.job_id WHERE a.id = ?",
        (appointment_id,),
    ).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found")
    return int(row["job_id"]), int(row["location_id"])


def _write_technician_location(
    conn: sqlite3.Connection,
    technician_id: int,
    *,
    state: str,
    latitude: float,
    longitude: float,
    updated_at: int,
    trip_origin: tuple[float, float] | None = None,
    trip_destination: tuple[float, float] | None = None,
    trip_departed_at: int | None = None,
    trip_travel_minutes: int | None = None,
) -> None:
    conn.execute(
        "UPDATE _technician_locations SET latitude = ?, longitude = ?, "
        "coordinates_updated_at = ?, location_state = ?, "
        "trip_origin_latitude = ?, trip_origin_longitude = ?, "
        "trip_destination_latitude = ?, trip_destination_longitude = ?, "
        "trip_departed_at = ?, trip_travel_minutes = ? WHERE technician_id = ?",
        (
            latitude,
            longitude,
            updated_at,
            state,
            trip_origin[0] if trip_origin is not None else None,
            trip_origin[1] if trip_origin is not None else None,
            trip_destination[0] if trip_destination is not None else None,
            trip_destination[1] if trip_destination is not None else None,
            trip_departed_at,
            trip_travel_minutes,
            technician_id,
        ),
    )


def _write_appointment_event(
    conn: sqlite3.Connection,
    *,
    appointment_id: int,
    technician_id: int | None,
    event_type: str,
    occurred_at: int,
    service_date: str,
) -> None:
    conn.execute(
        "INSERT INTO _appointment_events (appointment_id, technician_id, event_type, "
        "occurred_at, service_date, detail, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?)",
        (appointment_id, technician_id, event_type, occurred_at, service_date, occurred_at),
    )


_LEGAL_JOB_TERMINAL_PAIRS = frozenset({
    ("completed", "completed_paid"),
    ("completed", "completed_warranty"),
    ("failed", "failed_unsupported"),
    ("failed", "failed_unqualified"),
    ("no_show", "no_show_company"),
    ("cancelled", "cancelled_by_customer"),
})

_COMPLETED_JOB_OUTCOMES = frozenset({"completed_paid", "completed_warranty"})

_WORK_FAILED_OUTCOMES = {
    "unsupported": "failed_unsupported",
    "unqualified": "failed_unqualified",
}


def classify_job(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    status: str,
    outcome: str,
    now_ms: int,
    completed_at_ms: int | None = None,
) -> None:
    if (status, outcome) not in _LEGAL_JOB_TERMINAL_PAIRS:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"job {job_id} illegal terminal pair ({status!r}, {outcome!r})",
        )
    row = conn.execute("SELECT outcome FROM _jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"job {job_id} not found")
    if row["outcome"] is not None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"job {job_id} already classified with outcome {row['outcome']!r}",
        )
    completed_at = None
    if outcome in _COMPLETED_JOB_OUTCOMES:
        if completed_at_ms is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"job {job_id} classification {outcome!r} requires completed_at_ms",
            )
        completed_at = completed_at_ms
    conn.execute(
        "UPDATE _jobs SET status = ?, outcome = ?, completed_at = ?, modified_at = ? "
        "WHERE id = ?",
        (status, outcome, completed_at, now_ms, job_id),
    )


def _arrival_failure_reason(
    conn: sqlite3.Connection, job_id: int, technician_id: int
) -> str | None:
    row = conn.execute(
        "SELECT sc.supported_job_type_id AS supported_job_type_id "
        "FROM _jobs j JOIN _call_hidden ch ON ch.call_id = j.call_id "
        "JOIN _service_catalog sc ON sc.id = ch.requested_category_id "
        "WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"job {job_id} has no resolvable requested category",
        )
    supported_job_type_id = row["supported_job_type_id"]
    if supported_job_type_id is None:
        return "unsupported"
    skill_row = conn.execute(
        "SELECT required_skill_code FROM _job_types WHERE id = ?",
        (supported_job_type_id,),
    ).fetchone()
    required_skill_code = skill_row["required_skill_code"] if skill_row else None
    if required_skill_code is None:
        return None
    holder = conn.execute(
        "SELECT 1 FROM _technician_skills "
        "WHERE technician_id = ? AND skill_code = ? AND active = 1",
        (technician_id, required_skill_code),
    ).fetchone()
    return None if holder is not None else "unqualified"


def first_trip_dispatch_applied(conn: sqlite3.Connection, service_date: str) -> bool:
    day_start, day_end = _day_bounds_ms(service_date)
    row = conn.execute(
        "SELECT 1 FROM _events WHERE kind = ? AND status = 'applied' "
        "AND scheduled_at_ms >= ? AND scheduled_at_ms < ? LIMIT 1",
        (EventKind.FIRST_TRIP_DISPATCH.value, day_start, day_end),
    ).fetchone()
    return row is not None


def _pending_dispatch_event(conn: sqlite3.Connection, appointment_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT event_id, payload FROM _events WHERE kind = ? AND status = 'pending' "
        "AND entity_type = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_DISPATCH.value, EntityType.APPOINTMENT.value, appointment_id),
    ).fetchone()


def _pending_arrival_event(conn: sqlite3.Connection, appointment_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT event_id FROM _events WHERE kind = ? AND status = 'pending' "
        "AND entity_type = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_ARRIVAL.value, EntityType.APPOINTMENT.value, appointment_id),
    ).fetchone()


def _pending_work_started_event(conn: sqlite3.Connection, appointment_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT event_id FROM _events WHERE kind = ? AND status = 'pending' "
        "AND entity_type = ? AND entity_id = ?",
        (EventKind.WORK_STARTED.value, EntityType.APPOINTMENT.value, appointment_id),
    ).fetchone()


def _pending_work_failed_event(conn: sqlite3.Connection, appointment_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT event_id FROM _events WHERE kind = ? AND status = 'pending' "
        "AND entity_type = ? AND entity_id = ?",
        (EventKind.WORK_FAILED.value, EntityType.APPOINTMENT.value, appointment_id),
    ).fetchone()


def pending_dispatch_for_technician(conn: sqlite3.Connection, technician_id: int) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT event_id, entity_id, payload FROM _events "
        "WHERE kind = ? AND status = 'pending'",
        (EventKind.APPOINTMENT_DISPATCH.value,),
    ).fetchall()
    for candidate in rows:
        payload = _payload(candidate)
        if int(payload.get("technician_id", -1)) == technician_id:
            return candidate
    return None


def dispatch_trip(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    appointment_id: int,
    technician_id: int,
    origin: RoutablePoint,
    destination: RoutablePoint,
    departure_ms: int,
    service_date: str,
) -> int:
    conn = ctx.conn
    distance_km = geography.route_distance_km(
        origin.catalog_point.access_distance_km,
        origin.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km,
        destination.catalog_point.anchor_id,
        artifact,
        config,
    )
    day_type = day_type_for(service_date).value
    minute_of_day = _minute_of_day(departure_ms)
    condition_code = _destination_condition_code(conn, destination.effective_zone_id, service_date)
    trip_index = appointments.trip_index_for_appointment(conn, appointment_id)
    noise_key = travel.inbound_trip_noise_key(appointment_id, service_date, trip_index)
    actual_minutes = travel.actual_travel_minutes(
        ctx.rng,
        config,
        distance_km=distance_km,
        day_type=day_type,
        minute_of_day=minute_of_day,
        condition_code=condition_code,
        noise_key=noise_key,
    )
    _write_technician_location(
        conn,
        technician_id,
        state="traveling",
        latitude=origin.latitude,
        longitude=origin.longitude,
        updated_at=departure_ms,
        trip_origin=(origin.latitude, origin.longitude),
        trip_destination=(destination.latitude, destination.longitude),
        trip_departed_at=departure_ms,
        trip_travel_minutes=actual_minutes,
    )
    _write_appointment_event(
        conn,
        appointment_id=appointment_id,
        technician_id=technician_id,
        event_type="dispatched",
        occurred_at=departure_ms,
        service_date=service_date,
    )
    arrival_ms = departure_ms + actual_minutes * 60000
    Events(conn, ctx.rng).schedule(
        EventKind.APPOINTMENT_ARRIVAL,
        arrival_ms,
        entity_type=EntityType.APPOINTMENT.value,
        entity_id=appointment_id,
        payload={"appointment_id": appointment_id, "technician_id": technician_id},
    )
    return arrival_ms


def plan_dispatch_from_home(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    appointment_id: int,
    technician_id: int,
    service_date: str,
    floor_ms: int,
) -> int:
    conn = ctx.conn
    origin = technician_home_point(conn, technician_id)
    _job_id, location_id = _appointment_job_location_id(conn, appointment_id)
    destination = location_point(conn, location_id)
    first_block_start = _first_block_start(conn, appointment_id)
    minute_of_day = _minute_of_day(first_block_start)
    planned = planned_travel_minutes(
        conn,
        artifact,
        config,
        origin=origin,
        destination=destination,
        service_date=service_date,
        minute_of_day=minute_of_day,
    )
    departure = max(first_block_start - planned * 60000, floor_ms)
    Events(conn, ctx.rng).schedule(
        EventKind.APPOINTMENT_DISPATCH,
        departure,
        entity_type=EntityType.APPOINTMENT.value,
        entity_id=appointment_id,
        payload={"appointment_id": appointment_id, "technician_id": technician_id},
    )
    return departure


def _earliest_active_appointment_for_technician(
    conn: sqlite3.Connection, technician_id: int, service_date: str
) -> int | None:
    day_start, day_end = _day_bounds_ms(service_date)
    row = conn.execute(
        "SELECT a.id AS appointment_id FROM _appointment_assignments aa "
        "JOIN _appointments a ON a.id = aa.appointment_id "
        "JOIN _appointment_blocks ab ON ab.appointment_id = a.id AND ab.ordinal = 1 "
        "WHERE aa.technician_id = ? AND aa.status = 'active' "
        f"AND a.status NOT IN ({_TERMINAL_PLACEHOLDERS}) "
        "AND ab.block_start >= ? AND ab.block_start < ? "
        "ORDER BY ab.block_start ASC LIMIT 1",
        (technician_id, *_TERMINAL, day_start, day_end),
    ).fetchone()
    return int(row["appointment_id"]) if row is not None else None


def plan_next_dispatch_if_idle(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    technician_id: int,
    service_date: str,
    floor_ms: int,
) -> None:
    conn = ctx.conn
    next_appointment_id = _earliest_active_appointment_for_technician(
        conn, technician_id, service_date
    )
    if next_appointment_id is None:
        return
    if _pending_dispatch_event(conn, next_appointment_id) is not None:
        return
    plan_dispatch_from_home(
        ctx, config, artifact,
        appointment_id=next_appointment_id, technician_id=technician_id,
        service_date=service_date, floor_ms=floor_ms,
    )


def _schedule_return_home(
    ctx,
    *,
    from_appointment_id: int,
    technician_id: int,
    service_date: str,
    from_block_index: int,
    departure_ms: int,
) -> None:
    Events(ctx.conn, ctx.rng).schedule(
        EventKind.TECHNICIAN_RETURN_HOME,
        departure_ms,
        entity_type=EntityType.TECHNICIAN.value,
        entity_id=technician_id,
        payload={
            "appointment_id": from_appointment_id,
            "technician_id": technician_id,
            "service_date": service_date,
            "block_index": from_block_index,
        },
    )


def plan_next_chain_step(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    technician_id: int,
    service_date: str,
    from_appointment_id: int,
    from_location: RoutablePoint,
    from_block_index: int,
    departure_ms: int,
) -> None:
    conn = ctx.conn
    day_start, day_end = _day_bounds_ms(service_date)
    next_appt = conn.execute(
        "SELECT a.id AS appointment_id, ab.block_code AS block_code "
        "FROM _appointment_assignments aa "
        "JOIN _appointments a ON a.id = aa.appointment_id "
        "JOIN _appointment_blocks ab ON ab.appointment_id = a.id AND ab.ordinal = 1 "
        "WHERE aa.technician_id = ? AND aa.status = 'active' AND a.id != ? "
        f"AND a.status NOT IN ({_TERMINAL_PLACEHOLDERS}) "
        "AND ab.block_start >= ? AND ab.block_start < ? "
        "ORDER BY ab.block_start ASC LIMIT 1",
        (technician_id, from_appointment_id, *_TERMINAL, day_start, day_end),
    ).fetchone()
    if next_appt is not None and block_index(next_appt["block_code"]) == from_block_index + 1:
        next_appointment_id = int(next_appt["appointment_id"])
        _job_id, location_id = _appointment_job_location_id(conn, next_appointment_id)
        destination = location_point(conn, location_id)
        dispatch_trip(
            ctx,
            config,
            artifact,
            appointment_id=next_appointment_id,
            technician_id=technician_id,
            origin=from_location,
            destination=destination,
            departure_ms=departure_ms,
            service_date=service_date,
        )
        return
    _schedule_return_home(
        ctx,
        from_appointment_id=from_appointment_id,
        technician_id=technician_id,
        service_date=service_date,
        from_block_index=from_block_index,
        departure_ms=departure_ms,
    )


def replan_same_day(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    technician_id: int,
    service_date: str,
    appointment_id: int,
) -> None:
    conn = ctx.conn
    if not first_trip_dispatch_applied(conn, service_date):
        return
    state_row = conn.execute(
        "SELECT location_state FROM _technician_locations WHERE technician_id = ?",
        (technician_id,),
    ).fetchone()
    if state_row is None or state_row["location_state"] != "home":
        return
    pending = pending_dispatch_for_technician(conn, technician_id)
    if pending is None:
        plan_dispatch_from_home(
            ctx,
            config,
            artifact,
            appointment_id=appointment_id,
            technician_id=technician_id,
            service_date=service_date,
            floor_ms=ctx.now(),
        )
        return
    payload = _payload(pending)
    existing_appointment_id = int(payload["appointment_id"])
    if existing_appointment_id == appointment_id:
        return
    existing_start = _first_block_start(conn, existing_appointment_id)
    new_start = _first_block_start(conn, appointment_id)
    if new_start < existing_start:
        Events(conn, ctx.rng).cancel(int(pending["event_id"]))
        plan_dispatch_from_home(
            ctx,
            config,
            artifact,
            appointment_id=appointment_id,
            technician_id=technician_id,
            service_date=service_date,
            floor_ms=ctx.now(),
        )


def handle_outgoing_technician(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    appointment_id: int,
    technician_id: int,
    service_date: str,
) -> None:
    conn = ctx.conn
    arrival_pending = _pending_arrival_event(conn, appointment_id)
    if arrival_pending is not None:
        _abandon_en_route(
            ctx,
            config,
            artifact,
            appointment_id=appointment_id,
            technician_id=technician_id,
            service_date=service_date,
            arrival_event_id=int(arrival_pending["event_id"]),
        )
        return
    work_started_pending = _pending_work_started_event(conn, appointment_id)
    if work_started_pending is not None:
        _abandon_arrived_not_started(
            ctx,
            config,
            artifact,
            appointment_id=appointment_id,
            technician_id=technician_id,
            service_date=service_date,
            pending_event_id=int(work_started_pending["event_id"]),
        )
        return
    work_failed_pending = _pending_work_failed_event(conn, appointment_id)
    if work_failed_pending is not None:
        _abandon_arrived_not_started(
            ctx,
            config,
            artifact,
            appointment_id=appointment_id,
            technician_id=technician_id,
            service_date=service_date,
            pending_event_id=int(work_failed_pending["event_id"]),
        )
        return
    dispatch_pending = _pending_dispatch_event(conn, appointment_id)
    if dispatch_pending is not None:
        Events(conn, ctx.rng).cancel(int(dispatch_pending["event_id"]))
        plan_next_dispatch_if_idle(
            ctx, config, artifact,
            technician_id=technician_id, service_date=service_date, floor_ms=ctx.now(),
        )


def _abandon_en_route(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    appointment_id: int,
    technician_id: int,
    service_date: str,
    arrival_event_id: int,
) -> None:
    conn = ctx.conn
    now = ctx.now()
    Events(conn, ctx.rng).cancel(arrival_event_id)
    loc = conn.execute(
        "SELECT trip_origin_latitude, trip_origin_longitude, "
        "trip_destination_latitude, trip_destination_longitude, "
        "trip_departed_at, trip_travel_minutes FROM _technician_locations "
        "WHERE technician_id = ?",
        (technician_id,),
    ).fetchone()
    origin_lat = float(loc["trip_origin_latitude"])
    origin_lon = float(loc["trip_origin_longitude"])
    dest_lat = float(loc["trip_destination_latitude"])
    dest_lon = float(loc["trip_destination_longitude"])
    departed_at = int(loc["trip_departed_at"])
    travel_minutes = int(loc["trip_travel_minutes"])
    span_ms = travel_minutes * 60000
    fraction = 0.0 if span_ms <= 0 else min(1.0, max(0.0, (now - departed_at) / span_ms))
    turn_lat = origin_lat + (dest_lat - origin_lat) * fraction
    turn_lon = origin_lon + (dest_lon - origin_lon) * fraction
    elapsed_ms = now - departed_at
    ready_at = now + elapsed_ms
    new_travel_minutes = max(elapsed_ms // 60000, 1)

    home = technician_home_point(conn, technician_id)
    is_from_home = origin_lat == home.latitude and origin_lon == home.longitude

    _write_technician_location(
        conn,
        technician_id,
        state="traveling",
        latitude=turn_lat,
        longitude=turn_lon,
        updated_at=now,
        trip_origin=(turn_lat, turn_lon),
        trip_destination=(origin_lat, origin_lon),
        trip_departed_at=now,
        trip_travel_minutes=new_travel_minutes,
    )

    if is_from_home:
        conn.execute(
            "INSERT INTO _technician_movements (technician_id, service_date, movement_kind, "
            "origin_latitude, origin_longitude, destination_latitude, destination_longitude, "
            "departed_at, arrives_at, created_at) VALUES (?, ?, 'return_home', ?, ?, ?, ?, ?, ?, ?)",
            (
                technician_id, service_date, turn_lat, turn_lon,
                home.latitude, home.longitude, now, ready_at, now,
            ),
        )
        Events(conn, ctx.rng).schedule(
            EventKind.TECHNICIAN_HOME_ARRIVAL,
            ready_at,
            entity_type=EntityType.TECHNICIAN.value,
            entity_id=technician_id,
            payload={"service_date": service_date, "technician_id": technician_id},
        )
        return

    day_start, day_end = _day_bounds_ms(service_date)
    origin_row = conn.execute(
        "SELECT a.id AS appointment_id, j.location_id AS location_id, "
        "final_block.block_code AS block_code "
        "FROM _appointment_assignments aa "
        "JOIN _appointments a ON a.id = aa.appointment_id "
        "JOIN _jobs j ON j.id = a.job_id "
        "JOIN _locations loc ON loc.id = j.location_id "
        "JOIN _appointment_blocks first_block ON first_block.appointment_id = a.id "
        "AND first_block.ordinal = 1 "
        "JOIN _appointment_blocks final_block ON final_block.appointment_id = a.id "
        "AND final_block.ordinal = (SELECT MAX(ordinal) FROM _appointment_blocks "
        "WHERE appointment_id = a.id) "
        "WHERE aa.technician_id = ? AND loc.latitude = ? AND loc.longitude = ? "
        "AND a.id != ? "
        "AND first_block.block_start >= ? AND first_block.block_start < ? "
        "ORDER BY first_block.block_start DESC LIMIT 1",
        (technician_id, origin_lat, origin_lon, appointment_id, day_start, day_end),
    ).fetchone()
    if origin_row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"technician {technician_id} turn-back has no resolvable prior appointment "
            f"location on {service_date}",
        )
    from_location = location_point(conn, int(origin_row["location_id"]))
    plan_next_chain_step(
        ctx,
        config,
        artifact,
        technician_id=technician_id,
        service_date=service_date,
        from_appointment_id=int(origin_row["appointment_id"]),
        from_location=from_location,
        from_block_index=block_index(origin_row["block_code"]),
        departure_ms=ready_at,
    )


def _abandon_arrived_not_started(
    ctx,
    config: RunConfig,
    artifact: GeoArtifact,
    *,
    appointment_id: int,
    technician_id: int,
    service_date: str,
    pending_event_id: int,
) -> None:
    conn = ctx.conn
    now = ctx.now()
    Events(conn, ctx.rng).cancel(pending_event_id)
    _job_id, location_id = _appointment_job_location_id(conn, appointment_id)
    from_location = location_point(conn, location_id)
    blocks = _appointment_blocks(conn, appointment_id)
    from_block_index = block_index(blocks[-1]["block_code"])
    plan_next_chain_step(
        ctx,
        config,
        artifact,
        technician_id=technician_id,
        service_date=service_date,
        from_appointment_id=appointment_id,
        from_location=from_location,
        from_block_index=from_block_index,
        departure_ms=now,
    )


def off_hours_accrual_mark_handler(ctx, row: sqlite3.Row) -> dict | None:
    _advance_to_event(ctx, row)
    conn = ctx.conn
    payload = _payload(row)
    appointment_id = int(payload["appointment_id"])
    appt = conn.execute(
        "SELECT status, job_id FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if appt is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found")
    if appt["status"] in _TERMINAL:
        return None
    active = conn.execute(
        "SELECT technician_id FROM _appointment_assignments "
        "WHERE appointment_id = ? AND status = 'active'",
        (appointment_id,),
    ).fetchone()
    if active is None:
        return None
    technician_id = int(active["technician_id"])
    wage_row = conn.execute(
        "SELECT hourly_wage_cents FROM _technicians WHERE id = ?", (technician_id,)
    ).fetchone()
    new_payload = {
        **payload,
        "technician_id": technician_id,
        "hourly_wage_cents": int(wage_row["hourly_wage_cents"]),
        "job_id": int(appt["job_id"]),
    }
    Events(conn, ctx.rng).update_payload(int(row["event_id"]), new_payload)
    return None


def first_trip_dispatch_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        _advance_to_event(ctx, row)
        conn = ctx.conn
        service_date = clock.service_date_for_ms(int(row["scheduled_at_ms"]))
        day_start, day_end = _day_bounds_ms(service_date)
        floor_ms = appointments._instant(
            service_date, constants.OPENING_HOUR * 60 + constants.OPENING_MINUTE
        )

        first_block_appts = conn.execute(
            "SELECT a.id AS appointment_id FROM _appointments a "
            "JOIN _appointment_blocks ab ON ab.appointment_id = a.id AND ab.ordinal = 1 "
            f"WHERE a.status NOT IN ({_TERMINAL_PLACEHOLDERS}) "
            "AND ab.block_start >= ? AND ab.block_start < ?",
            (*_TERMINAL, day_start, day_end),
        ).fetchall()

        conn.execute(
            "UPDATE _day_state SET unresolved_execution_count = unresolved_execution_count + ? "
            "WHERE service_date = ?",
            (len(first_block_appts), service_date),
        )

        for appt_row in first_block_appts:
            if _active_technician_id(conn, int(appt_row["appointment_id"])) is None:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"appointment {appt_row['appointment_id']} has no active assignment "
                    f"at first_trip_dispatch for {service_date}",
                )

        technician_rows = conn.execute(
            "SELECT DISTINCT aa.technician_id AS technician_id FROM _appointment_assignments aa "
            "JOIN _appointments a ON a.id = aa.appointment_id "
            "JOIN _appointment_blocks ab ON ab.appointment_id = a.id AND ab.ordinal = 1 "
            "WHERE aa.status = 'active' "
            f"AND a.status NOT IN ({_TERMINAL_PLACEHOLDERS}) "
            "AND ab.block_start >= ? AND ab.block_start < ?",
            (*_TERMINAL, day_start, day_end),
        ).fetchall()

        for tech_row in technician_rows:
            technician_id = int(tech_row["technician_id"])
            earliest = conn.execute(
                "SELECT a.id AS appointment_id, ab.block_start AS block_start "
                "FROM _appointment_assignments aa "
                "JOIN _appointments a ON a.id = aa.appointment_id "
                "JOIN _appointment_blocks ab ON ab.appointment_id = a.id AND ab.ordinal = 1 "
                "WHERE aa.technician_id = ? AND aa.status = 'active' "
                f"AND a.status NOT IN ({_TERMINAL_PLACEHOLDERS}) "
                "AND ab.block_start >= ? AND ab.block_start < ? "
                "ORDER BY ab.block_start ASC LIMIT 1",
                (technician_id, *_TERMINAL, day_start, day_end),
            ).fetchone()
            appointment_id = int(earliest["appointment_id"])
            first_block_start = int(earliest["block_start"])
            origin = technician_home_point(conn, technician_id)
            _job_id, location_id = _appointment_job_location_id(conn, appointment_id)
            destination = location_point(conn, location_id)
            minute_of_day = _minute_of_day(first_block_start)
            planned = planned_travel_minutes(
                conn, artifact, config,
                origin=origin, destination=destination,
                service_date=service_date, minute_of_day=minute_of_day,
            )
            departure = max(first_block_start - planned * 60000, floor_ms)
            if departure == floor_ms:
                dispatch_trip(
                    ctx, config, artifact,
                    appointment_id=appointment_id, technician_id=technician_id,
                    origin=origin, destination=destination,
                    departure_ms=floor_ms, service_date=service_date,
                )
            else:
                Events(conn, ctx.rng).schedule(
                    EventKind.APPOINTMENT_DISPATCH, departure,
                    entity_type=EntityType.APPOINTMENT.value, entity_id=appointment_id,
                    payload={"appointment_id": appointment_id, "technician_id": technician_id},
                )
        return None

    return handler


def appointment_dispatch_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        departure_ms = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        appointment_id = int(payload["appointment_id"])
        technician_id = int(payload["technician_id"])
        status = conn.execute(
            "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
        ).fetchone()["status"]
        if status in _TERMINAL:
            return None
        service_date = clock.service_date_for_ms(departure_ms)
        origin = technician_home_point(conn, technician_id)
        _job_id, location_id = _appointment_job_location_id(conn, appointment_id)
        destination = location_point(conn, location_id)
        dispatch_trip(
            ctx, config, artifact,
            appointment_id=appointment_id, technician_id=technician_id,
            origin=origin, destination=destination,
            departure_ms=departure_ms, service_date=service_date,
        )
        return None

    return handler


def appointment_arrival_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        appointment_id = int(payload["appointment_id"])
        technician_id = int(payload["technician_id"])
        service_date = _appointment_service_date(conn, appointment_id)
        loc = conn.execute(
            "SELECT trip_destination_latitude, trip_destination_longitude "
            "FROM _technician_locations WHERE technician_id = ?",
            (technician_id,),
        ).fetchone()
        _write_technician_location(
            conn, technician_id, state="on_site",
            latitude=float(loc["trip_destination_latitude"]),
            longitude=float(loc["trip_destination_longitude"]),
            updated_at=now,
        )
        appt = conn.execute(
            "SELECT status, scheduled_start, arrival_window_end FROM _appointments WHERE id = ?",
            (appointment_id,),
        ).fetchone()
        if appt["status"] in _TERMINAL:
            blocks = _appointment_blocks(conn, appointment_id)
            final_block_index = block_index(blocks[-1]["block_code"])
            _job_id, location_id = _appointment_job_location_id(conn, appointment_id)
            origin = location_point(conn, location_id)
            plan_next_chain_step(
                ctx, config, artifact,
                technician_id=technician_id, service_date=service_date,
                from_appointment_id=appointment_id, from_location=origin,
                from_block_index=final_block_index, departure_ms=now,
            )
            return None
        _write_appointment_event(
            conn, appointment_id=appointment_id, technician_id=technician_id,
            event_type="arrived", occurred_at=now, service_date=service_date,
        )
        job_id, _location_id = _appointment_job_location_id(conn, appointment_id)
        failure_reason = _arrival_failure_reason(conn, job_id, technician_id)
        if failure_reason is not None:
            failed_at = now + config.get_int("execution.failed_visit_minutes") * 60000
            Events(conn, ctx.rng).schedule(
                EventKind.WORK_FAILED, failed_at,
                entity_type=EntityType.APPOINTMENT.value, entity_id=appointment_id,
                payload={
                    "appointment_id": appointment_id,
                    "technician_id": technician_id,
                    "reason": failure_reason,
                },
            )
            return None
        start_variation = start_variation_minutes(ctx.rng, appointment_id, service_date)
        work_started_ms = max(now, int(appt["scheduled_start"]) + start_variation * 60000)
        if work_started_ms > int(appt["arrival_window_end"]):
            return None
        Events(conn, ctx.rng).schedule(
            EventKind.WORK_STARTED, work_started_ms,
            entity_type=EntityType.APPOINTMENT.value, entity_id=appointment_id,
            payload={"appointment_id": appointment_id, "technician_id": technician_id},
        )
        return None

    return handler


def work_started_handler(config: RunConfig) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        appointment_id = int(payload["appointment_id"])
        technician_id = int(payload["technician_id"])
        service_date = _appointment_service_date(conn, appointment_id)
        scheduled_start = int(
            conn.execute(
                "SELECT scheduled_start FROM _appointments WHERE id = ?", (appointment_id,)
            ).fetchone()["scheduled_start"]
        )
        lateness_minutes = max(0, now - scheduled_start - _LATENESS_GRACE_MS) // 60000
        updated = conn.execute(
            "UPDATE _appointments SET status = 'in_progress', actual_start = ?, "
            "lateness_minutes = ?, modified_at = ? WHERE id = ? AND status = 'scheduled'",
            (now, lateness_minutes, now, appointment_id),
        )
        if updated.rowcount != 1:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"appointment {appointment_id} was not scheduled at work_started",
            )
        _write_appointment_event(
            conn, appointment_id=appointment_id, technician_id=technician_id,
            event_type="work_started", occurred_at=now, service_date=service_date,
        )
        reserved_blocks = int(
            conn.execute(
                "SELECT COUNT(*) c FROM _appointment_blocks WHERE appointment_id = ?",
                (appointment_id,),
            ).fetchone()["c"]
        )
        multiplier = execution_duration_multiplier(config)
        base = Decimal(120 * reserved_blocks) * multiplier
        base_minutes = int(base.to_integral_value(rounding=ROUND_HALF_UP))
        duration_variation = duration_variation_minutes(ctx.rng, appointment_id, service_date)
        duration_minutes = max(
            base_minutes + duration_variation,
            config.get_int("appointments.min_actual_duration_minutes"),
        )
        completion_ms = now + duration_minutes * 60000
        Events(conn, ctx.rng).schedule(
            EventKind.WORK_COMPLETED, completion_ms,
            entity_type=EntityType.APPOINTMENT.value, entity_id=appointment_id,
            payload={"appointment_id": appointment_id, "technician_id": technician_id},
        )
        return None

    return handler


def company_no_show_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        cutoff = int(row["scheduled_at_ms"])
        conn = ctx.conn
        payload = _payload(row)
        appointment_id = int(payload["appointment_id"])
        appt = conn.execute(
            "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        if appt is None:
            raise EngineError(
                ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found"
            )
        if appt["status"] != "scheduled":
            return None
        if _pending_work_failed_event(conn, appointment_id) is not None:
            return None
        service_date = _appointment_service_date(conn, appointment_id)
        updated = conn.execute(
            "UPDATE _appointments SET status = 'no_show', modified_at = ? "
            "WHERE id = ? AND status = 'scheduled'",
            (cutoff, appointment_id),
        )
        if updated.rowcount != 1:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"appointment {appointment_id} was not scheduled at company no-show",
            )
        technician_id = _active_technician_id(conn, appointment_id)
        _write_appointment_event(
            conn, appointment_id=appointment_id, technician_id=technician_id,
            event_type="no_show", occurred_at=cutoff, service_date=service_date,
        )
        job_id, _location_id = _appointment_job_location_id(conn, appointment_id)
        classify_job(
            conn, job_id=job_id, status="no_show", outcome="no_show_company", now_ms=cutoff,
        )
        conn.execute(
            "DELETE FROM _technician_block_holds WHERE appointment_id = ?",
            (appointment_id,),
        )
        appointment_number = str(
            conn.execute(
                "SELECT appointment_number FROM _appointments WHERE id = ?",
                (appointment_id,),
            ).fetchone()["appointment_number"]
        )
        conn.execute(
            "INSERT INTO _notifications (created_at, service_date, notification_type, "
            "entity_type, entity_id, message) "
            "VALUES (?, ?, 'company_no_show', 'appointment', ?, ?)",
            (
                cutoff, service_date, appointment_id,
                f"No technician started work for appointment {appointment_number} "
                f"on {service_date} before its arrival window ended, so the company "
                "missed the appointment.",
            ),
        )
        dispatch = _pending_dispatch_event(conn, appointment_id)
        if dispatch is not None:
            Events(conn, ctx.rng).cancel(int(dispatch["event_id"]))
            if technician_id is not None:
                plan_next_dispatch_if_idle(
                    ctx, config, artifact,
                    technician_id=technician_id, service_date=service_date,
                    floor_ms=now,
                )
        elif technician_id is not None and _pending_arrival_event(conn, appointment_id) is None:
            arrived = conn.execute(
                "SELECT 1 FROM _appointment_events WHERE appointment_id = ? "
                "AND technician_id = ? AND event_type = 'arrived' LIMIT 1",
                (appointment_id, technician_id),
            ).fetchone()
            if arrived is not None:
                _job_id, location_id = _appointment_job_location_id(conn, appointment_id)
                origin = location_point(conn, location_id)
                blocks = _appointment_blocks(conn, appointment_id)
                final_block_index = block_index(blocks[-1]["block_code"])
                plan_next_chain_step(
                    ctx, config, artifact,
                    technician_id=technician_id, service_date=service_date,
                    from_appointment_id=appointment_id, from_location=origin,
                    from_block_index=final_block_index, departure_ms=now,
                )
        conn.execute(
            "UPDATE _day_state SET unresolved_execution_count = unresolved_execution_count - 1 "
            "WHERE service_date = ?",
            (service_date,),
        )
        return None

    return handler


def true_job_type(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT jt.code AS code, jt.name AS name, jt.business_unit_id AS business_unit_id "
        "FROM _jobs j JOIN _call_hidden ch ON ch.call_id = j.call_id "
        "JOIN _service_catalog sc ON sc.id = ch.requested_category_id "
        "JOIN _job_types jt ON jt.id = sc.supported_job_type_id "
        "WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"job {job_id} has no resolvable true job type",
        )
    return row


def post_paid_completion(ctx, config: RunConfig, *, job_id: int, now_ms: int) -> int:
    conn = ctx.conn
    job = conn.execute(
        "SELECT customer_id, location_id FROM _jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"job {job_id} not found")
    customer = conn.execute(
        "SELECT segment FROM _customers WHERE id = ?", (job["customer_id"],)
    ).fetchone()
    if customer is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, f"job {job_id} has no customer of record"
        )
    job_type = true_job_type(conn, job_id)
    segment = str(customer["segment"])
    job_type_code = str(job_type["code"])
    completion_date = clock.service_date_for_ms(now_ms)

    amounts = config.resolved["invoices.amount"][job_type_code][segment]
    subtotal_cents = ctx.rng.triangular_cents(
        KeyedStreamName.JOB_FINANCE.value, EntityType.JOB.value, job_id, completion_date,
        "invoice_amount",
        amounts["min_cents"], amounts["mode_cents"], amounts["max_cents"], 0,
    )
    percent_range = config.resolved["materials.percent_range"][job_type_code]
    material_percent_basis_points = ctx.rng.bounded_int(
        KeyedStreamName.JOB_FINANCE.value, EntityType.JOB.value, job_id, completion_date,
        "material_percentage",
        percent_range["min_basis_points"], percent_range["max_basis_points"], 0,
    )
    material_cost_cents = basis_point_amount(subtotal_cents, material_percent_basis_points)
    total_cents = subtotal_cents

    invoice_cur = conn.execute(
        "INSERT INTO _invoices (invoice_number, job_id, customer_id, location_id, "
        "business_unit_id, invoice_date, issued_at, status, no_charge, subtotal_cents, "
        "total_cents, material_cost_cents, material_percent_basis_points, created_at) "
        "VALUES ('', ?, ?, ?, ?, ?, ?, 'paid', 0, ?, ?, ?, ?, ?)",
        (
            job_id, job["customer_id"], job["location_id"], job_type["business_unit_id"],
            completion_date, now_ms, subtotal_cents, total_cents, material_cost_cents,
            material_percent_basis_points, now_ms,
        ),
    )
    invoice_id = int(invoice_cur.lastrowid)
    conn.execute(
        "UPDATE _invoices SET invoice_number = ? WHERE id = ?",
        (f"INV-{invoice_id:06d}", invoice_id),
    )
    conn.execute(
        "INSERT INTO _invoice_items (invoice_id, item_type, description, quantity, "
        "amount_cents, created_at) VALUES (?, 'service', ?, 1, ?, ?)",
        (invoice_id, str(job_type["name"]), subtotal_cents, now_ms),
    )
    payment_cur = conn.execute(
        "INSERT INTO _payments (payment_number, invoice_id, customer_id, paid_at, "
        "amount_cents, payment_type, status, created_at) "
        "VALUES ('', ?, ?, ?, ?, 'immediate', 'posted', ?)",
        (invoice_id, job["customer_id"], now_ms, total_cents, now_ms),
    )
    payment_id = int(payment_cur.lastrowid)
    conn.execute(
        "UPDATE _payments SET payment_number = ? WHERE id = ?",
        (f"PAY-{payment_id:06d}", payment_id),
    )

    entries = [
        LedgerEntry(
            entry_type=LedgerCategory.REVENUE,
            amount_cents=total_cents,
            occurred_at_ms=now_ms,
            service_date=completion_date,
            description="paid completion revenue",
            job_id=job_id,
            invoice_id=invoice_id,
            payment_id=payment_id,
        ),
        LedgerEntry(
            entry_type=LedgerCategory.MATERIAL_COST,
            amount_cents=-material_cost_cents,
            occurred_at_ms=now_ms,
            service_date=completion_date,
            description="job material cost",
            job_id=job_id,
            invoice_id=invoice_id,
        ),
    ]
    ctx.services.ledger.post(entries)
    return invoice_id


def work_completed_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        appointment_id = int(payload["appointment_id"])
        technician_id = int(payload["technician_id"])
        service_date = _appointment_service_date(conn, appointment_id)
        job_id, location_id = _appointment_job_location_id(conn, appointment_id)

        updated = conn.execute(
            "UPDATE _appointments SET status = 'completed', actual_end = ?, "
            "modified_at = ? WHERE id = ? AND status = 'in_progress'",
            (now, now, appointment_id),
        )
        if updated.rowcount != 1:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"appointment {appointment_id} was not in_progress at work_completed",
            )
        _write_appointment_event(
            conn, appointment_id=appointment_id, technician_id=technician_id,
            event_type="work_completed", occurred_at=now, service_date=service_date,
        )
        classify_job(
            conn, job_id=job_id, status="completed", outcome="completed_paid",
            now_ms=now, completed_at_ms=now,
        )
        post_paid_completion(ctx, config, job_id=job_id, now_ms=now)
        loc = conn.execute(
            "SELECT latitude, longitude FROM _technician_locations WHERE technician_id = ?",
            (technician_id,),
        ).fetchone()
        _write_technician_location(
            conn, technician_id, state="on_site",
            latitude=float(loc["latitude"]), longitude=float(loc["longitude"]), updated_at=now,
        )
        conn.execute(
            "UPDATE _day_state SET unresolved_execution_count = unresolved_execution_count - 1 "
            "WHERE service_date = ?",
            (service_date,),
        )

        blocks = _appointment_blocks(conn, appointment_id)
        final_block_index = block_index(blocks[-1]["block_code"])
        origin = location_point(conn, location_id)
        plan_next_chain_step(
            ctx, config, artifact,
            technician_id=technician_id, service_date=service_date,
            from_appointment_id=appointment_id, from_location=origin,
            from_block_index=final_block_index, departure_ms=now,
        )
        return None

    return handler


def work_failed_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        appointment_id = int(payload["appointment_id"])
        technician_id = int(payload["technician_id"])
        reason = str(payload["reason"])
        service_date = _appointment_service_date(conn, appointment_id)
        job_id, location_id = _appointment_job_location_id(conn, appointment_id)

        updated = conn.execute(
            "UPDATE _appointments SET status = 'failed', modified_at = ? "
            "WHERE id = ? AND status = 'scheduled'",
            (now, appointment_id),
        )
        if updated.rowcount != 1:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"appointment {appointment_id} was not scheduled at work_failed",
            )
        _write_appointment_event(
            conn, appointment_id=appointment_id, technician_id=technician_id,
            event_type="work_failed", occurred_at=now, service_date=service_date,
        )
        classify_job(
            conn, job_id=job_id, status="failed", outcome=_WORK_FAILED_OUTCOMES[reason],
            now_ms=now,
        )
        loc = conn.execute(
            "SELECT latitude, longitude FROM _technician_locations WHERE technician_id = ?",
            (technician_id,),
        ).fetchone()
        _write_technician_location(
            conn, technician_id, state="on_site",
            latitude=float(loc["latitude"]), longitude=float(loc["longitude"]), updated_at=now,
        )
        conn.execute(
            "UPDATE _day_state SET unresolved_execution_count = unresolved_execution_count - 1 "
            "WHERE service_date = ?",
            (service_date,),
        )

        blocks = _appointment_blocks(conn, appointment_id)
        final_block_index = block_index(blocks[-1]["block_code"])
        origin = location_point(conn, location_id)
        plan_next_chain_step(
            ctx, config, artifact,
            technician_id=technician_id, service_date=service_date,
            from_appointment_id=appointment_id, from_location=origin,
            from_block_index=final_block_index, departure_ms=now,
        )
        return None

    return handler


def technician_return_home_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        from_appointment_id = int(payload["appointment_id"])
        technician_id = int(payload["technician_id"])
        service_date = str(payload["service_date"])
        block_index_value = int(payload["block_index"])

        _job_id, location_id = _appointment_job_location_id(conn, from_appointment_id)
        origin = location_point(conn, location_id)
        destination = technician_home_point(conn, technician_id)

        distance_km = geography.route_distance_km(
            origin.catalog_point.access_distance_km, origin.catalog_point.anchor_id,
            destination.catalog_point.access_distance_km, destination.catalog_point.anchor_id,
            artifact, config,
        )
        day_type = day_type_for(service_date).value
        minute_of_day = _minute_of_day(now)
        condition_code = _destination_condition_code(
            conn, destination.effective_zone_id, service_date
        )
        noise_key = travel.return_home_trip_noise_key(technician_id, service_date, block_index_value)
        travel_minutes = travel.actual_travel_minutes(
            ctx.rng, config,
            distance_km=distance_km, day_type=day_type, minute_of_day=minute_of_day,
            condition_code=condition_code, noise_key=noise_key,
        )
        arrives_at = now + travel_minutes * 60000

        conn.execute(
            "INSERT INTO _technician_movements (technician_id, service_date, movement_kind, "
            "origin_latitude, origin_longitude, destination_latitude, destination_longitude, "
            "departed_at, arrives_at, created_at) VALUES (?, ?, 'return_home', ?, ?, ?, ?, ?, ?, ?)",
            (
                technician_id, service_date,
                origin.latitude, origin.longitude,
                destination.latitude, destination.longitude,
                now, arrives_at, now,
            ),
        )
        _write_technician_location(
            conn, technician_id, state="traveling",
            latitude=origin.latitude, longitude=origin.longitude, updated_at=now,
            trip_origin=(origin.latitude, origin.longitude),
            trip_destination=(destination.latitude, destination.longitude),
            trip_departed_at=now, trip_travel_minutes=travel_minutes,
        )
        Events(conn, ctx.rng).schedule(
            EventKind.TECHNICIAN_HOME_ARRIVAL, arrives_at,
            entity_type=EntityType.TECHNICIAN.value, entity_id=technician_id,
            payload={"service_date": service_date, "technician_id": technician_id},
        )
        return None

    return handler


def technician_home_arrival_handler(config: RunConfig, artifact: GeoArtifact | None) -> EventHandler:

    def handler(ctx, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        conn = ctx.conn
        payload = _payload(row)
        technician_id = int(payload["technician_id"])
        service_date = str(payload["service_date"])

        home = technician_home_point(conn, technician_id)
        _write_technician_location(
            conn, technician_id, state="home",
            latitude=home.latitude, longitude=home.longitude, updated_at=now,
        )

        plan_next_dispatch_if_idle(
            ctx, config, artifact,
            technician_id=technician_id, service_date=service_date, floor_ms=now,
        )
        return None

    return handler


def build_handlers(config: RunConfig, artifact: GeoArtifact | None) -> dict[EventKind, EventHandler]:
    return {
        EventKind.OFF_HOURS_ACCRUAL_MARK: off_hours_accrual_mark_handler,
        EventKind.FIRST_TRIP_DISPATCH: first_trip_dispatch_handler(config, artifact),
        EventKind.APPOINTMENT_DISPATCH: appointment_dispatch_handler(config, artifact),
        EventKind.APPOINTMENT_ARRIVAL: appointment_arrival_handler(config, artifact),
        EventKind.WORK_STARTED: work_started_handler(config),
        EventKind.WORK_COMPLETED: work_completed_handler(config, artifact),
        EventKind.WORK_FAILED: work_failed_handler(config, artifact),
        EventKind.COMPANY_NO_SHOW: company_no_show_handler(config, artifact),
        EventKind.TECHNICIAN_RETURN_HOME: technician_return_home_handler(config, artifact),
        EventKind.TECHNICIAN_HOME_ARRIVAL: technician_home_arrival_handler(config, artifact),
    }


__all__ = [
    "BLOCK_CODES",
    "RoutablePoint",
    "block_code_at",
    "block_end_instant",
    "block_index",
    "block_start_instant",
    "build_handlers",
    "classify_job",
    "day_type_for",
    "dispatch_trip",
    "duration_variation_minutes",
    "execution_duration_multiplier",
    "first_trip_dispatch_applied",
    "handle_outgoing_technician",
    "location_point",
    "pending_dispatch_for_technician",
    "plan_dispatch_from_home",
    "plan_next_chain_step",
    "plan_next_dispatch_if_idle",
    "planned_travel_minutes",
    "replan_same_day",
    "start_variation_minutes",
    "technician_home_point",
]
