from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from itertools import count

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    audit_count,
    geo_artifact_sha256,
    register_conn,
)
from csr_bench import booking, calls, clock, crm, db, geo_reader
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.enums import EventKind
from csr_bench.errors import ErrorCode
from csr_bench.events import Events
from csr_bench.run_init import initialize_run, open_run_db

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
        "INSERT INTO _invocations (type, ts_ms, sequence, status, attempt_count) "
        "VALUES (?, ?, "
        "(SELECT COALESCE(MAX(sequence), 0) + 1 FROM _invocations), 'active', 0)",
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
        "competitor_captured_at, updated_at) VALUES (?, ?, NULL, NULL, ?)",
        (customer_id, propensity_micros, now),
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

    occupant_catalog = _usable_catalog_rows(conn, 2)[1]
    occupant_id, occupant_customer_id, occupant_location_id = _fresh_registered_call(
        conn, controller, occupant_catalog,
    )
    _, occupant_body = _book(
        controller, cfg,
        call_id=occupant_id, customer_id=occupant_customer_id, location_id=occupant_location_id,
        job_type_id=1, service_date=START_DATE, block_start="11:00", technician_id=1,
    )
    _ok(occupant_body)

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
        "grid_violation", ErrorCode.STATE_ILLEGAL.value, job_type_id=2, block_start="15:00",
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
    assert after["_appointment_blocks"] == before["_appointment_blocks"] + 2
    assert after["_appointment_assignments"] == before["_appointment_assignments"] + 1
    assert after["_technician_block_holds"] == before["_technician_block_holds"] + 2
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
        {
            "start": clock.to_iso8601(clock.phoenix_instant(2026, 10, 1, 11, 0)),
            "end": clock.to_iso8601(clock.phoenix_instant(2026, 10, 1, 13, 0)),
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
    config = _config(
        **_DECLINE_ALWAYS,
        **{"competitors.attempt_probability": 1.0, "competitors.served_well_probability": 1.0},
    )
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
    competitor_rows = _audit_rows(conn, stream_name="competitor_leak", entity_id=call_id)
    acceptance_rows = _audit_rows(conn, stream_name="acceptance", entity_id=call_id)
    conn.close()
    return snapshot, competitor_rows, acceptance_rows, call_id


def test_decline_via_booking_equals_decline_via_reject(tmp_path):
    snap_booking, comp_booking, accept_booking, call_id_booking = _decline_via(
        tmp_path, "decline_via_booking.db", via_booking=True,
    )
    snap_reject, comp_reject, accept_reject, call_id_reject = _decline_via(
        tmp_path, "decline_via_reject.db", via_booking=False,
    )

    assert call_id_booking == call_id_reject
    assert comp_booking == comp_reject
    assert len(comp_booking) == 2
    assert accept_reject == []
    assert accept_booking == [
        ("acceptance", "call", call_id_booking, "", "offer_acceptance", 0),
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
            _audit_rows(conn, entity_id=call_id),
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


def test_booking_isolation_unrelated_activity_does_not_shift_the_outcome(tmp_path):
    config = _config()

    conn_a, cfg_a, controller_a = _build(tmp_path, config=config, name="det04_clean.db")
    now_a = _now_ms(conn_a)
    catalog_a1, catalog_a2 = _usable_catalog_rows(conn_a, 2)
    _open_invocation(conn_a, now_a)
    call_a_id, _, _ = _fresh_registered_call(conn_a, controller_a, catalog_a1)
    call_b_id, customer_b_id, location_b_id = _fresh_registered_call(conn_a, controller_a, catalog_a2)
    _, clean_body = _book(
        controller_a, cfg_a,
        call_id=call_b_id, customer_id=customer_b_id, location_id=location_b_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result_clean = _ok(clean_body)
    hidden_clean = conn_a.execute(
        "SELECT acceptance_result FROM _call_hidden WHERE call_id = ?", (call_b_id,)
    ).fetchone()["acceptance_result"]
    audit_clean = _audit_rows(conn_a, stream_name="acceptance", entity_id=call_b_id)
    conn_a.close()

    conn_b, cfg_b, controller_b = _build(tmp_path, config=config, name="det04_noisy.db")
    now_b = _now_ms(conn_b)
    catalog_b1, catalog_b2 = _usable_catalog_rows(conn_b, 2)
    _open_invocation(conn_b, now_b)
    call_a_id_2, _, _ = _fresh_registered_call(conn_b, controller_b, catalog_b1)
    assert call_a_id_2 == call_a_id

    reject_request_id = _next_request_id("det04_reject")
    _, reject_body = _reject(controller_b, cfg_b, call_id=call_a_id_2, request_id=reject_request_id)
    _ok(reject_body)
    _, replay_body = _reject(controller_b, cfg_b, call_id=call_a_id_2, request_id=reject_request_id)
    _ok(replay_body)

    call_b_id_2, customer_b_id_2, location_b_id_2 = _fresh_registered_call(
        conn_b, controller_b, catalog_b2,
    )
    assert call_b_id_2 == call_b_id

    _, failed_body = _book(
        controller_b, cfg_b,
        call_id=call_b_id_2, customer_id=customer_b_id_2, location_id=location_b_id_2,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=999999,
    )
    _err(failed_body)

    _, noisy_body = _book(
        controller_b, cfg_b,
        call_id=call_b_id_2, customer_id=customer_b_id_2, location_id=location_b_id_2,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result_noisy = _ok(noisy_body)
    hidden_noisy = conn_b.execute(
        "SELECT acceptance_result FROM _call_hidden WHERE call_id = ?", (call_b_id_2,)
    ).fetchone()["acceptance_result"]
    audit_noisy = _audit_rows(conn_b, stream_name="acceptance", entity_id=call_b_id_2)

    assert result_noisy == result_clean
    assert hidden_noisy == hidden_clean
    assert audit_noisy == audit_clean


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

    assert _audit_rows(conn, stream_name="cancellation", entity_id=appointment_id) == [
        ("cancellation", "appointment", appointment_id, "", "cancellation_exists", 0),
        ("cancellation", "appointment", appointment_id, "", "cancellation_arrival", 1),
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

    assert _audit_rows(conn, stream_name="cancellation", entity_id=appointment_id) == [
        ("cancellation", "appointment", appointment_id, "", "cancellation_exists", 0),
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

    assert _audit_rows(conn, stream_name="cancellation", entity_id=appointment_id) == [
        ("cancellation", "appointment", appointment_id, "", "cancellation_exists", 0),
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

    assert _audit_rows(conn, stream_name="cancellation", entity_id=appointment_id) == [
        ("cancellation", "appointment", appointment_id, "", "cancellation_exists", 0),
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
    assert _audit_rows(conn, stream_name="job_quality", entity_id=job_id) == [
        ("job_quality", "job", job_id, creation_date, "complexity", 0),
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
