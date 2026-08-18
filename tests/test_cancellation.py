from __future__ import annotations

import json
from itertools import count

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    geo_artifact_sha256,
    register_conn,
)
from csr_bench import appointments, booking, calls, clock, execution
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.enums import EventKind, RunStatus
from csr_bench.errors import ErrorCode
from csr_bench.run_init import initialize_run, open_run_db

SEED = 17
START_DATE = "2026-10-01"
END_DATE = "2026-10-05"

_ZONE_WEIGHTS = [0] * 10

_ACCEPT_ALWAYS = {
    "acceptance.base_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
    "acceptance.floor_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
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
        "demand.zone_weights": _ZONE_WEIGHTS,
        "run.end_date": END_DATE,
        **_ACCEPT_ALWAYS,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _build(tmp_path, *, config=None, name="run.db"):
    cfg = config if config is not None else _config()
    db_path = tmp_path / name
    initialize_run(db_path, master_seed=SEED, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    return conn, cfg, controller


def _now_ms(conn) -> int:
    return conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]


def _open_invocation(conn, now, invocation_type="call") -> int:
    cur = conn.execute(
        "INSERT INTO _invocations (type, ts_ms, sequence, status) "
        "VALUES (?, ?, "
        "(SELECT COALESCE(MAX(sequence), 0) + 1 FROM _invocations), 'active')",
        (invocation_type, now),
    )
    return int(cur.lastrowid)


def _usable_catalog_rows(conn, count_):
    rows = conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT ?",
        (count_,),
    ).fetchall()
    assert len(rows) == count_
    return rows


def _insert_customer(conn, now, segment="residential", propensity_micros=0):
    cur = conn.execute(
        "INSERT INTO _customers (name, segment, status, created_at, modified_at) "
        "VALUES ('Fixture Customer', ?, 'active', ?, ?)",
        (segment, now, now),
    )
    customer_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO _customer_contacts (customer_id, contact_type, value, do_not_text, "
        "memo, created_at) VALUES (?, 'phone', '4805551234', 0, '', ?)",
        (customer_id, now),
    )
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
    from csr_bench import db
    from csr_bench.events import Events

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
    return clock.phoenix_instant(2026, 10, 1, 8, 0) + next(_arrival_seq) * 60_000


def _registered_call(
    conn, controller, *, customer_id, location_id, arrival_at=None, **pending_overrides
):
    now = _now_ms(conn)
    if arrival_at is None:
        arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at, customer_id=customer_id, location_id=location_id,
        **pending_overrides,
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


def _next_request_id(prefix: str) -> str:
    return f"cancellation.{prefix}.{next(_request_seq)}"


def _ok(body: dict) -> dict:
    assert body["ok"] is True, body
    return body["result"]


def _err(body: dict) -> dict:
    assert body["ok"] is False, body
    return body["error"]


def _book_via_call(
    conn, controller, cfg, *, customer_id, location_id, job_type_id=1,
    service_date=START_DATE, block_start="09:00", technician_id=1,
    requested_category_id=None,
):
    now = _now_ms(conn)
    invocation_id = _open_invocation(conn, now)
    pending_overrides = {}
    if requested_category_id is not None:
        pending_overrides["requested_category_id"] = requested_category_id
    call_id = _registered_call(
        conn, controller, customer_id=customer_id, location_id=location_id,
        **pending_overrides,
    )
    response = booking.book_command(
        controller.services, _next_request_id("book"), cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=job_type_id, service_date=service_date, block_start=block_start,
        technician_id=technician_id,
    )
    body = json.loads(response)
    result = _ok(body)
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )
    return call_id, result


