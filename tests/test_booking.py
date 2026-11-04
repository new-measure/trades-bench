from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, localcontext
from itertools import count

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    audit_count,
    geo_artifact_sha256,
    next_test_origin,
    register_conn,
)
from trades_bench import (
    appointments,
    booking,
    calls,
    clock,
    crm,
    db,
    geo_reader,
    ops_read,
    origin_keys,
    sql_gate,
)
from trades_bench.adapter import AdapterResponse
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import EventKind, RunStatus
from trades_bench.errors import ErrorCode
from trades_bench.events import Events
from trades_bench.registry import OPERATIONS_BY_NAME
from trades_bench.run_init import initialize_run, open_run_db

SEED = 17
START_DATE = "2026-10-01"
END_DATE = "2026-10-05"

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)

_ACCEPT_ALWAYS = {
    "acceptance.base_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
    "acceptance.floor_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
}
_DECLINE_ALWAYS = {
    "acceptance.base_probability_by_urgency": {"standard": 0.0, "urgent": 0.0},
    "acceptance.floor_probability_by_urgency": {"standard": 0.0, "urgent": 0.0},
}
_CANCEL_ALWAYS = {
    "cancellations.base_probability_by_segment": {
        "residential": 1.0, "commercial": 1.0, "apartment": 1.0,
    },
}
_CANCEL_NEVER = {
    "cancellations.base_probability_by_segment": {
        "residential": 0.0, "commercial": 0.0, "apartment": 0.0,
    },
}

_request_seq = count(1)
_arrival_seq = count(0)


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": END_DATE,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _init(path, config, *, seed=SEED):
    initialize_run(path, master_seed=seed, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    return open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)


def _build(tmp_path, *, seed=SEED, config=None, name="run.db"):
    cfg = config if config is not None else _config()
    conn = _init(tmp_path / name, cfg, seed=seed)
    register_conn(conn)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    return conn, cfg, controller


def _now_ms(conn) -> int:
    return conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]


def _set_clock(conn, ms: int, service_date: str) -> None:
    conn.execute(
        "UPDATE _clock SET current_time_ms = ?, service_date = ? WHERE singleton = 1",
        (ms, service_date),
    )


def _open_invocation(conn, now, invocation_type="call"):
    conn.execute(
        "INSERT INTO _invocations (type, ts_ms, sequence, status) "
        "VALUES (?, ?, "
        "(SELECT COALESCE(MAX(sequence), 0) + 1 FROM _invocations), 'active')",
        (invocation_type, now),
    )


def _usable_catalog_rows(conn, count_):
    rows = conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT ?",
        (count_,),
    ).fetchall()
    assert len(rows) == count_
    return rows


def _insert_customer(conn, now, segment="residential", propensity_micros=1_000_000):
    cur = conn.execute(
        "INSERT INTO _customers (name, segment, status, created_at, modified_at) "
        "VALUES ('Fixture Customer', ?, 'active', ?, ?)",
        (segment, now, now),
    )
    customer_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO _customer_repeat_state (customer_id, propensity_micros, last_outcome, "
        "competitor_captured_at, updated_at, origin_key) VALUES (?, ?, NULL, NULL, ?, ?)",
        (customer_id, propensity_micros, now, f"t:customer:{customer_id}"),
    )
    return customer_id


def _insert_location(conn, now, customer_id, catalog):
    cur = conn.execute(
        "INSERT INTO _locations (customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, zone_id, active, created_at, modified_at, "
        "effective_zone_id, address_catalog_id) "
        "VALUES (?, 'Main', ?, '', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            customer_id,
            f"{catalog['house_number']} {catalog['street']}",
            catalog["city"],
            catalog["state"],
            catalog["postal_code"],
            catalog["latitude"],
            catalog["longitude"],
            catalog["zone_id"],
            now,
            now,
            catalog["effective_zone_id"],
            catalog["id"],
        ),
    )
    return int(cur.lastrowid)


def _insert_bare_call(
    conn, now, *, call_kind="new_job", status="active", customer_id=None, location_id=None,
):
    cur = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, customer_id, location_id, requested_service_name, urgent, created_at) "
        "VALUES (?, ?, ?, ?, 'Fixture Caller', '4805551234', ?, ?, 'HVAC service', 0, ?)",
        (now, START_DATE, call_kind, status, customer_id, location_id, now),
    )
    return int(cur.lastrowid)


def _insert_pending(conn, now, arrival_at, **overrides):
    fields = {
        "service_date": START_DATE,
        "zone_id": None,
        "arrival_at": arrival_at,
        "call_kind": "new_job",
        "channel": "existing_customer",
        "segment": "residential",
        "requested_category_id": 2,
        "urgent": 0,
        "customer_id": None,
        "location_id": None,
        "address_catalog_id": None,
        "caller_name": "Fixture Caller",
        "caller_phone": "4805551234",
        "prospect_id": None,
        "related_job_id": None,
        "slot_index": None,
        "origin_key": next_test_origin(conn),
        "created_at": now,
    }
    fields.update(overrides)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO _pending_calls ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return int(cur.lastrowid)


def _arrive_applied(conn, rng, pending_id, arrival_at):
    db.begin(conn)
    rng.enter_txn()
    try:
        event_id = Events(conn, rng).schedule(
            EventKind.CALL_INVOCATION,
            arrival_at,
            entity_type="pending_call",
            entity_id=pending_id,
        )
        event_row = conn.execute(
            "SELECT * FROM _events WHERE event_id = ?", (event_id,)
        ).fetchone()
        call_id = calls.arrive(conn, event_row, now_ms=arrival_at)
        conn.execute(
            "UPDATE _events SET status = 'applied', applied_at_ms = ? WHERE event_id = ?",
            (arrival_at, event_id),
        )
        conn.execute(
            "UPDATE _invocations SET origin_event_id = ? WHERE status = 'active'",
            (event_id,),
        )
    except BaseException:
        rng.exit_txn()
        db.rollback(conn)
        raise
    rng.exit_txn()
    db.commit(conn)
    return call_id


def _arrival_at():
    return clock.phoenix_instant(2026, 10, 1, 9, 0) + next(_arrival_seq) * 60_000


def _registered_call(
    conn, controller, *, customer_id, location_id, segment="residential", urgent=0,
    related_job_id=None,
):
    now = _now_ms(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        channel="existing_customer", segment=segment, customer_id=customer_id,
        location_id=location_id, urgent=urgent, related_job_id=related_job_id,
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


def _fresh_registered_call(conn, controller, catalog, **kwargs):
    now = _now_ms(conn)
    customer_id = _insert_customer(conn, now, segment=kwargs.get("segment", "residential"))
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id, **kwargs)
    return call_id, customer_id, location_id


def _next_request_id(prefix: str) -> str:
    return f"{prefix}.{next(_request_seq)}"


def _book(controller, cfg, *, request_id=None, **kwargs):
    rid = request_id if request_id is not None else _next_request_id("book")
    response = booking.book_command(controller.services, rid, cfg, **kwargs)
    return response, json.loads(response)


def _reject(controller, cfg, *, call_id, request_id=None):
    rid = request_id if request_id is not None else _next_request_id("reject")
    response = calls.reject_command(controller.services, rid, cfg, call_id=call_id)
    return response, json.loads(response)


def _ok(body: dict) -> dict:
    assert body["ok"] is True, body
    return body["result"]


def _err(body: dict) -> dict:
    assert body["ok"] is False, body
    return body["error"]


def _call_row(conn, call_id):
    return conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()


def _call_terminal_snapshot(conn, call_id):
    row = conn.execute(
        "SELECT status, terminal_reason, terminal_at, created_job_id FROM _calls WHERE id = ?",
        (call_id,),
    ).fetchone()
    return tuple(row)


def _customer_repeat_row(conn, customer_id):
    return conn.execute(
        "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
    ).fetchone()


def _rng_snapshot(conn):
    rows = conn.execute(
        "SELECT stream_name, seed, counter FROM _rng_state ORDER BY stream_name"
    ).fetchall()
    return [tuple(row) for row in rows]


_BOOKING_TABLES = (
    "_jobs", "_appointments", "_appointment_blocks", "_appointment_assignments",
    "_technician_block_holds", "_job_outcomes", "_cancellation_schedule",
    "_ledger_entries", "_notifications", "_events",
)


def _table_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def _counts(conn, tables=_BOOKING_TABLES):
    return {table: _table_count(conn, table) for table in tables}


def _world_snapshot(conn):
    return (audit_count(conn), _counts(conn))


def _audit_rows(conn, **filters):
    clauses = " AND ".join(f"{k} = ?" for k in filters)
    where = f"WHERE {clauses}" if clauses else ""
    rows = conn.execute(
        f"SELECT stream_name, entity_type, entity_id, scope, event_kind, draw_index "
        f"FROM _random_audit {where} ORDER BY seq",
        tuple(filters.values()),
    ).fetchall()
    return [tuple(row) for row in rows]


