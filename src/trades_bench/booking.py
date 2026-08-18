from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext

from . import appointments, calls, clock, constants, surface
from .config import RunConfig, probability_threshold
from .enums import EventKind, KeyedStreamName
from .errors import EngineError, ErrorCode
from .events import Events
from .geo_reader import GeoArtifact
from .txn import Services, TxnContext, command

_BLOCK_START_BY_LABEL: dict[str, str] = {
    f"{start // 60:02d}:{start % 60:02d}": code
    for code, (start, _end) in zip(
        appointments.BLOCK_CODES, constants.BLOCK_GRID_MINUTES, strict=True
    )
}


def _state_illegal(reason: str, message: str, **details: object) -> EngineError:
    return EngineError(ErrorCode.STATE_ILLEGAL, message, details={"reason": reason, **details})


def _validation_error(rule: str, field: str, message: str) -> EngineError:
    return EngineError(ErrorCode.INVALID_ARGUMENT, message, details={"field": field, "rule": rule})


def _call_row(conn: sqlite3.Connection, call_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"call {call_id} not found")
    return row


_CALL_WINDOW_MINUTES_PER_DAY = constants.CALL_WINDOW_END_MIN - constants.CALL_WINDOW_START_MIN


def _day_call_bounds(d: date) -> tuple[int, int]:
    lo = clock.phoenix_instant(
        d.year, d.month, d.day,
        constants.CALL_WINDOW_START_MIN // 60, constants.CALL_WINDOW_START_MIN % 60,
    )
    hi = clock.phoenix_instant(
        d.year, d.month, d.day,
        constants.CALL_WINDOW_END_MIN // 60, constants.CALL_WINDOW_END_MIN % 60,
    )
    return lo, hi