def _cancellation_schedule_row(conn, appointment_id):
    return conn.execute(
        "SELECT cancel_exists, arrival_at, event_id FROM _cancellation_schedule "
        "WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()


def _apply_call_invocation_event(controller, event_id):
    handler = controller.handlers[EventKind.CALL_INVOCATION]
    response = controller.events.apply(event_id, handler, services=controller.services)
    body = json.loads(response)
    assert body["ok"] is True, body
    return body["result"]


def _event_row(conn, event_id):
    return conn.execute("SELECT * FROM _events WHERE event_id = ?", (event_id,)).fetchone()


def _latest_applied_call_at(conn):
    row = conn.execute(
        "SELECT MAX(applied_at_ms) AS latest FROM _events WHERE kind = ? AND status = 'applied'",
        (EventKind.CALL_INVOCATION.value,),
    ).fetchone()
    return row["latest"] if row["latest"] is not None else _now_ms(conn)


def _manufacture_call(
    conn, *, call_kind, customer_id, location_id, address_catalog_id,
    requested_service_name="HVAC service", target_appointment_id=None, target_job_id=None,
    status="active", segment="residential", buffer_ms=60_000,
):
    from csr_bench import clock as clock_module
    from csr_bench import db

    now = max(_now_ms(conn), _latest_applied_call_at(conn) + buffer_ms)
    db.begin(conn)
    try:
        clock_module.advance(conn, now)
        event_cur = conn.execute(
            "INSERT INTO _events (kind, scheduled_at_ms, priority, entity_type, entity_id, "
            "payload_version, payload, status, created_at_ms, applied_at_ms) "
            "VALUES (?, ?, 910, 'appointment', ?, 1, NULL, 'applied', ?, ?)",
            (EventKind.CALL_INVOCATION.value, now, target_appointment_id or 0, now, now),
        )
        fake_event_id = int(event_cur.lastrowid)
        call_cur = conn.execute(
            "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
            "caller_phone, segment, customer_id, location_id, requested_service_name, urgent, "
            "related_job_id, target_appointment_id, target_job_id, address_catalog_id, created_at) "
            "VALUES (?, ?, ?, ?, 'Fixture Caller', '4805551234', ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?)",
            (
                now, START_DATE, call_kind, status, segment, customer_id, location_id,
                requested_service_name, target_appointment_id, target_job_id,
                address_catalog_id, now,
            ),
        )
        call_id = int(call_cur.lastrowid)
        conn.execute(
            "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, "
            "prospect_id, arrival_event_id, created_at) VALUES (?, ?, 2, NULL, ?, ?)",
            (call_id, segment, fake_event_id, now),
        )
    except BaseException:
        db.rollback(conn)
        raise
    db.commit(conn)
    return call_id, now


def _world_snapshot(conn):
    tables = (
        "_customers", "_customer_repeat_state", "_locations",
        "_calls", "_call_hidden", "_jobs", "_appointments", "_appointment_blocks",
        "_appointment_assignments", "_appointment_events", "_technician_block_holds",
        "_job_outcomes", "_cancellation_schedule", "_ledger_entries", "_notifications",
        "_events", "_technician_locations", "_technician_movements",
    )
    return {table: conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] for table in tables}


def _rng_snapshot(conn):
    rows = conn.execute(
        "SELECT stream_name, seed, counter FROM _rng_state ORDER BY stream_name"
    ).fetchall()
    return [tuple(row) for row in rows]


def _step_until(controller, predicate, max_steps=20000):
    for _ in range(max_steps):
        if predicate():
            return
        if not controller.step():
            break
    assert predicate(), "condition never became true within the step budget"