def test_offer_validation_failures_leave_world_unchanged_then_offer_is_consumed(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)

    unregistered_arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, unregistered_arrival_at,
        channel="new_caller", address_catalog_id=catalog["id"],
    )
    unregistered_call_id = _arrive_applied(
        conn, controller.services.rng, pending_id, unregistered_arrival_at,
    )
    before_unreg_world = _world_snapshot(conn)
    _, unreg_body = _book(
        controller, cfg,
        call_id=unregistered_call_id, customer_id=1, location_id=1,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    unreg_err = _err(unreg_body)
    assert unreg_err["code"] == ErrorCode.STATE_ILLEGAL.value
    assert unreg_err["details"]["reason"] == "registration_incomplete"
    assert _world_snapshot(conn) == before_unreg_world

    training_start = clock.phoenix_instant(2026, 10, 1, 11, 0)
    training = conn.execute(
        "INSERT INTO _non_job_appointments (technician_id, kind, name, start_at, end_at, "
        "block_code, status, created_at) VALUES (1, 'training', 'course', ?, ?, 'B11_13', "
        "'scheduled', ?)",
        (training_start, training_start + 7_200_000, now),
    )
    conn.execute(
        "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
        "appointment_id, non_job_appointment_id, created_at) VALUES (1, ?, 'training', "
        "NULL, ?, ?)",
        (training_start, int(training.lastrowid), now),
    )

    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    defaults = {
        "call_id": call_id, "customer_id": customer_id, "location_id": location_id,
        "job_type_id": 1, "service_date": START_DATE, "block_start": "09:00",
        "technician_id": 1,
    }

    def _assert_rejected(reason, code, **overrides):
        before_world = _world_snapshot(conn)
        before_rng = _rng_snapshot(conn)
        before_terminal = _call_terminal_snapshot(conn, call_id)
        _, body = _book(controller, cfg, **{**defaults, **overrides})
        err = _err(body)
        assert err["code"] == code
        if reason is not None:
            assert err["details"]["reason"] == reason
        assert _world_snapshot(conn) == before_world
        assert _rng_snapshot(conn) == before_rng
        assert _call_terminal_snapshot(conn, call_id) == before_terminal

    _assert_rejected(
        "customer_mismatch", ErrorCode.STATE_ILLEGAL.value, customer_id=customer_id + 999,
    )
    _assert_rejected(
        "location_mismatch", ErrorCode.STATE_ILLEGAL.value, location_id=location_id + 999,
    )
    _assert_rejected(None, ErrorCode.ENTITY_NOT_FOUND.value, technician_id=999999)
    _assert_rejected(None, ErrorCode.ENTITY_NOT_FOUND.value, job_type_id=999999)
    _assert_rejected(
        "shift_coverage", ErrorCode.STATE_ILLEGAL.value, technician_id=2, block_start="17:00",
    )
    _assert_rejected("overlap", ErrorCode.STATE_ILLEGAL.value, block_start="11:00")

    _, body = _book(controller, cfg, **defaults)
    result = _ok(body)
    assert result["outcome"] == "accepted"

    _, second_body = _book(controller, cfg, **defaults)
    second_err = _err(second_body)
    assert second_err["details"]["reason"] == "call_not_active"

    _, reject_body = _reject(controller, cfg, call_id=call_id)
    reject_err = _err(reject_body)
    assert reject_err["details"]["reason"] == "call_not_active"

    location_body = crm.create_location_command(
        controller.services, _next_request_id("create_location"), cfg,
        call_id=call_id, customer_id=customer_id, address=catalog["formatted_address"],
        artifact=ARTIFACT,
    )
    location_err = _err(json.loads(location_body))
    assert location_err["details"]["reason"] == "call_not_active"


def test_block_already_started_rejected_and_leaves_world_unchanged(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _set_clock(conn, clock.phoenix_instant(2026, 10, 1, 9, 30), START_DATE)

    before_world = _world_snapshot(conn)
    before_rng = _rng_snapshot(conn)
    before_terminal = _call_terminal_snapshot(conn, call_id)
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    err = _err(body)
    assert err["details"]["reason"] == "block_already_started"
    assert _world_snapshot(conn) == before_world
    assert _rng_snapshot(conn) == before_rng
    assert _call_terminal_snapshot(conn, call_id) == before_terminal


def test_accepted_booking_writes_all_tables_atomically(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    before = _counts(conn)
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=2, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    after = _counts(conn)

    assert after["_jobs"] == before["_jobs"] + 1
    assert after["_appointments"] == before["_appointments"] + 1
    assert after["_appointment_blocks"] == before["_appointment_blocks"] + 1
    assert after["_appointment_assignments"] == before["_appointment_assignments"] + 1
    assert after["_technician_block_holds"] == before["_technician_block_holds"] + 1
    assert after["_job_outcomes"] == before["_job_outcomes"] + 1
    assert after["_cancellation_schedule"] == before["_cancellation_schedule"] + 1
    assert after["_ledger_entries"] == before["_ledger_entries"]
    assert after["_notifications"] == before["_notifications"]

    call = _call_row(conn, call_id)
    assert call["status"] == "booked"
    assert call["terminal_reason"] == "accepted"
    assert call["created_job_id"] == result["job_id"]
    hidden = conn.execute(
        "SELECT acceptance_result FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    assert hidden["acceptance_result"] == 1

    assert set(result) == {
        "call_id", "outcome", "job_id", "appointment_id", "assignment_id", "blocks", "message",
    }
    assert result["call_id"] == call_id
    assert result["outcome"] == "accepted"
    assert result["job_id"] is not None
    assert result["appointment_id"] is not None
    assert result["assignment_id"] is not None
    assert result["message"] == "Customer accepted. Booking created."
    assert result["blocks"] == [
        {
            "start": clock.to_iso8601(clock.phoenix_instant(2026, 10, 1, 9, 0)),
            "end": clock.to_iso8601(clock.phoenix_instant(2026, 10, 1, 11, 0)),
        },
    ]


def test_declined_booking_writes_nothing(tmp_path):
    config = _config(**_DECLINE_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    before = _counts(conn)
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    assert result == {
        "call_id": call_id, "outcome": "customer_declined", "job_id": None,
        "appointment_id": None, "assignment_id": None, "blocks": [],
        "message": "Customer declined the offered appointment.",
    }
    assert _counts(conn) == before

    call = _call_row(conn, call_id)
    assert call["status"] == "declined"
    assert call["terminal_reason"] == "customer_declined"
    assert call["created_job_id"] is None
    hidden = conn.execute(
        "SELECT acceptance_result FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    assert hidden["acceptance_result"] == 0


def _decline_via(tmp_path, name, *, via_booking):
    config = _config(**_DECLINE_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config, name=name)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    if via_booking:
        _, body = _book(
            controller, cfg,
            call_id=call_id, customer_id=customer_id, location_id=location_id,
            job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
        )
    else:
        _, body = _reject(controller, cfg, call_id=call_id)
    _ok(body)

    row = _customer_repeat_row(conn, customer_id)
    call = _call_row(conn, call_id)
    snapshot = {
        "propensity_micros": row["propensity_micros"],
        "last_outcome": row["last_outcome"],
        "competitor_captured_at_is_null": row["competitor_captured_at"] is None,
        "status": call["status"],
        "terminal_reason": call["terminal_reason"],
        "terminal_at": call["terminal_at"],
    }
    call_origin = origin_keys.of_call(conn, call_id)
    competitor_rows = _audit_rows(conn, stream_name="competitor_leak", scope=call_origin)
    acceptance_rows = _audit_rows(conn, stream_name="acceptance", scope=call_origin)
    conn.close()
    return snapshot, competitor_rows, acceptance_rows, call_origin


def test_decline_via_booking_equals_decline_via_reject(tmp_path):
    snap_booking, comp_booking, accept_booking, origin_booking = _decline_via(
        tmp_path, "decline_via_booking.db", via_booking=True,
    )
    snap_reject, comp_reject, accept_reject, origin_reject = _decline_via(
        tmp_path, "decline_via_reject.db", via_booking=False,
    )

    assert origin_booking == origin_reject
    assert comp_booking == comp_reject == []
    assert accept_reject == []
    assert accept_booking == [
        ("acceptance", "call_origin", 1, origin_booking, "offer_acceptance", 0),
    ]

    assert snap_booking["propensity_micros"] == snap_reject["propensity_micros"]
    assert snap_booking["last_outcome"] == snap_reject["last_outcome"]
    assert snap_booking["competitor_captured_at_is_null"] == snap_reject["competitor_captured_at_is_null"]
    assert snap_booking["terminal_at"] == snap_reject["terminal_at"]

    assert snap_booking["status"] == "declined"
    assert snap_booking["terminal_reason"] == "customer_declined"
    assert snap_reject["status"] == "rejected"
    assert snap_reject["terminal_reason"] == "agent_rejected"


def test_offer_horizon_has_no_cap(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog_1, catalog_2 = _usable_catalog_rows(conn, 2)
    _open_invocation(conn, now)

    for offset_days, catalog in ((30, catalog_1), (400, catalog_2)):
        customer_id = _insert_customer(conn, now)
        location_id = _insert_location(conn, now, customer_id, catalog)
        call_id = _registered_call(
            conn, controller, customer_id=customer_id, location_id=location_id,
        )
        service_date = (date.fromisoformat(START_DATE) + timedelta(days=offset_days)).isoformat()
        assert service_date > cfg.get_string("run.end_date")

        _, body = _book(
            controller, cfg,
            call_id=call_id, customer_id=customer_id, location_id=location_id,
            job_type_id=1, service_date=service_date, block_start="09:00", technician_id=1,
        )
        result = _ok(body)
        assert result["outcome"] == "accepted"
        job = conn.execute("SELECT * FROM _jobs WHERE id = ?", (result["job_id"],)).fetchone()
        assert job is not None
        appointment = conn.execute(
            "SELECT scheduled_start FROM _appointments WHERE id = ?", (result["appointment_id"],),
        ).fetchone()
        assert clock.service_date_for_ms(appointment["scheduled_start"]) == service_date


def test_acceptance_outcome_is_independent_of_technician_identity(tmp_path):
    outcomes = {}
    for technician_id, name in ((1, "tech_a.db"), (3, "tech_b.db")):
        config = _config()
        conn, cfg, controller = _build(tmp_path, config=config, name=name)
        now = _now_ms(conn)
        catalog = _usable_catalog_rows(conn, 1)[0]
        _open_invocation(conn, now)
        customer_id = _insert_customer(conn, now)
        location_id = _insert_location(conn, now, customer_id, catalog)
        call_id = _registered_call(
            conn, controller, customer_id=customer_id, location_id=location_id,
        )
        _, body = _book(
            controller, cfg,
            call_id=call_id, customer_id=customer_id, location_id=location_id,
            job_type_id=1, service_date=START_DATE, block_start="09:00",
            technician_id=technician_id,
        )
        result = _ok(body)
        hidden = conn.execute(
            "SELECT acceptance_result FROM _call_hidden WHERE call_id = ?", (call_id,)
        ).fetchone()
        outcomes[technician_id] = (
            result["outcome"],
            hidden["acceptance_result"],
            _audit_rows(conn, scope=origin_keys.of_call(conn, call_id)),
        )
        conn.close()

    assert outcomes[1] == outcomes[3]


def _call_with_fixed_lead(conn, controller, catalog, arrived_at, **overrides):
    now = _now_ms(conn)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    pending_id = _insert_pending(
        conn, now, arrived_at,
        channel="existing_customer", customer_id=customer_id, location_id=location_id,
        **overrides,
    )
    call_id = _arrive_applied(conn, controller.services.rng, pending_id, arrived_at)
    return call_id, customer_id, location_id


def test_acceptance_declines_at_long_lead_and_accepts_at_short_lead_under_fixed_decay(tmp_path):
    config = _config(**{
        "acceptance.base_probability_by_urgency": {"standard": 0.999, "urgent": 0.999},
        "acceptance.lead_time_decay_by_urgency": {"standard": 2.0, "urgent": 2.0},
        "acceptance.floor_probability_by_urgency": {"standard": 0.0, "urgent": 0.0},
    })

    conn_short, cfg_short, controller_short = _build(
        tmp_path, config=config, name="accept_short_lead.db",
    )
    now_short = _now_ms(conn_short)
    catalog_short = _usable_catalog_rows(conn_short, 1)[0]
    _open_invocation(conn_short, now_short)
    short_arrival = clock.phoenix_instant(2026, 10, 1, 8, 59)
    call_short, customer_short, location_short = _call_with_fixed_lead(
        conn_short, controller_short, catalog_short, short_arrival,
    )
    _, short_body = _book(
        controller_short, cfg_short,
        call_id=call_short, customer_id=customer_short, location_id=location_short,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    short_result = _ok(short_body)
    assert short_result["outcome"] == "accepted"

    conn_long, cfg_long, controller_long = _build(
        tmp_path, config=config, name="accept_long_lead.db",
    )
    now_long = _now_ms(conn_long)
    catalog_long = _usable_catalog_rows(conn_long, 1)[0]
    _open_invocation(conn_long, now_long)
    long_arrival = clock.phoenix_instant(2026, 10, 1, 8, 59)
    call_long, customer_long, location_long = _call_with_fixed_lead(
        conn_long, controller_long, catalog_long, long_arrival,
    )
    far_date = (date.fromisoformat(START_DATE) + timedelta(days=30)).isoformat()
    _, long_body = _book(
        controller_long, cfg_long,
        call_id=call_long, customer_id=customer_long, location_id=location_long,
        job_type_id=1, service_date=far_date, block_start="09:00", technician_id=1,
    )
    long_result = _ok(long_body)
    assert long_result["outcome"] == "customer_declined"


def test_off_hours_acceptance_multiplier_leaves_standard_and_urgent_unchanged_by_default():
    multipliers = _config().resolved["acceptance.off_hours_multiplier_by_urgency"]
    p_accept = Decimal("0.9")
    standard = booking.offer_acceptance_probability(
        p_accept, off_hours=True, off_hours_multiplier=multipliers["standard"]
    )
    urgent = booking.offer_acceptance_probability(
        p_accept, off_hours=True, off_hours_multiplier=multipliers["urgent"]
    )
    daytime = booking.offer_acceptance_probability(
        p_accept, off_hours=False, off_hours_multiplier=multipliers["standard"]
    )
    assert standard == p_accept
    assert urgent == p_accept
    assert daytime == p_accept
    floored = booking._acceptance_probability(
        Decimal("0.9"), Decimal("0.15"), Decimal("1000"), Decimal("0.25")
    )
    assert booking.offer_acceptance_probability(
        floored, off_hours=True, off_hours_multiplier=Decimal("0.4")
    ) == Decimal("0.100")


@pytest.mark.parametrize(
    ("urgent", "service_date", "block_start", "expected"),
    [
        (0, "2026-10-01", "17:00", "customer_declined"),
        (0, "2026-10-03", "09:00", "customer_declined"),
        (0, "2026-10-01", "09:00", "accepted"),
        (1, "2026-10-01", "17:00", "accepted"),
    ],
)
def test_off_hours_offers_apply_the_urgency_multiplier_to_acceptance(
    tmp_path, urgent, service_date, block_start, expected
):
    config = _config(**{
        "run.end_date": "2026-10-03",
        "acceptance.base_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
        "acceptance.lead_time_decay_by_urgency": {"standard": 0.0, "urgent": 0.0},
        "acceptance.floor_probability_by_urgency": {"standard": 0.0, "urgent": 0.0},
        "acceptance.off_hours_multiplier_by_urgency": {"standard": 0.0, "urgent": 1.0},
    })
    conn, cfg, controller = _build(tmp_path, config=config, name="off_hours_accept.db")
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id, customer_id, location_id = _call_with_fixed_lead(
        conn, controller, catalog, clock.phoenix_instant(2026, 10, 1, 8, 59), urgent=urgent,
    )
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=service_date, block_start=block_start, technician_id=1,
    )
    assert _ok(body)["outcome"] == expected


def test_urgent_acceptance_is_certain_same_day_then_drops_by_calendar_day():
    config = _config()
    assert [booking.urgent_acceptance_probability(config, lead) for lead in (0, 1, 2, 9)] == [
        Decimal("1"), Decimal("0.15"), Decimal("0.05"), Decimal("0.05"),
    ]


@pytest.mark.parametrize(
    ("by_lead_day", "service_date", "block_start", "expected"),
    [
        ([1, 0, 0], "2026-10-01", "17:00", "accepted"),
        ([1, 0, 0], "2026-10-02", "07:00", "customer_declined"),
        ([0, 1, 0], "2026-10-02", "07:00", "accepted"),
        ([0, 0, 1], "2026-10-03", "09:00", "accepted"),
        ([1, 1, 0], "2026-10-03", "09:00", "customer_declined"),
    ],
)
def test_urgent_offers_use_the_calendar_day_lead_probability(
    tmp_path, by_lead_day, service_date, block_start, expected
):
    config = _config(**{
        "run.end_date": "2026-10-03",
        "acceptance.urgent_probability_by_lead_day": by_lead_day,
    })
    conn, cfg, controller = _build(tmp_path, config=config, name="urgent_accept.db")
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id, customer_id, location_id = _call_with_fixed_lead(
        conn, controller, catalog, clock.phoenix_instant(2026, 10, 1, 8, 59), urgent=1,
    )
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=service_date, block_start=block_start, technician_id=1,
    )
    assert _ok(body)["outcome"] == expected


def test_install_late_window_multiplier_applies_only_to_installs_from_three_pm():
    config = _config()
    p_accept = Decimal("0.9")

    def adjusted(job_type_code, block_code):
        return booking.install_window_acceptance_probability(
            config, p_accept, job_type_code=job_type_code, first_block_code=block_code
        )

    assert adjusted("hvac_installation", "B15_17") == Decimal("0.45")
    assert adjusted("hvac_installation", "B13_15") == p_accept
    assert adjusted("hvac_service", "B15_17") == p_accept


@pytest.mark.parametrize(
    ("block_start", "expected"), [("13:00", "accepted"), ("15:00", "customer_declined")]
)
def test_install_offers_from_three_pm_are_declined_under_a_zero_multiplier(
    tmp_path, block_start, expected
):
    config = _config(**_ACCEPT_ALWAYS, **{"acceptance.install_late_window_multiplier": 0})
    conn, cfg, controller = _build(tmp_path, config=config, name="install_late.db")
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id, customer_id, location_id = _call_with_fixed_lead(
        conn, controller, catalog, clock.phoenix_instant(2026, 10, 1, 8, 59),
    )
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=2, service_date=START_DATE, block_start=block_start, technician_id=1,
    )
    assert _ok(body)["outcome"] == expected


def test_acceptance_probability_formula_is_monotone_nonincreasing_and_floor_is_respected():
    base = Decimal("1")
    floor = Decimal("0")
    for decay in (Decimal("0.15"), Decimal("0.50")):
        leads = [Decimal(x) for x in (0, 1, 2, 5, 10, 30)]
        values = [booking._acceptance_probability(base, decay, lead, floor) for lead in leads]
        assert values == sorted(values, reverse=True)
        assert all(Decimal(0) <= v <= Decimal(1) for v in values)
        assert values[0] == base

    high_floor = Decimal("0.25")
    far_out = booking._acceptance_probability(base, Decimal("0.50"), Decimal("1000"), high_floor)
    assert far_out == high_floor


def _raw_acceptance(base: Decimal, decay: Decimal, lead_days: Decimal) -> Decimal:
    with localcontext() as decctx:
        decctx.prec = 50
        return base * (-decay * lead_days).exp()


def test_acceptance_probability_default_config_sweep_stays_bounded_and_monotone():
    config = _config()
    base_by_urgency = config.resolved["acceptance.base_probability_by_urgency"]
    decay_by_urgency = config.resolved["acceptance.lead_time_decay_by_urgency"]
    floor_by_urgency = config.resolved["acceptance.floor_probability_by_urgency"]

    lead_days_grid = (
        [Decimal("0"), Decimal("0.25"), Decimal("0.5")]
        + [Decimal(n) for n in range(1, 31)]
        + [Decimal(45), Decimal(90), Decimal(365), Decimal(100000)]
    )

    for urgency_class in ("standard", "urgent"):
        base = base_by_urgency[urgency_class]
        decay = decay_by_urgency[urgency_class]
        floor = floor_by_urgency[urgency_class]
        values = [
            booking._acceptance_probability(base, decay, lead_days, floor)
            for lead_days in lead_days_grid
        ]
        raw_values = [_raw_acceptance(base, decay, lead_days) for lead_days in lead_days_grid]

        assert values[0] == base
        assert values[-1] == floor

        above_floor_seen = False
        for prev, curr, raw_curr in zip(values, values[1:], raw_values[1:], strict=False):
            if raw_curr > floor:
                assert curr < prev, f"{urgency_class}: {curr} >= {prev} before the floor is reached"
                above_floor_seen = True
            else:
                assert curr <= prev, f"{urgency_class}: {curr} > {prev} along increasing lead_days"
        assert above_floor_seen, f"{urgency_class}: sweep never left the pre-floor region"

        for value in values:
            assert isinstance(value, Decimal)
            assert value.is_finite()
            assert floor <= value <= base
            assert Decimal("0") <= value <= Decimal("1")


def test_cancellation_arrival_window_overnight_case_matches_spec_example():
    now = clock.phoenix_instant(2026, 10, 1, 18, 30)
    scheduled_start = clock.phoenix_instant(2026, 10, 2, 7, 0)
    upper_bound = scheduled_start - 60 * 60_000

    window = booking._cancellation_arrival_window(now, upper_bound)
    assert window.total == 29
    assert booking._nth_cancellation_arrival(window, 0) == clock.phoenix_instant(2026, 10, 1, 18, 31)
    assert booking._nth_cancellation_arrival(window, 28) == clock.phoenix_instant(2026, 10, 1, 18, 59)


def test_cancellation_arrival_window_same_day_empty_case_matches_spec_example():
    now = clock.phoenix_instant(2026, 10, 1, 14, 55)
    scheduled_start = clock.phoenix_instant(2026, 10, 1, 15, 0)
    upper_bound = scheduled_start - 60 * 60_000

    window = booking._cancellation_arrival_window(now, upper_bound)
    assert window.total == 0


def test_cancellation_arrival_window_multi_day_index_mapping():
    now = clock.phoenix_instant(2026, 10, 1, 9, 0)
    scheduled_start = clock.phoenix_instant(2026, 10, 4, 9, 0)
    upper_bound = scheduled_start - 60 * 60_000

    window = booking._cancellation_arrival_window(now, upper_bound)
    assert window.total == 599 + 720 + 720 + 61

    expected = {
        0: clock.phoenix_instant(2026, 10, 1, 9, 1),
        598: clock.phoenix_instant(2026, 10, 1, 18, 59),
        599: clock.phoenix_instant(2026, 10, 2, 7, 0),
        1318: clock.phoenix_instant(2026, 10, 2, 18, 59),
        1319: clock.phoenix_instant(2026, 10, 3, 7, 0),
        2038: clock.phoenix_instant(2026, 10, 3, 18, 59),
        2039: clock.phoenix_instant(2026, 10, 4, 7, 0),
        2099: clock.phoenix_instant(2026, 10, 4, 8, 0),
    }
    for index, instant in expected.items():
        assert booking._nth_cancellation_arrival(window, index) == instant


DET04_SERVICE_DATE = "2026-10-02"
DET04_DAY_ONE = "2026-10-01"
DET04_SEED = 20
DET04_TECHNICIAN_BY_SERVICE = {
    "hvac service": (1, 1),
    "hvac installation": (2, 2),
    "plumbing": (3, 3),
    "electrical": (4, 4),
}
DET04_CANCELLATION = {
    "cancellations.base_probability_by_segment": {
        "residential": 0.5, "commercial": 0.5, "apartment": 0.5,
    },
    "cancellations.lead_days_coefficient": 0,
    "cancellations.storm_multiplier": 1,
    "cancellations.urgent_multiplier": 1,
}
DET04_D_BLOCKS = ("13:00", "11:00", "15:00")
DET04_UNCOVERED_BLOCK = "17:00"
DET04_NON_ON_CALL_TECHNICIAN_ID = 2
DET04_B_DRAW_KINDS = (
    "offer_acceptance", "complexity", "cancellation_exists", "travel_noise",
    "start_variation", "work_duration", "invoice_amount", "material_percentage",
)
DET04_CONFIG = {
    **DET04_CANCELLATION,
    "demand.level_start": 20,
    "finance.starting_cash": 50_000_000,
    "run.end_date": DET04_SERVICE_DATE,
}


def _register_and_book(
    controller, cfg, *, call_id, catalog, technician_id, prefix,
    service_date=DET04_SERVICE_DATE, block_start="09:00", job_type_id=1,
):
    customer_id = _ok(json.loads(crm.create_customer_command(
        controller.services, f"{prefix}-customer", cfg, call_id=call_id,
        name="Fixture Caller", segment="residential",
        contacts=[{"contact_type": "phone", "value": "4805551234"}],
    )))["customer_id"]
    location_id = _ok(json.loads(crm.create_location_command(
        controller.services, f"{prefix}-location", cfg, call_id=call_id,
        customer_id=customer_id, address=catalog["formatted_address"], artifact=ARTIFACT,
    )))["location_id"]
    _, body = _book(
        controller, cfg, request_id=f"{prefix}-book",
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=job_type_id, service_date=service_date, block_start=block_start,
        technician_id=technician_id,
    )
    return customer_id, location_id, _ok(body)


class _Det04Adapter(ScriptedNoOpAdapter):

    def __init__(self, handle_a, b_origin=None, b_rejects=False, d_origin=None, d_block=None):
        super().__init__()
        self.d_origin = d_origin
        self.d_block = d_block
        self.d = None
        self.d_attempts = 0
        self.preconditions = {}
        self.controller = None
        self.handle_a = handle_a
        self.b_rejects = b_rejects
        self.b_origin = b_origin
        self.a_origin = None
        self.a_cancellation_seen = False
        self.c_slot = None
        self.b = None
        self.interleavings = {}

    def send(self, prompt):
        self.prompts.append(prompt)
        if prompt.kind.value != "call":
            return AdapterResponse(text="", terminal_action_taken=False)
        controller = self.controller
        payload = prompt.payload
        call_id = int(payload["call_id"])
        origin = origin_keys.of_call(controller.conn, call_id)
        if "appointment_id" in payload:
            if self.a_origin is not None and origin == f"x:{self.a_origin}":
                self.a_cancellation_seen = True
            _ok(json.loads(appointments.acknowledge_cancellation_command(
                controller.services, f"ack-{call_id}", controller.config,
                call_id=call_id, artifact=controller.artifact,
            )))
            return AdapterResponse(text="", terminal_action_taken=True)
        service = str(payload["requested_service"]).casefold()
        supported = service in DET04_TECHNICIAN_BY_SERVICE
        today = clock.service_date_for_ms(prompt.ts_ms)
        if origin == self.a_origin:
            return AdapterResponse(text="", terminal_action_taken=False)
        if self.a_origin is None and supported and today == DET04_DAY_ONE:
            self.a_origin = origin
            return self._handle_a(call_id, payload)
        if self._is_b(origin, supported, today):
            self.b_origin = origin
            if self.b_rejects:
                self.b = {"call_id": call_id}
                _ok(json.loads(calls.reject_command(
                    controller.services, f"b-{call_id}-reject", controller.config,
                    call_id=call_id,
                )))
            else:
                self._book_b(call_id, payload, service)
            return AdapterResponse(text="", terminal_action_taken=True)
        if self._is_d(origin, supported, today):
            technician_id, job_type_id = DET04_TECHNICIAN_BY_SERVICE[service]
            block_start = self.d_block or DET04_D_BLOCKS[self.d_attempts % len(DET04_D_BLOCKS)]
            self.d_attempts += 1
            booked = self._register_book(
                call_id, payload, technician_id=technician_id, job_type_id=job_type_id,
                service_date=DET04_SERVICE_DATE, block_start=block_start, prefix=f"d-{call_id}",
            )
            if self.d_origin is not None or self._cancels(booked):
                self.d_origin = origin
                self.d_block = block_start
                self.d = booked
            return AdapterResponse(text="", terminal_action_taken=True)
        if (
            self.handle_a == "creates_entities" and self.c_slot is None and supported
            and today == DET04_DAY_ONE and not self.a_cancellation_seen
        ):
            self.c_slot = (4, DET04_SERVICE_DATE, "11:00")
            self._register_book(call_id, payload, technician_id=4, job_type_id=4,
                                service_date=DET04_SERVICE_DATE, block_start="11:00",
                                prefix=f"c-{call_id}")
            return AdapterResponse(text="", terminal_action_taken=True)
        _ok(json.loads(calls.reject_command(
            controller.services, f"reject-{call_id}", controller.config, call_id=call_id,
        )))
        return AdapterResponse(text="", terminal_action_taken=True)

    def _cancels(self, booked):
        if booked["outcome"] != "accepted":
            return False
        row = self.controller.conn.execute(
            "SELECT cancel_exists FROM _cancellation_schedule WHERE appointment_id = ?",
            (booked["appointment_id"],),
        ).fetchone()
        return bool(row["cancel_exists"])

    def _is_d(self, origin, supported, today):
        if self.b is None or self.d is not None or self.b_rejects:
            return False
        if self.d_origin is not None:
            return origin == self.d_origin
        return supported and today == DET04_DAY_ONE

    def _is_b(self, origin, supported, today):
        if self.b is not None:
            return False
        if self.b_origin is not None:
            return origin == self.b_origin
        return self.a_cancellation_seen and supported and today == DET04_DAY_ONE

    def _handle_a(self, call_id, payload):
        controller = self.controller
        if self.handle_a == "untouched":
            return AdapterResponse(text="", terminal_action_taken=False)
        if self.handle_a == "rejects":
            _ok(json.loads(calls.reject_command(
                controller.services, f"reject-{call_id}", controller.config, call_id=call_id,
            )))
            return AdapterResponse(text="", terminal_action_taken=True)
        booked = self._register_book(
            call_id, payload, technician_id=4, job_type_id=4, service_date=DET04_DAY_ONE,
            block_start="15:00", prefix=f"a-{call_id}",
        )
        self.preconditions["a_outcome"] = booked["outcome"]
        return AdapterResponse(text="", terminal_action_taken=True)

    def _register_book(self, call_id, payload, *, technician_id, job_type_id, service_date,
                       block_start, prefix):
        controller = self.controller
        customer_id = _ok(json.loads(crm.create_customer_command(
            controller.services, f"{prefix}-customer", controller.config, call_id=call_id,
            name=str(payload["caller"]), segment=str(payload["customer_type"]),
            contacts=[{"contact_type": "phone", "value": str(payload["phone"])}],
        )))["customer_id"]
        location_id = _ok(json.loads(crm.create_location_command(
            controller.services, f"{prefix}-location", controller.config, call_id=call_id,
            customer_id=customer_id, address=str(payload["address"]), artifact=ARTIFACT,
        )))["location_id"]
        _, body = _book(
            controller, controller.config, request_id=f"{prefix}-book", call_id=call_id,
            customer_id=customer_id, location_id=location_id, job_type_id=job_type_id,
            service_date=service_date, block_start=block_start, technician_id=technician_id,
            artifact=ARTIFACT,
        )
        return body["result"] if body["ok"] else {"outcome": body["error"]["code"]}

    def _interleave_before_booking_b(self, call_id, customer_id, location_id):
        controller = self.controller
        conn = controller.conn
        audit_before = audit_count(conn)
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        agent = sql_gate.open_agent_session(db_path)
        self.interleavings["sql_rows"] = len(
            sql_gate.execute_gated(agent, "SELECT id FROM calls").fetchall()
        )
        agent.close()
        spec = OPERATIONS_BY_NAME["calls.list"]
        self.interleavings["read_rows"] = len(ops_read.execute_view_list(
            conn, controller.config, spec, ops_read.validate_operation_args(spec, {}),
        )[0])
        _technician_id, date_, _block = self.c_slot
        _, uncovered = _book(
            controller, controller.config, request_id=f"uncovered-{call_id}", call_id=call_id,
            customer_id=customer_id, location_id=location_id, job_type_id=4,
            service_date=date_, block_start=DET04_UNCOVERED_BLOCK,
            technician_id=DET04_NON_ON_CALL_TECHNICIAN_ID, artifact=ARTIFACT,
        )
        self.interleavings["uncovered"] = (uncovered.get("error") or {"code": "booked"})["code"]
        self.interleavings["audit_delta"] = audit_count(conn) - audit_before

    def _book_b(self, call_id, payload, service):
        controller = self.controller
        technician_id, job_type_id = DET04_TECHNICIAN_BY_SERVICE[service]
        customer_id = _ok(json.loads(crm.create_customer_command(
            controller.services, f"b-{call_id}-customer", controller.config, call_id=call_id,
            name=str(payload["caller"]), segment=str(payload["customer_type"]),
            contacts=[{"contact_type": "phone", "value": str(payload["phone"])}],
        )))["customer_id"]
        if self.handle_a == "creates_entities":
            malformed = json.loads(crm.create_location_command(
                controller.services, f"b-{call_id}-bad-location", controller.config,
                call_id=call_id, customer_id=customer_id, address="nowhere at all",
                artifact=ARTIFACT,
            ))
            self.interleavings["malformed"] = (malformed.get("error") or {"code": "created"})["code"]
        location_id = _ok(json.loads(crm.create_location_command(
            controller.services, f"b-{call_id}-location", controller.config, call_id=call_id,
            customer_id=customer_id, address=str(payload["address"]), artifact=ARTIFACT,
        )))["location_id"]
        if self.handle_a == "creates_entities":
            self._interleave_before_booking_b(call_id, customer_id, location_id)
        args = {
            "call_id": call_id, "customer_id": customer_id, "location_id": location_id,
            "job_type_id": job_type_id, "service_date": DET04_SERVICE_DATE,
            "block_start": "09:00", "technician_id": technician_id, "artifact": ARTIFACT,
        }
        raw, body = _book(controller, controller.config, request_id=f"b-{call_id}-book", **args)
        outcome = body["result"] if body["ok"] else {"outcome": body["error"]["code"]}
        self.b = {"call_id": call_id, "customer_id": customer_id, "location_id": location_id,
                  **outcome}
        if self.handle_a == "creates_entities":
            audit_before = audit_count(controller.conn)
            replay, _ = _book(
                controller, controller.config, request_id=f"b-{call_id}-book", **args,
            )
            self.interleavings["replay_identical"] = replay == raw
            self.interleavings["replay_audit_delta"] = audit_count(controller.conn) - audit_before


def _det04_b_audit(conn, origin):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT stream_name, entity_type, entity_id, scope, event_kind, draw_index "
            "FROM _random_audit WHERE scope = ? OR scope LIKE ? ORDER BY seq",
            (origin, f"{origin}|%"),
        )
    ]


def _det04_rejected_b_run(tmp_path, name, handle_a, b_origin=None):
    config = _config(**DET04_CONFIG)
    conn = _init(tmp_path / name, config, seed=DET04_SEED)
    register_conn(conn)
    adapter = _Det04Adapter(handle_a, b_origin, b_rejects=True)
    controller = Controller(conn, config, adapter, artifact_dir=GEO_ARTIFACT_DIR)
    adapter.controller = controller
    assert controller.run() is RunStatus.COMPLETED, "DET-04 precondition: the run completes"
    assert adapter.b is not None, "DET-04 precondition: call B arrived and was handled"
    return {
        "origin": adapter.b_origin,
        "call_id": adapter.b["call_id"],
        "audit": _det04_b_audit(conn, adapter.b_origin),
        "cancellation_seen": adapter.a_cancellation_seen,
    }


def _row_tuple(row):
    return None if row is None else tuple(row)


def _det04_precondition_failures(status, adapter):
    checks = [
        (status is RunStatus.COMPLETED, f"the run completes (status {status})"),
        (
            adapter.handle_a != "creates_entities"
            or adapter.preconditions.get("a_outcome") == "accepted",
            f"call A's booking is accepted (got {adapter.preconditions.get('a_outcome')})",
        ),
        (
            adapter.handle_a != "creates_entities" or adapter.a_cancellation_seen,
            "call A's cancellation call arrives before call B",
        ),
        (adapter.b is not None, "call B arrives and is booked"),
        (
            adapter.b is None or adapter.b.get("outcome") == "accepted",
            f"call B's booking is accepted (got {adapter.b and adapter.b.get('outcome')})",
        ),
        (
            adapter.handle_a != "creates_entities"
            or adapter.interleavings.get("uncovered") == "state_illegal",
            "booking B into an off-hours window its technician does not cover fails "
            f"(got {adapter.interleavings.get('uncovered')})",
        ),
        (
            adapter.d is not None and adapter.d.get("outcome") == "accepted",
            "a later call D is booked and accepted",
        ),
    ]
    return [f"DET-04 precondition failed: {message}" for ok, message in checks if not ok]


def _require_det04_preconditions(controller, adapter):
    failures = _det04_precondition_failures(controller.run(), adapter)
    assert not failures, failures


def _det04_run(tmp_path, name, handle_a, b_origin=None, d=(None, None)):
    config = _config(**DET04_CONFIG)
    conn = _init(tmp_path / name, config, seed=DET04_SEED)
    register_conn(conn)
    adapter = _Det04Adapter(handle_a, b_origin, d_origin=d[0], d_block=d[1])
    controller = Controller(conn, config, adapter, artifact_dir=GEO_ARTIFACT_DIR)
    adapter.controller = controller
    _require_det04_preconditions(controller, adapter)
    b = adapter.b
    origin = adapter.b_origin
    observed = {
        "acceptance": conn.execute(
            "SELECT acceptance_result FROM _call_hidden WHERE call_id = ?", (b["call_id"],)
        ).fetchone()[0],
        "blocks": b["blocks"],
        "complexity": conn.execute(
            "SELECT complexity_micros FROM _job_outcomes WHERE job_id = ?", (b["job_id"],)
        ).fetchone()[0],
        "cancellation": _row_tuple(conn.execute(
            "SELECT cancel_exists, arrival_at FROM _cancellation_schedule "
            "WHERE appointment_id = ?", (b["appointment_id"],),
        ).fetchone()),
        "execution": [
            (row["event_type"], row["occurred_at"])
            for row in conn.execute(
                "SELECT event_type, occurred_at FROM _appointment_events "
                "WHERE appointment_id = ? ORDER BY occurred_at, event_type",
                (b["appointment_id"],),
            )
        ],
        "appointment": _row_tuple(conn.execute(
            "SELECT status, actual_start, actual_end, lateness_minutes FROM _appointments "
            "WHERE id = ?", (b["appointment_id"],),
        ).fetchone()),
        "finance": _row_tuple(conn.execute(
            "SELECT subtotal_cents, material_percent_basis_points FROM _invoices "
            "WHERE job_id = ?", (b["job_id"],),
        ).fetchone()),
        "audit": [
            tuple(row)
            for row in conn.execute(
                "SELECT stream_name, entity_type, entity_id, scope, event_kind, draw_index "
                "FROM _random_audit WHERE scope = ? OR scope LIKE ? ORDER BY seq",
                (origin, f"{origin}|%"),
            )
        ],
    }
    observed["d_cancellation"] = _row_tuple(conn.execute(
        "SELECT cancel_exists, arrival_at FROM _cancellation_schedule WHERE appointment_id = ?",
        (adapter.d["appointment_id"],),
    ).fetchone())
    observed["d_audit"] = _det04_b_audit(conn, adapter.d_origin)
    ids = (b["call_id"], b["customer_id"], b["location_id"], b["job_id"], b["appointment_id"])
    return {
        "origin": origin, "d": (adapter.d_origin, adapter.d_block), "ids": ids,
        "observed": observed,
        "adapter": adapter,
    }


@pytest.fixture(scope="module")
def det04_runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("det04")
    created = _det04_run(base, "created.db", "creates_entities")
    return {
        "creates_entities": created,
        "rejects": _det04_run(
            base, "rejects.db", "rejects", created["origin"], created["d"],
        ),
        "untouched": _det04_run(
            base, "untouched.db", "untouched", created["origin"], created["d"],
        ),
    }


def test_det04_entities_created_for_a_shift_no_draw_of_a_rejected_b(tmp_path):
    created = _det04_rejected_b_run(tmp_path, "b_rejected_created.db", "creates_entities")
    untouched = _det04_rejected_b_run(
        tmp_path, "b_rejected_untouched.db", "untouched", created["origin"],
    )

    assert created["cancellation_seen"]
    assert untouched["origin"] == created["origin"]
    assert created["call_id"] > untouched["call_id"]
    assert created["audit"] == untouched["audit"] == []


def test_det04_scenario_preconditions_hold_in_every_world(det04_runs):
    for name, world in det04_runs.items():
        adapter = world["adapter"]
        assert _det04_precondition_failures(RunStatus.COMPLETED, adapter) == [], name


def test_det04_a_creates_entities_and_a_cancellation_call_before_b(det04_runs):
    created = det04_runs["creates_entities"]
    adapter = created["adapter"]

    assert adapter.a_cancellation_seen
    assert adapter.c_slot is not None
    assert created["origin"].startswith(f"n:{DET04_DAY_ONE}:")
    for other in ("rejects", "untouched"):
        other_ids = det04_runs[other]["ids"]
        assert det04_runs[other]["origin"] == created["origin"]
        assert all(mine > theirs for mine, theirs in zip(created["ids"], other_ids, strict=True))


def test_det04_b_keyed_outcomes_and_realized_execution_are_identical(det04_runs):
    created = det04_runs["creates_entities"]["observed"]
    rejects = det04_runs["rejects"]["observed"]
    untouched = det04_runs["untouched"]["observed"]

    assert created["appointment"][0] == "completed"
    assert sorted(row[4] for row in created["audit"]) == sorted(DET04_B_DRAW_KINDS)
    assert created["d_cancellation"][0] == 1
    assert "cancellation_arrival" in {row[4] for row in created["d_audit"]}
    for key in created:
        assert created[key] == rejects[key] == untouched[key], key


def test_det04_interleavings_change_nothing_and_a_replay_returns_stored_bytes(det04_runs):
    interleavings = det04_runs["creates_entities"]["adapter"].interleavings

    assert interleavings["sql_rows"] > 0
    assert interleavings["read_rows"] > 0
    assert interleavings["uncovered"] == "state_illegal"
    assert interleavings["malformed"] == "invalid_argument"
    assert interleavings["audit_delta"] == 0
    assert interleavings["replay_identical"] is True
    assert interleavings["replay_audit_delta"] == 0


def test_cancellation_scheduled_at_a_minute_aligned_instant_within_bounds(tmp_path):
    config = _config(**_ACCEPT_ALWAYS, **_CANCEL_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    appointment_id = result["appointment_id"]

    row = conn.execute(
        "SELECT cancel_exists, arrival_at, event_id FROM _cancellation_schedule "
        "WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()
    assert row["cancel_exists"] == 1
    assert row["arrival_at"] is not None
    assert row["event_id"] is not None

    booking_now = clock.phoenix_instant(2026, 10, 1, 6, 45)
    scheduled_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    cutoff_ms = cfg.get_int("cancellations.cancellation_cutoff_minutes") * 60_000
    arrival_at = row["arrival_at"]
    assert arrival_at % 60_000 == 0
    assert arrival_at > booking_now
    assert arrival_at <= scheduled_start - cutoff_ms
    local = clock.phoenix_local(arrival_at)
    assert 420 <= local.hour * 60 + local.minute < 1140

    event = conn.execute(
        "SELECT kind, priority, entity_type, entity_id, payload, status, scheduled_at_ms "
        "FROM _events WHERE event_id = ?",
        (row["event_id"],),
    ).fetchone()
    assert event["kind"] == EventKind.CALL_INVOCATION.value
    assert event["priority"] == 910
    assert event["entity_type"] == "appointment"
    assert event["entity_id"] == appointment_id
    assert json.loads(event["payload"]) == {"appointment_id": appointment_id}
    assert event["status"] == "pending"
    assert event["scheduled_at_ms"] == arrival_at

    appointment_origin = origin_keys.of_appointment(conn, appointment_id)
    assert _audit_rows(conn, stream_name="cancellation", scope=appointment_origin) == [
        ("cancellation", "appointment_origin", 1, appointment_origin, "cancellation_exists", 0),
        ("cancellation", "appointment_origin", 1, appointment_origin, "cancellation_arrival", 1),
    ]


def test_cancellation_never_exists_writes_null_row_and_takes_one_draw(tmp_path):
    config = _config(**_ACCEPT_ALWAYS, **_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    appointment_id = result["appointment_id"]

    row = conn.execute(
        "SELECT cancel_exists, arrival_at, event_id FROM _cancellation_schedule "
        "WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()
    assert (row["cancel_exists"], row["arrival_at"], row["event_id"]) == (0, None, None)

    appointment_origin = origin_keys.of_appointment(conn, appointment_id)
    assert _audit_rows(conn, stream_name="cancellation", scope=appointment_origin) == [
        ("cancellation", "appointment_origin", 1, appointment_origin, "cancellation_exists", 0),
    ]


def test_cancellation_empty_window_writes_no_arrival_and_takes_no_arrival_draw(tmp_path):
    config = _config(**_ACCEPT_ALWAYS, **_CANCEL_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _set_clock(conn, clock.phoenix_instant(2026, 10, 1, 18, 59), START_DATE)
    tomorrow = "2026-10-02"

    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=tomorrow, block_start="07:00", technician_id=1,
    )
    result = _ok(body)
    appointment_id = result["appointment_id"]

    row = conn.execute(
        "SELECT cancel_exists, arrival_at, event_id FROM _cancellation_schedule "
        "WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()
    assert (row["cancel_exists"], row["arrival_at"], row["event_id"]) == (1, None, None)

    appointment_origin = origin_keys.of_appointment(conn, appointment_id)
    assert _audit_rows(conn, stream_name="cancellation", scope=appointment_origin) == [
        ("cancellation", "appointment_origin", 1, appointment_origin, "cancellation_exists", 0),
    ]
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_type = 'appointment' "
        "AND entity_id = ?",
        (EventKind.CALL_INVOCATION.value, appointment_id),
    ).fetchone()["c"] == 0


def test_cancellation_same_day_empty_window_writes_no_arrival_and_takes_no_arrival_draw(tmp_path):
    config = _config(**_ACCEPT_ALWAYS, **_CANCEL_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _set_clock(conn, clock.phoenix_instant(2026, 10, 1, 14, 55), START_DATE)

    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="15:00", technician_id=1,
    )
    result = _ok(body)
    appointment_id = result["appointment_id"]

    row = conn.execute(
        "SELECT cancel_exists, arrival_at, event_id FROM _cancellation_schedule "
        "WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()
    assert (row["cancel_exists"], row["arrival_at"], row["event_id"]) == (1, None, None)

    appointment_origin = origin_keys.of_appointment(conn, appointment_id)
    assert _audit_rows(conn, stream_name="cancellation", scope=appointment_origin) == [
        ("cancellation", "appointment_origin", 1, appointment_origin, "cancellation_exists", 0),
    ]
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_type = 'appointment' "
        "AND entity_id = ?",
        (EventKind.CALL_INVOCATION.value, appointment_id),
    ).fetchone()["c"] == 0


def _p_cancel_config(**overrides):
    keys = {
        "cancellations.base_probability_by_segment": {
            "residential": 0.001, "commercial": 0.001, "apartment": 0.001,
        },
        "cancellations.urgent_multiplier": 1.0,
        "cancellations.lead_days_coefficient": 0.0,
        "cancellations.storm_multiplier": 1.0,
    }
    keys.update(overrides)
    return _config(**_ACCEPT_ALWAYS, **keys)


_STORM_TEST_DATE = "2026-12-01"


def _insert_storm_day(conn, now, zone_id, local_date):
    conn.execute(
        "INSERT INTO _zone_weather_daily (zone_id, local_date, high_f_tenths, low_f_tenths, "
        "precipitation_mm_tenths, wind_mph_tenths, storm_flag, storm_severity_milli, "
        "condition_code, created_at) VALUES (?, ?, 900, 700, 500, 300, 1, 500, 'storm', ?)",
        (zone_id, local_date, now),
    )


def _cancel_exists_for(
    tmp_path, name, config, *, urgent=0, storm=False, service_date=START_DATE,
):
    conn, cfg, controller = _build(tmp_path, config=config, name=name)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    if storm:
        _insert_storm_day(conn, now, catalog["effective_zone_id"], service_date)
    call_id = _registered_call(
        conn, controller, customer_id=customer_id, location_id=location_id, urgent=urgent,
    )
    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=service_date, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    row = conn.execute(
        "SELECT cancel_exists FROM _cancellation_schedule WHERE appointment_id = ?",
        (result["appointment_id"],),
    ).fetchone()
    conn.close()
    return row["cancel_exists"]


def test_cancellation_exists_flips_with_urgent_multiplier_holding_base_fixed(tmp_path):
    config = _p_cancel_config(**{"cancellations.urgent_multiplier": 1000.0})
    assert _cancel_exists_for(tmp_path, "cancel_urgent_off.db", config, urgent=0) == 0
    assert _cancel_exists_for(tmp_path, "cancel_urgent_on.db", config, urgent=1) == 1


def test_cancellation_exists_flips_with_storm_flag_holding_base_fixed(tmp_path):
    config = _p_cancel_config(**{"cancellations.storm_multiplier": 1000.0})
    assert _cancel_exists_for(
        tmp_path, "cancel_storm_off.db", config, storm=False, service_date=_STORM_TEST_DATE,
    ) == 0
    assert _cancel_exists_for(
        tmp_path, "cancel_storm_on.db", config, storm=True, service_date=_STORM_TEST_DATE,
    ) == 1


def test_idempotent_replay_returns_stored_response_and_conflicting_args_conflict(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    request_id = _next_request_id("book_idempotent")
    kwargs = {
        "call_id": call_id, "customer_id": customer_id, "location_id": location_id,
        "job_type_id": 1, "service_date": START_DATE, "block_start": "09:00",
        "technician_id": 1,
    }
    first_response, first_body = _book(controller, cfg, request_id=request_id, **kwargs)
    _ok(first_body)

    audit_after_first = audit_count(conn)
    jobs_after_first = _table_count(conn, "_jobs")

    second_response, _ = _book(controller, cfg, request_id=request_id, **kwargs)
    assert second_response == first_response
    assert audit_count(conn) == audit_after_first
    assert _table_count(conn, "_jobs") == jobs_after_first

    other_customer_id = _insert_customer(conn, now)
    other_location_id = _insert_location(conn, now, other_customer_id, catalog)
    other_call_id = _registered_call(
        conn, controller, customer_id=other_customer_id, location_id=other_location_id,
    )
    conflicting = dict(kwargs)
    conflicting.update(
        call_id=other_call_id, customer_id=other_customer_id, location_id=other_location_id,
    )
    _, conflict_body = _book(controller, cfg, request_id=request_id, **conflicting)
    err = _err(conflict_body)
    assert err["code"] == ErrorCode.REQUEST_ID_CONFLICT.value


def test_callback_call_sets_recall_for_job_id_ordinary_call_gets_null(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)

    completed_at = clock.phoenix_instant(2026, 9, 20, 15, 0)
    prior_call = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (?, '2026-09-19', 'new_job', 'booked', 'Fixture Caller', '4805551234', "
        "'HVAC service', 0, ?)",
        (clock.phoenix_instant(2026, 9, 19, 10, 0), now),
    )
    prior_job = conn.execute(
        "INSERT INTO _jobs (job_number, call_id, customer_id, location_id, job_type_id, "
        "requested_service_name, status, priority, no_charge, summary, created_at, "
        "completed_at, modified_at) "
        "VALUES ('9001', ?, ?, ?, 1, 'HVAC service', 'completed', 'normal', 0, '', ?, ?, ?)",
        (int(prior_call.lastrowid), customer_id, location_id, now, completed_at, now),
    )
    job_id = int(prior_job.lastrowid)

    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        channel="callback", customer_id=customer_id, location_id=location_id,
        related_job_id=job_id,
    )
    callback_call_id = _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)

    _, body = _book(
        controller, cfg,
        call_id=callback_call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    booked_job = conn.execute(
        "SELECT recall_for_job_id FROM _jobs WHERE id = ?", (result["job_id"],)
    ).fetchone()
    assert booked_job["recall_for_job_id"] == job_id

    other_customer_id = _insert_customer(conn, now)
    other_location_id = _insert_location(conn, now, other_customer_id, catalog)
    ordinary_call_id = _registered_call(
        conn, controller, customer_id=other_customer_id, location_id=other_location_id,
    )
    _, body2 = _book(
        controller, cfg,
        call_id=ordinary_call_id, customer_id=other_customer_id, location_id=other_location_id,
        job_type_id=1, service_date=START_DATE, block_start="11:00", technician_id=1,
    )
    result2 = _ok(body2)
    booked_job2 = conn.execute(
        "SELECT recall_for_job_id FROM _jobs WHERE id = ?", (result2["job_id"],)
    ).fetchone()
    assert booked_job2["recall_for_job_id"] is None


def test_complexity_draw_within_configured_range_and_keyed_on_job_and_creation_date(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _, body = _book(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=2, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(body)
    job_id = result["job_id"]

    dist = cfg.resolved["quality.complexity_distribution"]["hvac_installation"]
    lo = int(dist["min"] * Decimal(10**6))
    hi = int(dist["max"] * Decimal(10**6))
    outcome = conn.execute(
        "SELECT complexity_micros FROM _job_outcomes WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert lo <= outcome["complexity_micros"] <= hi

    creation_date = clock.service_date_for_ms(_now_ms(conn))
    complexity_scope = f"{origin_keys.of_job(conn, job_id)}|{creation_date}"
    assert _audit_rows(conn, stream_name="job_quality", scope=complexity_scope) == [
        ("job_quality", "job_origin", 1, complexity_scope, "complexity", 0),
    ]


def test_cancellation_call_and_stale_call_rejected(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog_a, catalog_b = _usable_catalog_rows(conn, 2)
    _open_invocation(conn, now)

    cancellation_call_id = _insert_bare_call(conn, now, call_kind="cancellation")
    _, cancel_body = _book(
        controller, cfg,
        call_id=cancellation_call_id, customer_id=1, location_id=1,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    cancel_err = _err(cancel_body)
    assert cancel_err["details"]["reason"] == "cancellation_call"

    customer_a = _insert_customer(conn, now)
    location_a = _insert_location(conn, now, customer_a, catalog_a)
    call_a_id = _registered_call(conn, controller, customer_id=customer_a, location_id=location_a)

    customer_b = _insert_customer(conn, now)
    location_b = _insert_location(conn, now, customer_b, catalog_b)
    _registered_call(conn, controller, customer_id=customer_b, location_id=location_b)

    _, stale_body = _book(
        controller, cfg,
        call_id=call_a_id, customer_id=customer_a, location_id=location_a,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    stale_err = _err(stale_body)
    assert stale_err["details"]["reason"] == "call_not_active"


def test_non_canonical_service_date_spellings_never_bypass_a_block_lock(tmp_path):
    config = _config(**_ACCEPT_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    target = date(2026, 10, 2)
    canonical = target.isoformat()
    basic_spelling = target.strftime("%Y%m%d")
    week = target.isocalendar()
    week_spelling = f"{week.year}-W{week.week:02d}-{week.weekday}"
    assert date.fromisoformat(basic_spelling) == target
    assert date.fromisoformat(week_spelling) == target

    conn.execute(
        "INSERT INTO _block_locks (service_date, block_code, block_start, locked_at) "
        "VALUES (?, 'B09_11', ?, ?)",
        (canonical, clock.phoenix_instant(2026, 10, 2, 9, 0), now),
    )

    defaults = {
        "call_id": call_id, "customer_id": customer_id, "location_id": location_id,
        "job_type_id": 1, "block_start": "09:00", "technician_id": 1,
    }

    _, locked_body = _book(controller, cfg, service_date=canonical, **defaults)
    locked_err = _err(locked_body)
    assert locked_err["details"]["reason"] == "locked_appointment"

    for spelling in (basic_spelling, week_spelling):
        before_world = _world_snapshot(conn)
        before_rng = _rng_snapshot(conn)
        before_terminal = _call_terminal_snapshot(conn, call_id)
        _, body = _book(controller, cfg, service_date=spelling, **defaults)
        err = _err(body)
        assert err["code"] == ErrorCode.STATE_ILLEGAL.value
        assert err["details"]["reason"] == "invalid_service_date"
        assert _world_snapshot(conn) == before_world
        assert _rng_snapshot(conn) == before_rng
        assert _call_terminal_snapshot(conn, call_id) == before_terminal


def _arrived_call_with_origin(conn, controller, catalog, origin_key, minute):
    now = _now_ms(conn)
    arrival_at = clock.phoenix_instant(2026, 10, 1, 9, minute)
    pending_id = _insert_pending(
        conn, now, arrival_at, channel="new_caller", zone_id=4,
        address_catalog_id=catalog["id"], origin_key=origin_key,
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


def _pending_event_order(controller, event_ids):
    conn = controller.conn
    events = controller.services.events
    listed = [
        int(row["event_id"])
        for row in events.pending_in_order()
        if int(row["event_id"]) in event_ids
    ]
    placeholders = ", ".join("?" for _ in event_ids)
    conn.execute(
        f"UPDATE _events SET status = 'cancelled' WHERE status = 'pending' "
        f"AND event_id NOT IN ({placeholders})",
        tuple(event_ids),
    )
    applied = []
    while (row := events.next_pending()) is not None:
        applied.append(int(row["event_id"]))
        conn.execute("UPDATE _events SET status = 'applied' WHERE event_id = ?", (row["event_id"],))
    assert applied == listed, (applied, listed)
    return applied


def test_a_cancellation_scheduled_at_booking_ties_after_a_later_new_call_by_origin(tmp_path):
    config = _config(**_ACCEPT_ALWAYS, **_CANCEL_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=config)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, _now_ms(conn))
    booking_origin = "n:2026-10-01:4:3"
    call_id = _arrived_call_with_origin(conn, controller, catalog, booking_origin, 0)
    _, _, booked = _register_and_book(
        controller, cfg, call_id=call_id, catalog=catalog, technician_id=1, prefix="tie",
    )
    cancellation = conn.execute(
        "SELECT e.event_id, e.scheduled_at_ms, e.order_key FROM _cancellation_schedule s "
        "JOIN _events e ON e.event_id = s.event_id WHERE s.appointment_id = ?",
        (booked["appointment_id"],),
    ).fetchone()
    assert cancellation["order_key"] == f"x:{booking_origin}"

    later_origin = "n:2026-10-02:4:0"
    db.begin(conn)
    controller.services.rng.enter_txn()
    try:
        later_event = controller.services.events.schedule(
            EventKind.CALL_INVOCATION, int(cancellation["scheduled_at_ms"]),
            entity_type="pending_call", entity_id=999, order_key=later_origin,
        )
    finally:
        controller.services.rng.exit_txn()
    db.commit(conn)

    assert later_event > int(cancellation["event_id"])
    assert _pending_event_order(controller, {later_event, int(cancellation["event_id"])}) == [
        later_event, int(cancellation["event_id"]),
    ]


def test_same_instant_appointment_events_apply_in_origin_order_not_booking_order(tmp_path):
    config = _config(**_ACCEPT_ALWAYS, **_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=config)
    catalog_late, catalog_early = _usable_catalog_rows(conn, 2)
    _open_invocation(conn, _now_ms(conn))
    late_call = _arrived_call_with_origin(conn, controller, catalog_late, "n:2026-10-01:4:9", 0)
    _, _, late = _register_and_book(
        controller, cfg, call_id=late_call, catalog=catalog_late, technician_id=1, prefix="late",
    )
    early_call = _arrived_call_with_origin(conn, controller, catalog_early, "n:2026-10-01:4:1", 1)
    _, _, early = _register_and_book(
        controller, cfg, call_id=early_call, catalog=catalog_early, technician_id=3,
        prefix="early",
    )
    guards = {
        int(row["entity_id"]): row
        for row in conn.execute(
            "SELECT event_id, entity_id, scheduled_at_ms, priority, order_key FROM _events "
            "WHERE kind = ? AND status = 'pending'",
            (EventKind.COMPANY_NO_SHOW.value,),
        )
    }
    late_guard = guards[late["appointment_id"]]
    early_guard = guards[early["appointment_id"]]

    assert late_guard["scheduled_at_ms"] == early_guard["scheduled_at_ms"]
    assert late_guard["priority"] == early_guard["priority"]
    assert int(late_guard["event_id"]) < int(early_guard["event_id"])
    assert (late_guard["order_key"], early_guard["order_key"]) == (
        "n:2026-10-01:4:9", "n:2026-10-01:4:1",
    )
    assert _pending_event_order(
        controller, {int(late_guard["event_id"]), int(early_guard["event_id"])}
    ) == [int(early_guard["event_id"]), int(late_guard["event_id"])]