def _minute_count(lo: int, hi: int) -> int:
    return max(0, (hi - lo) // 60_000 + 1)


@dataclass(frozen=True)
class _CancellationWindow:

    total: int
    start_date: date
    first_lo: int
    first_count: int
    full_days: int
    last_lo: int
    last_count: int


def _cancellation_arrival_window(now: int, upper_bound: int) -> _CancellationWindow:
    start_date = date.fromisoformat(clock.service_date_for_ms(now))
    end_date = date.fromisoformat(clock.service_date_for_ms(upper_bound))
    next_minute = ((now // 60_000) + 1) * 60_000
    upper_minute = (upper_bound // 60_000) * 60_000

    if end_date < start_date:
        return _CancellationWindow(0, start_date, 0, 0, 0, 0, 0)

    first_call_start, first_call_end = _day_call_bounds(start_date)
    first_lo = max(first_call_start, next_minute)

    if end_date == start_date:
        first_hi = min(first_call_end - 60_000, upper_minute)
        first_count = _minute_count(first_lo, first_hi)
        return _CancellationWindow(first_count, start_date, first_lo, first_count, 0, 0, 0)

    first_hi = first_call_end - 60_000
    first_count = _minute_count(first_lo, first_hi)

    full_days = (end_date - start_date).days - 1

    last_call_start, last_call_end = _day_call_bounds(end_date)
    last_lo = last_call_start
    last_hi = min(last_call_end - 60_000, upper_minute)
    last_count = _minute_count(last_lo, last_hi)

    total = first_count + full_days * _CALL_WINDOW_MINUTES_PER_DAY + last_count
    return _CancellationWindow(
        total, start_date, first_lo, first_count, full_days, last_lo, last_count
    )


def _acceptance_probability(
    base: Decimal, decay: Decimal, lead_days: Decimal, floor: Decimal
) -> Decimal:
    with localcontext() as decctx:
        decctx.prec = 50
        return max(floor, base * (-decay * lead_days).exp())


def _nth_cancellation_arrival(window: _CancellationWindow, index: int) -> int:
    offset = index
    if offset < window.first_count:
        return window.first_lo + offset * 60_000
    offset -= window.first_count

    full_minutes = window.full_days * _CALL_WINDOW_MINUTES_PER_DAY
    if offset < full_minutes:
        day_offset, minute_offset = divmod(offset, _CALL_WINDOW_MINUTES_PER_DAY)
        d = window.start_date + timedelta(days=day_offset + 1)
        day_lo, _day_hi = _day_call_bounds(d)
        return day_lo + minute_offset * 60_000
    offset -= full_minutes

    if offset < window.last_count:
        return window.last_lo + offset * 60_000
    raise EngineError(ErrorCode.INVARIANT_VIOLATED, "cancellation arrival index out of range")


def book(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    customer_id: int,
    location_id: int,
    job_type_id: int,
    service_date: str,
    block_start: str,
    technician_id: int,
    artifact: GeoArtifact | None = None,
) -> dict:
    for name, value in (
        ("call_id", call_id), ("customer_id", customer_id), ("location_id", location_id),
        ("job_type_id", job_type_id), ("technician_id", technician_id),
    ):
        surface.require_id(name, value)
    for name, value in (("service_date", service_date), ("block_start", block_start)):
        surface.require_str(name, value)

    conn = ctx.conn
    now = ctx.now()

    call = _call_row(conn, call_id)
    calls.require_current_new_job_call(ctx, call)

    if call["customer_id"] is None or call["location_id"] is None:
        raise _state_illegal(
            "registration_incomplete",
            f"call {call_id} has not completed customer/location registration",
        )
    if customer_id != call["customer_id"]:
        raise _state_illegal(
            "customer_mismatch",
            f"customer_id {customer_id} does not match the call's customer "
            f"{call['customer_id']}",
        )
    if location_id != call["location_id"]:
        raise _state_illegal(
            "location_mismatch",
            f"location_id {location_id} does not match the call's location "
            f"{call['location_id']}",
        )

    location = conn.execute(
        "SELECT customer_id, effective_zone_id FROM _locations WHERE id = ?", (location_id,)
    ).fetchone()
    if location is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"location {location_id} not found")
    if location["customer_id"] != customer_id:
        raise _state_illegal(
            "location_customer_mismatch",
            f"location {location_id} belongs to customer {location['customer_id']}, "
            f"not customer_id {customer_id}",
        )

    technician = conn.execute(
        "SELECT final_working_date FROM _technicians WHERE id = ?",
        (technician_id,),
    ).fetchone()
    if technician is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"technician_id {technician_id} not found")

    service_date = surface.require_service_date("service_date", service_date)

    if (
        technician["final_working_date"] is not None
        and service_date >= technician["final_working_date"]
    ):
        raise _state_illegal(
            "final_working_date",
            f"technician {technician_id} has a final working date on or before {service_date}",
        )

    first_block_code = _BLOCK_START_BY_LABEL.get(block_start)
    if first_block_code is None:
        raise _validation_error(
            "block_alignment", "block_start",
            f"block_start {block_start!r} is not a valid grid start",
        )

    plan = appointments.validate_booking(
        ctx, config,
        job_type_id=job_type_id, technician_id=technician_id,
        service_date=service_date, first_block_code=first_block_code,
    )

    scheduled_start = plan.windows[0][0]
    urgency_class = "urgent" if call["urgent"] else "standard"
    params = conn.execute(
        "SELECT base_micros, decay_micros, floor_micros FROM _acceptance_parameters "
        "WHERE urgency_class = ?",
        (urgency_class,),
    ).fetchone()
    if params is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"no acceptance parameters for urgency_class {urgency_class!r}",
        )

    lead_ms = scheduled_start - call["arrived_at"]
    lead_days = Decimal(lead_ms) / Decimal(86_400_000)

    base = Decimal(params["base_micros"]) / Decimal(10**6)
    decay = Decimal(params["decay_micros"]) / Decimal(10**6)
    floor_p = Decimal(params["floor_micros"]) / Decimal(10**6)
    p_accept = _acceptance_probability(base, decay, lead_days, floor_p)

    accept_threshold = probability_threshold(p_accept)
    accepted = ctx.rng.chance(
        KeyedStreamName.ACCEPTANCE.value, "call", call_id, "", "offer_acceptance",
        accept_threshold, draw_index=0,
    )
    conn.execute(
        "UPDATE _call_hidden SET acceptance_result = ? WHERE call_id = ?",
        (int(accepted), call_id),
    )

    if not accepted:
        calls.record_resolved_attempt(ctx, int(call_id))
        calls.ordinary_rejection(ctx, config, call, terminal_reason="customer_declined")
        return {
            "call_id": call_id,
            "outcome": "customer_declined",
            "job_id": None,
            "appointment_id": None,
            "assignment_id": None,
            "blocks": [],
            "message": "Customer declined the offered appointment.",
        }

    created = appointments.create_booking(
        ctx, config, plan,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        requested_service_name=call["requested_service_name"],
        priority=("urgent" if call["urgent"] else "normal"),
        summary=call["requested_service_name"],
        recall_for_job_id=call["related_job_id"],
        artifact=artifact,
    )
    job_id = created["job_id"]
    appointment_id = created["appointment_id"]

    conn.execute(
        "UPDATE _calls SET status = 'booked', terminal_reason = 'accepted', "
        "terminal_at = ?, created_job_id = ? WHERE id = ?",
        (now, job_id, call_id),
    )
    calls.record_resolved_attempt(ctx, int(call_id))

    creation_date = clock.service_date_for_ms(now)
    u01 = ctx.rng.uniform01(
        KeyedStreamName.JOB_QUALITY.value, "job", job_id, creation_date, "complexity", 0,
    )
    dist = config.resolved["quality.complexity_distribution"][plan.job_type_code]
    complexity = dist["min"] + (dist["max"] - dist["min"]) * u01
    complexity_micros = int(
        (complexity * Decimal(10**6)).to_integral_value(rounding=ROUND_FLOOR)
    )
    complexity_micros = max(0, min(1_000_000, complexity_micros))
    conn.execute(
        "INSERT INTO _job_outcomes (job_id, complexity_micros, created_at) VALUES (?, ?, ?)",
        (job_id, complexity_micros, now),
    )

    customer_row = conn.execute(
        "SELECT segment FROM _customers WHERE id = ?", (customer_id,)
    ).fetchone()
    if customer_row is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"customer {customer_id} not found")
    segment = customer_row["segment"]

    storm_row = conn.execute(
        "SELECT storm_flag FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (location["effective_zone_id"], service_date),
    ).fetchone()
    storm = storm_row is not None and bool(storm_row["storm_flag"])

    base_by_segment = config.resolved["cancellations.base_probability_by_segment"]
    urgent_multiplier = config.get_decimal("cancellations.urgent_multiplier")
    lead_days_coefficient = config.get_decimal("cancellations.lead_days_coefficient")
    storm_multiplier = config.get_decimal("cancellations.storm_multiplier")
    p_cancel = (
        base_by_segment[segment]
        * (urgent_multiplier if call["urgent"] else Decimal(1))
        * (Decimal(1) + lead_days_coefficient * lead_days)
        * (storm_multiplier if storm else Decimal(1))
    )
    p_cancel = max(Decimal(0), min(Decimal(1), p_cancel))
    cancel_threshold = probability_threshold(p_cancel)
    cancel_exists = ctx.rng.chance(
        KeyedStreamName.CANCELLATION.value, "appointment", appointment_id, "",
        "cancellation_exists", cancel_threshold, draw_index=0,
    )

    if not cancel_exists:
        conn.execute(
            "INSERT INTO _cancellation_schedule "
            "(appointment_id, cancel_exists, arrival_at, event_id, created_at) "
            "VALUES (?, 0, NULL, NULL, ?)",
            (appointment_id, now),
        )
    else:
        cutoff_minutes = config.get_int("cancellations.cancellation_cutoff_minutes")
        upper_bound = scheduled_start - cutoff_minutes * 60_000
        window = _cancellation_arrival_window(now, upper_bound)
        if window.total == 0:
            conn.execute(
                "INSERT INTO _cancellation_schedule "
                "(appointment_id, cancel_exists, arrival_at, event_id, created_at) "
                "VALUES (?, 1, NULL, NULL, ?)",
                (appointment_id, now),
            )
        else:
            u = ctx.rng.draw(
                KeyedStreamName.CANCELLATION.value, "appointment", appointment_id, "",
                "cancellation_arrival", 1,
            )
            arrival_at = _nth_cancellation_arrival(window, u % window.total)
            event_id = Events(conn, ctx.rng).schedule(
                EventKind.CALL_INVOCATION, arrival_at,
                entity_type="appointment", entity_id=appointment_id,
                payload={"appointment_id": appointment_id},
            )
            conn.execute(
                "INSERT INTO _cancellation_schedule "
                "(appointment_id, cancel_exists, arrival_at, event_id, created_at) "
                "VALUES (?, 1, ?, ?, ?)",
                (appointment_id, arrival_at, event_id, now),
            )

    blocks = [
        {"start": clock.to_iso8601(start), "end": clock.to_iso8601(end)}
        for start, end in plan.windows
    ]
    return {
        "call_id": call_id,
        "outcome": "accepted",
        "job_id": job_id,
        "appointment_id": appointment_id,
        "assignment_id": created["assignment_id"],
        "blocks": blocks,
        "message": "Customer accepted. Booking created.",
    }


def book_command(
    services: Services,
    request_id: str,
    config: RunConfig,
    *,
    call_id: int,
    customer_id: int,
    location_id: int,
    job_type_id: int,
    service_date: str,
    block_start: str,
    technician_id: int,
    artifact: GeoArtifact | None = None,
) -> bytes:
    return command(
        services,
        request_id,
        lambda ctx: book(
            ctx, config,
            call_id=call_id, customer_id=customer_id, location_id=location_id,
            job_type_id=job_type_id, service_date=service_date, block_start=block_start,
            technician_id=technician_id, artifact=artifact,
        ),
        operation="jobs.book",
        args={
            "call_id": call_id,
            "customer_id": customer_id,
            "location_id": location_id,
            "job_type_id": job_type_id,
            "service_date": service_date,
            "block_start": block_start,
            "technician_id": technician_id,
        },
    )


__all__ = ["book", "book_command"]