def _appointment_row(conn, appointment_id):
    return conn.execute(
        "SELECT * FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()


def _job_row(conn, job_id):
    return conn.execute("SELECT * FROM _jobs WHERE id = ?", (job_id,)).fetchone()


def _hold_count(conn, appointment_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM _technician_block_holds WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()["c"]


class _AckAdapter(ScriptedNoOpAdapter):

    def __init__(self, controller_holder):
        super().__init__()
        self.controller_holder = controller_holder
        self.acknowledged_call_ids = []

    def send(self, prompt):
        if prompt.kind.value == "call" and "appointment_id" in prompt.payload:
            controller = self.controller_holder["controller"]
            call_id = prompt.payload["call_id"]
            response = appointments.acknowledge_cancellation_command(
                controller.services, _next_request_id("ack"), controller.config,
                call_id=call_id, artifact=controller.artifact,
            )
            body = json.loads(response)
            assert body["ok"] is True, body
            self.acknowledged_call_ids.append(call_id)
        return super().send(prompt)


def _two_catalog_rows(conn):
    rows = _usable_catalog_rows(conn, 2)
    return rows[0], rows[1]


def _setup_alive_appointment(tmp_path, *, config_overrides=None, name="run.db"):
    cfg = _config(**{**_CANCEL_ALWAYS, **(config_overrides or {})})
    conn, cfg, controller = _build(tmp_path, config=cfg, name=name)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]
    schedule = _cancellation_schedule_row(conn, appointment_id)
    assert schedule["cancel_exists"] == 1
    assert schedule["event_id"] is not None
    return conn, cfg, controller, appointment_id, job_id, customer_id, location_id, schedule


def test_cancellation_arrival_creates_call_with_prompt_payload_fields(tmp_path):
    conn, cfg, controller, appointment_id, job_id, customer_id, location_id, schedule = (
        _setup_alive_appointment(tmp_path)
    )
    event_id = schedule["event_id"]
    arrival_at = schedule["arrival_at"]

    applied = _apply_call_invocation_event(controller, event_id)
    assert applied["status"] == "applied"

    call = conn.execute(
        "SELECT c.*, h.arrival_event_id FROM _calls c JOIN _call_hidden h ON h.call_id = c.id "
        "WHERE h.arrival_event_id = ?",
        (event_id,),
    ).fetchone()
    assert call is not None
    assert call["call_kind"] == "cancellation"
    assert call["status"] == "active"
    assert call["target_appointment_id"] == appointment_id
    assert call["target_job_id"] == job_id
    assert call["customer_id"] == customer_id
    assert call["location_id"] == location_id
    assert call["segment"] is None
    assert call["service_street"] is None
    assert call["urgent"] == 0
    assert call["related_job_id"] is None
    assert call["arrived_at"] == arrival_at

    event_row = _event_row(conn, event_id)
    payload = calls.prompt_payload(conn, event_row, arrival_at)
    assert payload["call_id"] == call["id"]
    assert payload["appointment_id"] == appointment_id
    assert payload["customer_id"] == customer_id
    assert payload["location_id"] == location_id
    assert payload["time"] == clock.to_iso8601(arrival_at)
    appt = _appointment_row(conn, appointment_id)
    assert payload["scheduled"] == clock.to_iso8601(int(appt["scheduled_start"]))
    assert payload["scheduled_end"] == clock.to_iso8601(int(appt["scheduled_end"]))
    assert "requested_service" not in payload
    assert "urgent" not in payload
    assert "address" not in payload


def test_cancellation_arrival_resolves_hidden_category_for_job_type_other(tmp_path):
    conn, cfg, controller = _build(tmp_path, config=_config(**_CANCEL_ALWAYS))
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
        job_type_id=5,
    )
    appointment_id = result["appointment_id"]
    schedule = _cancellation_schedule_row(conn, appointment_id)
    event_id = schedule["event_id"]

    applied = _apply_call_invocation_event(controller, event_id)
    assert applied["status"] == "applied"

    cancellation = conn.execute(
        "SELECT c.*, h.requested_category_id FROM _calls c "
        "JOIN _call_hidden h ON h.call_id = c.id WHERE h.arrival_event_id = ?",
        (event_id,),
    ).fetchone()
    assert cancellation is not None
    assert cancellation["call_kind"] == "cancellation"
    assert cancellation["requested_category_id"] == 2


def test_cancellation_arrival_hidden_category_matches_requested_service_name_for_mismatched_job_type(
    tmp_path,
):
    conn, cfg, controller = _build(tmp_path, config=_config(**_CANCEL_ALWAYS))
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    roofing_category_id = int(
        conn.execute(
            "SELECT id FROM _service_catalog WHERE name = 'Roofing'"
        ).fetchone()["id"]
    )
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
        job_type_id=3, requested_category_id=roofing_category_id,
    )
    appointment_id = result["appointment_id"]
    schedule = _cancellation_schedule_row(conn, appointment_id)
    event_id = schedule["event_id"]

    applied = _apply_call_invocation_event(controller, event_id)
    assert applied["status"] == "applied"

    cancellation = conn.execute(
        "SELECT c.*, h.requested_category_id FROM _calls c "
        "JOIN _call_hidden h ON h.call_id = c.id WHERE h.arrival_event_id = ?",
        (event_id,),
    ).fetchone()
    assert cancellation["requested_category_id"] == roofing_category_id
    catalog_name = conn.execute(
        "SELECT name FROM _service_catalog WHERE id = ?", (roofing_category_id,)
    ).fetchone()["name"]
    assert catalog_name == "Roofing"
    assert cancellation["requested_service_name"] == catalog_name


@pytest.mark.parametrize(
    "dead_status", ["cancelled", "completed", "failed", "no_show", "in_progress"],
)
def test_dead_event_drop_leaves_no_world_effect(tmp_path, dead_status):
    conn, cfg, controller, appointment_id, job_id, customer_id, location_id, schedule = (
        _setup_alive_appointment(tmp_path, name=f"run-{dead_status}.db")
    )
    event_id = schedule["event_id"]

    conn.execute(
        "UPDATE _appointments SET status = ? WHERE id = ?", (dead_status, appointment_id)
    )
    before_world = _world_snapshot(conn)
    before_rng = _rng_snapshot(conn)
    before_appt = dict(_appointment_row(conn, appointment_id))

    applied = _apply_call_invocation_event(controller, event_id)
    assert applied["status"] == "cancelled"

    event_row = _event_row(conn, event_id)
    assert event_row["status"] == "cancelled"

    assert _world_snapshot(conn) == before_world
    assert dict(_appointment_row(conn, appointment_id)) == before_appt
    assert _rng_snapshot(conn) == before_rng


def test_cancellation_arrival_does_not_touch_a_colliding_pending_call(tmp_path):
    conn, cfg, controller, appointment_id, job_id, customer_id, location_id, schedule = (
        _setup_alive_appointment(tmp_path)
    )
    event_id = schedule["event_id"]

    now = _now_ms(conn)
    colliding_id = conn.execute(
        "INSERT INTO _pending_calls (service_date, zone_id, arrival_at, call_kind, channel, "
        "segment, requested_category_id, urgent, caller_name, caller_phone, created_at) "
        "VALUES (?, NULL, ?, 'new_job', 'new_caller', 'residential', 2, 0, "
        "'Should Not Be Touched', '5551230000', ?)",
        (START_DATE, now, now),
    ).lastrowid
    assert int(colliding_id) == appointment_id

    applied = _apply_call_invocation_event(controller, event_id)
    assert applied["status"] == "applied"

    pending = conn.execute(
        "SELECT caller_name FROM _pending_calls WHERE id = ?", (colliding_id,)
    ).fetchone()
    assert pending is not None
    assert pending["caller_name"] == "Should Not Be Touched"

    call = conn.execute(
        "SELECT c.* FROM _calls c JOIN _call_hidden h ON h.call_id = c.id "
        "WHERE h.arrival_event_id = ?",
        (event_id,),
    ).fetchone()
    assert call["target_appointment_id"] == appointment_id
    assert call["call_kind"] == "cancellation"


@pytest.mark.parametrize(
    "dead_status, expected_disposition", [(None, "applied"), ("cancelled", "cancelled")]
)
def test_idempotent_replay_of_call_invocation_event_apply_is_byte_identical(
    tmp_path, dead_status, expected_disposition
):
    conn, cfg, controller, appointment_id, job_id, customer_id, location_id, schedule = (
        _setup_alive_appointment(tmp_path, name=f"run-replay-{dead_status}.db")
    )
    event_id = schedule["event_id"]
    handler = controller.handlers[EventKind.CALL_INVOCATION]

    if dead_status is not None:
        conn.execute(
            "UPDATE _appointments SET status = ? WHERE id = ?", (dead_status, appointment_id)
        )

    first = controller.events.apply(event_id, handler, services=controller.services)
    first_body = json.loads(first)
    assert first_body["ok"] is True, first_body
    assert first_body["result"]["status"] == expected_disposition

    before_second_calls = conn.execute("SELECT COUNT(*) c FROM _calls").fetchone()["c"]
    before_second_hidden = conn.execute("SELECT COUNT(*) c FROM _call_hidden").fetchone()["c"]
    before_second_events = conn.execute("SELECT COUNT(*) c FROM _events").fetchone()["c"]

    second = controller.events.apply(event_id, handler, services=controller.services)
    assert first == second

    assert conn.execute("SELECT COUNT(*) c FROM _calls").fetchone()["c"] == before_second_calls
    assert (
        conn.execute("SELECT COUNT(*) c FROM _call_hidden").fetchone()["c"]
        == before_second_hidden
    )
    assert conn.execute("SELECT COUNT(*) c FROM _events").fetchone()["c"] == before_second_events


def test_acknowledge_cancellation_cancels_frees_and_sets_job_status(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    technician_id = execution._active_technician_id(conn, appointment_id)
    assignment_id = conn.execute(
        "SELECT id FROM _appointment_assignments WHERE appointment_id = ? AND status = 'active'",
        (appointment_id,),
    ).fetchone()["id"]
    assert _hold_count(conn, appointment_id) > 0

    cancellation_call_id, now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    invocation_id = _open_invocation(conn, now)

    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=cancellation_call_id, artifact=controller.artifact,
    )
    body = json.loads(response)
    result = _ok(body)
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )

    assert result["call_id"] == cancellation_call_id
    assert result["outcome"] == "acknowledged"
    assert result["appointment_id"] == appointment_id
    assert len(result["released_blocks"]) == 1
    assert result["message"] == (
        "Cancellation acknowledged. The appointment is cancelled and its blocks are released."
    )

    appt = _appointment_row(conn, appointment_id)
    assert appt["status"] == "cancelled"
    assert appt["cancelled_at"] == now

    job = _job_row(conn, job_id)
    assert job["status"] == "cancelled"
    assert job["outcome"] == "cancelled_by_customer"

    assignment = conn.execute(
        "SELECT status, unassigned_at FROM _appointment_assignments WHERE id = ?",
        (assignment_id,),
    ).fetchone()
    assert assignment["status"] == "cancelled"
    assert assignment["unassigned_at"] == now

    assert _hold_count(conn, appointment_id) == 0

    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (cancellation_call_id,)).fetchone()
    assert call["status"] == "acknowledged"
    assert call["terminal_reason"] == "cancellation_acknowledged"
    assert call["terminal_at"] == now

    notification = conn.execute(
        "SELECT * FROM _notifications WHERE notification_type = 'customer_cancellation_acknowledged'"
    ).fetchone()
    assert notification is not None
    assert notification["entity_type"] == "appointment"
    assert notification["entity_id"] == appointment_id

    assert technician_id is not None


def test_freed_block_is_immediately_rebookable(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    cancellation_call_id, now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    invocation_id = _open_invocation(conn, now)
    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=cancellation_call_id, artifact=controller.artifact,
    )
    _ok(json.loads(response))
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )

    second_customer_id = _insert_customer(conn, _now_ms(conn))
    second_location_id = _insert_location(conn, _now_ms(conn), second_customer_id, catalog)
    _, rebooked = _book_via_call(
        conn, controller, cfg, customer_id=second_customer_id, location_id=second_location_id,
    )
    assert rebooked["appointment_id"] != appointment_id
    assert _hold_count(conn, rebooked["appointment_id"]) > 0


def test_acknowledge_cancellation_has_zero_propensity_competitor_and_rng_deltas(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    cancellation_call_id, now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    invocation_id = _open_invocation(conn, now)

    repeat_before = dict(
        conn.execute(
            "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    )
    prospect_before = conn.execute("SELECT COUNT(*) c FROM _prospect_state").fetchone()["c"]
    rng_before = _rng_snapshot(conn)

    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=cancellation_call_id, artifact=controller.artifact,
    )
    _ok(json.loads(response))
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )

    repeat_after = dict(
        conn.execute(
            "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    )
    assert repeat_after == repeat_before
    assert conn.execute(
        "SELECT COUNT(*) c FROM _prospect_state"
    ).fetchone()["c"] == prospect_before
    assert _rng_snapshot(conn) == rng_before


def test_acknowledge_cancellation_replay_is_byte_identical(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    cancellation_call_id, now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    invocation_id = _open_invocation(conn, now)
    request_id = _next_request_id("ack")

    first = appointments.acknowledge_cancellation_command(
        controller.services, request_id, cfg,
        call_id=cancellation_call_id, artifact=controller.artifact,
    )
    second = appointments.acknowledge_cancellation_command(
        controller.services, request_id, cfg,
        call_id=cancellation_call_id, artifact=controller.artifact,
    )
    assert first == second
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )


def test_acknowledge_cancellation_rejects_a_new_job_call(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)

    invocation_id = _open_invocation(conn, now)
    new_job_call_id = _registered_call(
        conn, controller, customer_id=customer_id, location_id=location_id
    )

    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=new_job_call_id, artifact=controller.artifact,
    )
    error = _err(json.loads(response))
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "new_job_call"
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )


def test_acknowledge_cancellation_rejects_a_stale_call(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    stale_call_id, stale_now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    invocation_id = _open_invocation(conn, _now_ms(conn))
    superseding_call_id = _registered_call(
        conn, controller, customer_id=customer_id, location_id=location_id,
        arrival_at=stale_now + 60_000,
    )

    def _applied_at(call_id):
        return conn.execute(
            "SELECT e.applied_at_ms FROM _events e "
            "JOIN _call_hidden h ON h.arrival_event_id = e.event_id "
            "WHERE h.call_id = ?",
            (call_id,),
        ).fetchone()["applied_at_ms"]

    assert _applied_at(superseding_call_id) > _applied_at(stale_call_id)

    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=stale_call_id, artifact=controller.artifact,
    )
    error = _err(json.loads(response))
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "call_not_active"
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )


def test_acknowledge_cancellation_rejects_an_already_terminal_call(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    terminal_call_id, now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id, status="acknowledged",
    )
    invocation_id = _open_invocation(conn, now)

    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=terminal_call_id, artifact=controller.artifact,
    )
    error = _err(json.loads(response))
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "call_not_active"
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )


def test_post_dispatch_same_day_cancellation_replans_the_technician(tmp_path):
    cfg = _config(**_CANCEL_NEVER)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    first_catalog, second_catalog = _two_catalog_rows(conn)

    first_customer = _insert_customer(conn, now)
    first_location = _insert_location(conn, now, first_customer, first_catalog)
    _, first_result = _book_via_call(
        conn, controller, cfg, customer_id=first_customer, location_id=first_location,
        block_start="09:00",
    )
    first_appointment_id = first_result["appointment_id"]
    first_job_id = first_result["job_id"]

    second_customer = _insert_customer(conn, _now_ms(conn))
    second_location = _insert_location(conn, _now_ms(conn), second_customer, second_catalog)
    _, second_result = _book_via_call(
        conn, controller, cfg, customer_id=second_customer, location_id=second_location,
        block_start="13:00",
    )
    second_appointment_id = second_result["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
            (EventKind.APPOINTMENT_DISPATCH.value, first_appointment_id),
        ).fetchone() is not None,
    )
    assert _appointment_row(conn, first_appointment_id)["status"] == "scheduled"
    technician_id = execution._active_technician_id(conn, first_appointment_id)
    assert technician_id is not None

    cancellation_call_id, now = _manufacture_call(
        conn, call_kind="cancellation", customer_id=first_customer, location_id=first_location,
        address_catalog_id=first_catalog["id"], target_appointment_id=first_appointment_id,
        target_job_id=first_job_id,
    )
    invocation_id = _open_invocation(conn, now)
    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg,
        call_id=cancellation_call_id, artifact=controller.artifact,
    )
    _ok(json.loads(response))
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )

    assert conn.execute(
        "SELECT status FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_DISPATCH.value, first_appointment_id),
    ).fetchone()["status"] == "cancelled"

    replanned = conn.execute(
        "SELECT status, payload FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_DISPATCH.value, second_appointment_id),
    ).fetchone()
    assert replanned is not None
    assert replanned["status"] == "pending"
    assert json.loads(replanned["payload"])["technician_id"] == technician_id

    status = controller.run()
    assert status is RunStatus.COMPLETED
    assert _appointment_row(conn, second_appointment_id)["status"] == "completed"
    assert _appointment_row(conn, first_appointment_id)["status"] == "cancelled"

    day = conn.execute(
        "SELECT unresolved_execution_count FROM _day_state WHERE service_date = ?", (START_DATE,)
    ).fetchone()
    assert day["unresolved_execution_count"] == 0


def test_day_loop_end_to_end_cancellation(tmp_path):
    cfg = _config(**_CANCEL_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    holder = {"controller": controller}
    adapter = _AckAdapter(holder)
    controller.adapter = adapter

    status = controller.run()
    assert status is RunStatus.COMPLETED
    assert adapter.acknowledged_call_ids

    appt = _appointment_row(conn, appointment_id)
    assert appt["status"] == "cancelled"
    job = _job_row(conn, job_id)
    assert job["status"] == "cancelled"
    assert job["outcome"] == "cancelled_by_customer"
    assert _hold_count(conn, appointment_id) == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM _notifications WHERE notification_type = "
        "'customer_cancellation_acknowledged'"
    ).fetchone()["c"] == 1
