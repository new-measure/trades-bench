from __future__ import annotations

import json
import sqlite3
from itertools import count

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import GEO_ARTIFACT_DIR, GEO_ARTIFACT_ID, geo_artifact_sha256, register_conn
from csr_bench import appointments, booking, calls, clock, control, db
from csr_bench._dump import dump
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.enums import EventKind
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.events import Events
from csr_bench.run_init import initialize_run, open_run_db
from csr_bench.txn import command, is_reserved, privileged_request_id

SEED = 17
START_DATE = "2026-10-01"
END_DATE = "2026-10-05"

_ZONE_WEIGHTS = [0] * 10
_ACCEPT_ALWAYS = {
    "acceptance.base_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
    "acceptance.floor_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
}
_DECLINE_ALWAYS = {
    "acceptance.base_probability_by_urgency": {"standard": 0.0, "urgent": 0.0},
    "acceptance.floor_probability_by_urgency": {"standard": 0.0, "urgent": 0.0},
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


def _new_caller_call(
    conn, controller, *, catalog, caller_name="Fixture Caller", caller_phone="4805551234",
):
    now = _now_ms(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        channel="new_caller", address_catalog_id=catalog["id"],
        caller_name=caller_name, caller_phone=caller_phone,
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


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


def _book_via_call(
    conn, controller, cfg, *, customer_id, location_id, job_type_id=1,
    service_date=START_DATE, block_start="09:00", technician_id=1,
):
    now = _now_ms(conn)
    invocation_id = _open_invocation(conn, now)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)
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


def _latest_applied_call_at(conn):
    row = conn.execute(
        "SELECT MAX(applied_at_ms) AS latest FROM _events WHERE kind = ? AND status = 'applied'",
        (EventKind.CALL_INVOCATION.value,),
    ).fetchone()
    return row["latest"] if row["latest"] is not None else _now_ms(conn)


def _manufacture_cancellation_call(
    conn, *, customer_id, location_id, address_catalog_id, target_appointment_id,
    target_job_id, requested_service_name="HVAC service", buffer_ms=60_000,
):
    now = max(_now_ms(conn), _latest_applied_call_at(conn) + buffer_ms)
    db.begin(conn)
    try:
        clock.advance(conn, now)
        event_cur = conn.execute(
            "INSERT INTO _events (kind, scheduled_at_ms, priority, entity_type, entity_id, "
            "payload_version, payload, status, created_at_ms, applied_at_ms) "
            "VALUES (?, ?, 910, 'appointment', ?, 1, NULL, 'applied', ?, ?)",
            (EventKind.CALL_INVOCATION.value, now, target_appointment_id, now, now),
        )
        fake_event_id = int(event_cur.lastrowid)
        call_cur = conn.execute(
            "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
            "caller_phone, segment, customer_id, location_id, requested_service_name, urgent, "
            "related_job_id, target_appointment_id, target_job_id, address_catalog_id, "
            "created_at) "
            "VALUES (?, ?, 'cancellation', 'active', 'Fixture Caller', '4805551234', "
            "'residential', ?, ?, ?, 0, NULL, ?, ?, ?, ?)",
            (
                now, START_DATE, customer_id, location_id, requested_service_name,
                target_appointment_id, target_job_id, address_catalog_id, now,
            ),
        )
        call_id = int(call_cur.lastrowid)
        conn.execute(
            "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, "
            "prospect_id, arrival_event_id, created_at) VALUES (?, 'residential', 2, NULL, ?, ?)",
            (call_id, fake_event_id, now),
        )
    except BaseException:
        db.rollback(conn)
        raise
    db.commit(conn)
    return call_id, now


def _hold_count(conn, appointment_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM _technician_block_holds WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()["c"]


def _appointment_row(conn, appointment_id):
    return conn.execute("SELECT * FROM _appointments WHERE id = ?", (appointment_id,)).fetchone()


def _job_row(conn, job_id):
    return conn.execute("SELECT * FROM _jobs WHERE id = ?", (job_id,)).fetchone()


def _next_request_id(prefix: str) -> str:
    return f"control.{prefix}.{next(_request_seq)}"


def _ok(body: dict) -> dict:
    assert body["ok"] is True, body
    return body["result"]


def _err(body: dict) -> dict:
    assert body["ok"] is False, body
    return body["error"]


def _call_row(conn, call_id):
    return conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()


def _attempt_rows(conn, call_id):
    return conn.execute(
        "SELECT attempt_number, prompted_at, outcome FROM _call_attempts "
        "WHERE call_id = ? ORDER BY attempt_number",
        (call_id,),
    ).fetchall()


def _record(services, cfg, *, call_id, attempt_number):
    response = control.record_call_attempt_op(
        services, cfg, call_id=call_id, attempt_number=attempt_number,
    )
    return response, json.loads(response)


def _mark_unanswered(services, cfg, *, call_id, artifact=None):
    response = control.mark_call_unanswered_op(
        services, cfg, call_id=call_id, artifact=artifact,
    )
    return response, json.loads(response)


def _ledger_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _ledger_entries").fetchone()["c"]


def _audit_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]


def _new_job_setup(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog)
    return conn, cfg, controller, call_id


def test_record_call_attempt_writes_incremental_rows_and_returns_numbers(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    frozen = _now_ms(conn)

    for expected_number in (1, 2, 3):
        _, body = _record(
            controller.services, cfg, call_id=call_id, attempt_number=expected_number,
        )
        result = _ok(body)
        assert result == {"call_id": call_id, "attempt_number": expected_number}

    rows = [dict(r) for r in _attempt_rows(conn, call_id)]
    assert rows == [
        {"attempt_number": 1, "prompted_at": frozen, "outcome": "incomplete"},
        {"attempt_number": 2, "prompted_at": frozen, "outcome": "incomplete"},
        {"attempt_number": 3, "prompted_at": frozen, "outcome": "incomplete"},
    ]


def test_three_frozen_clock_attempts_enable_mark_call_unanswered(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    frozen = _now_ms(conn)

    for n in (1, 2, 3):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)

    rows = _attempt_rows(conn, call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [
        (1, "incomplete"), (2, "incomplete"), (3, "incomplete"),
    ]
    assert {r["prompted_at"] for r in rows} == {frozen}

    _, body = _mark_unanswered(controller.services, cfg, call_id=call_id)
    _ok(body)
    assert _call_row(conn, call_id)["status"] == "unanswered"


def test_record_call_attempt_idempotent_replay(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)

    first, body1 = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body1)
    second, body2 = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    assert second == first
    assert len(_attempt_rows(conn, call_id)) == 1


def test_record_call_attempt_number_mismatch_is_explicit_and_unpoisoning(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=2)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "attempt_number_mismatch"
    assert err["details"]["expected"] == 1
    assert err["details"]["supplied"] == 2
    assert _attempt_rows(conn, call_id) == []

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body)
    assert len(_attempt_rows(conn, call_id)) == 1


def test_record_call_attempt_beyond_three_rejected_and_resolved_only_via_terminal_action(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    for n in (1, 2, 3):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)

    before = _attempt_rows(conn, call_id)
    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=4)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "three_attempt_limit"
    assert err["details"]["recorded"] == 3
    assert _attempt_rows(conn, call_id) == before

    _ok(json.loads(calls.reject_command(
        controller.services, _next_request_id("reject"), cfg, call_id=call_id,
    )))
    rows = _attempt_rows(conn, call_id)
    assert [r["outcome"] for r in rows] == [
        "incomplete", "incomplete", "incomplete", "resolved",
    ]
    assert rows[3]["attempt_number"] == 4


def test_record_call_attempt_invalid_argument_shapes(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)

    for bad_call_id in ("5", True, 5.0, None):
        _, body = _record(
            controller.services, cfg, call_id=bad_call_id, attempt_number=1,
        )
        assert _err(body)["code"] == "invalid_argument"

    for bad_number in ("1", True, 1.0, None):
        _, body = _record(
            controller.services, cfg, call_id=call_id, attempt_number=bad_number,
        )
        assert _err(body)["code"] == "invalid_argument"

    assert _attempt_rows(conn, call_id) == []


def test_malformed_arguments_leave_no_idempotency_rows_and_do_not_poison(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)

    _, body = _record(controller.services, cfg, call_id=str(call_id), attempt_number=1)
    assert _err(body)["code"] == "invalid_argument"
    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number="1")
    assert _err(body)["code"] == "invalid_argument"
    _, body = _mark_unanswered(controller.services, cfg, call_id=str(call_id))
    assert _err(body)["code"] == "invalid_argument"

    rows = conn.execute(
        "SELECT request_id FROM _idempotency WHERE request_id LIKE '__control__%'"
    ).fetchall()
    assert rows == []

    for n in (1, 2, 3):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)
    _, body = _mark_unanswered(controller.services, cfg, call_id=call_id)
    _ok(body)
    assert _call_row(conn, call_id)["status"] == "unanswered"


def test_record_call_attempt_missing_call_not_found(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    _, body = _record(controller.services, cfg, call_id=999999, attempt_number=1)
    assert _err(body)["code"] == "entity_not_found"


def test_record_call_attempt_rejects_a_stale_call(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog_a, catalog_b = _usable_catalog_rows(conn, 2)
    _open_invocation(conn, now)
    call_a = _new_caller_call(
        conn, controller, catalog=catalog_a, caller_name="A", caller_phone="4805550001"
    )
    _new_caller_call(conn, controller, catalog=catalog_b, caller_name="B", caller_phone="4805550002")

    before = _attempt_rows(conn, call_a)
    _, body = _record(controller.services, cfg, call_id=call_a, attempt_number=1)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"
    assert _attempt_rows(conn, call_a) == before


def test_record_call_attempt_rejects_a_terminal_call(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    _ok(json.loads(calls.reject_command(
        controller.services, _next_request_id("reject"), cfg, call_id=call_id,
    )))

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"


def test_record_call_attempt_rejects_when_active_invocation_is_not_a_call(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    conn.execute("UPDATE _invocations SET status = 'completed' WHERE status = 'active'")
    _open_invocation(conn, _now_ms(conn), invocation_type="opening")

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"


def test_mark_call_unanswered_premature_error_then_legal_finalization(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)

    _, body = _mark_unanswered(controller.services, cfg, call_id=call_id)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "three_attempt_limit"
    assert err["details"]["recorded"] == 0
    assert _call_row(conn, call_id)["status"] == "active"

    for n in (1, 2, 3):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)

    _, body = _mark_unanswered(controller.services, cfg, call_id=call_id)
    _ok(body)
    assert _call_row(conn, call_id)["status"] == "unanswered"


def test_mark_call_unanswered_requires_an_active_call(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    for n in (1, 2, 3):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)
    _ok(json.loads(calls.reject_command(
        controller.services, _next_request_id("reject"), cfg, call_id=call_id,
    )))

    _, body = _mark_unanswered(controller.services, cfg, call_id=call_id)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"


def test_mark_call_unanswered_idempotent_replay(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    for n in (1, 2, 3):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)

    first, body1 = _mark_unanswered(controller.services, cfg, call_id=call_id)
    _ok(body1)
    audit_after_first = _audit_count(conn)
    ledger_after_first = _ledger_count(conn)

    second, body2 = _mark_unanswered(controller.services, cfg, call_id=call_id)
    assert second == first
    assert _audit_count(conn) == audit_after_first
    assert _ledger_count(conn) == ledger_after_first
    rows = _attempt_rows(conn, call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [
        (1, "incomplete"), (2, "incomplete"), (3, "incomplete"),
    ]


def test_resolved_row_written_by_booking_accepted_after_attempt(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    _open_invocation(conn, now)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)
    frozen = _now_ms(conn)

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body)

    response = booking.book_command(
        controller.services, _next_request_id("book"), cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(json.loads(response))
    assert result["outcome"] == "accepted"

    rows = [dict(r) for r in _attempt_rows(conn, call_id)]
    assert rows == [
        {"attempt_number": 1, "prompted_at": frozen, "outcome": "incomplete"},
        {"attempt_number": 2, "prompted_at": frozen, "outcome": "resolved"},
    ]
    assert _call_row(conn, call_id)["status"] == "booked"


def test_resolved_row_written_by_reject_after_attempt(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    frozen = _now_ms(conn)

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body)
    _ok(json.loads(calls.reject_command(
        controller.services, _next_request_id("reject"), cfg, call_id=call_id,
    )))

    rows = [dict(r) for r in _attempt_rows(conn, call_id)]
    assert rows == [
        {"attempt_number": 1, "prompted_at": frozen, "outcome": "incomplete"},
        {"attempt_number": 2, "prompted_at": frozen, "outcome": "resolved"},
    ]
    assert _call_row(conn, call_id)["status"] == "rejected"


def test_resolved_row_written_by_declined_booking_after_attempt(tmp_path):
    cfg = _config(**_DECLINE_ALWAYS)
    conn, cfg, controller = _build(tmp_path, config=cfg)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    _open_invocation(conn, now)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body)

    response = booking.book_command(
        controller.services, _next_request_id("book"), cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="09:00", technician_id=1,
    )
    result = _ok(json.loads(response))
    assert result["outcome"] == "customer_declined"

    rows = _attempt_rows(conn, call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [
        (1, "incomplete"), (2, "resolved"),
    ]
    assert _call_row(conn, call_id)["status"] == "declined"


def test_resolved_row_written_by_acknowledged_cancellation_after_attempt(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    cancellation_call_id, manufactured_now = _manufacture_cancellation_call(
        conn, customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    _open_invocation(conn, manufactured_now)

    _, body = _record(
        controller.services, cfg, call_id=cancellation_call_id, attempt_number=1,
    )
    _ok(body)

    response = appointments.acknowledge_cancellation_command(
        controller.services, _next_request_id("ack"), cfg, call_id=cancellation_call_id,
    )
    result = _ok(json.loads(response))
    assert result["outcome"] == "acknowledged"

    rows = _attempt_rows(conn, cancellation_call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [
        (1, "incomplete"), (2, "resolved"),
    ]
    assert _call_row(conn, cancellation_call_id)["status"] == "acknowledged"


def test_clean_settle_writes_no_attempt_rows(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    _ok(json.loads(calls.reject_command(
        controller.services, _next_request_id("reject"), cfg, call_id=call_id,
    )))
    assert _attempt_rows(conn, call_id) == []


def test_failed_terminal_action_writes_no_resolved_row(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    _open_invocation(conn, now)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body)

    response = booking.book_command(
        controller.services, _next_request_id("book"), cfg,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        job_type_id=1, service_date=START_DATE, block_start="99:99", technician_id=1,
    )
    err = _err(json.loads(response))
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "grid_violation"

    rows = _attempt_rows(conn, call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [(1, "incomplete")]
    assert _call_row(conn, call_id)["status"] == "active"


def test_replay_of_settling_command_does_not_duplicate_resolved_row(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    request_id = _next_request_id("reject")

    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    _ok(body)

    first = calls.reject_command(controller.services, request_id, cfg, call_id=call_id)
    assert json.loads(first)["ok"] is True
    second = calls.reject_command(controller.services, request_id, cfg, call_id=call_id)
    assert second == first

    rows = _attempt_rows(conn, call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [
        (1, "incomplete"), (2, "resolved"),
    ]


def test_resolved_row_is_always_last_and_record_after_settlement_fails(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    first_attempt_bytes = None
    for n in (1, 2):
        response, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        _ok(body)
        if n == 1:
            first_attempt_bytes = response
    _ok(json.loads(calls.reject_command(
        controller.services, _next_request_id("reject"), cfg, call_id=call_id,
    )))

    rows = _attempt_rows(conn, call_id)
    assert [(r["attempt_number"], r["outcome"]) for r in rows] == [
        (1, "incomplete"), (2, "incomplete"), (3, "resolved"),
    ]

    replay, _ = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    assert replay == first_attempt_bytes
    for n in (3, 4):
        _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=n)
        err = _err(body)
        assert err["code"] == "state_illegal"
        assert err["details"]["reason"] == "call_not_active"
    assert len(_attempt_rows(conn, call_id)) == 3


def test_call_attempts_written_by_record_call_attempt_are_append_only(tmp_path):
    conn, cfg, controller, call_id = _new_job_setup(tmp_path)
    _, body = _record(controller.services, cfg, call_id=call_id, attempt_number=1)
    result = _ok(body)
    row = conn.execute(
        "SELECT id FROM _call_attempts WHERE call_id = ? AND attempt_number = ?",
        (call_id, result["attempt_number"]),
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError, match="append_only_table"):
        conn.execute("UPDATE _call_attempts SET outcome = 'resolved' WHERE id = ?", (row["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="append_only_table"):
        conn.execute("DELETE FROM _call_attempts WHERE id = ?", (row["id"],))


def test_four_way_rejection_equivalence_including_mark_call_unanswered(tmp_path):
    legs = ("agent_rejected", "customer_declined", "unanswered_direct", "unanswered_control")
    ordinary_reason = {
        "agent_rejected": "agent_rejected",
        "customer_declined": "customer_declined",
        "unanswered_direct": "unanswered",
    }
    arrival_at = clock.phoenix_instant(2026, 10, 1, 8, 0)
    call_ids = {}
    dumps = {}
    terminal = {}

    for leg in legs:
        conn, cfg, controller = _build(tmp_path, name=f"{leg}.db")
        now = _now_ms(conn)
        catalog = _usable_catalog_rows(conn, 1)[0]
        _open_invocation(conn, now)
        pending_id = _insert_pending(
            conn, now, arrival_at,
            channel="new_caller", address_catalog_id=catalog["id"],
        )
        call_id = _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)
        call_ids[leg] = call_id

        if leg in ordinary_reason:
            reason = ordinary_reason[leg]
            call = conn.execute(
                "SELECT * FROM _calls WHERE id = ?", (call_id,)
            ).fetchone()

            def fn(ctx, call=call, reason=reason, cfg=cfg):
                calls.ordinary_rejection(ctx, cfg, call, terminal_reason=reason)
                return None

            response = command(
                controller.services, _next_request_id("ordinary_rejection"), fn,
                operation="test.ordinary_rejection", args={"call_id": call_id, "reason": reason},
            )
            assert json.loads(response)["ok"] is True
        else:
            for n in (1, 2, 3):
                _, attempt_body = _record(
                    controller.services, cfg, call_id=call_id, attempt_number=n,
                )
                _ok(attempt_body)
            _, body = _mark_unanswered(controller.services, cfg, call_id=call_id)
            _ok(body)

        call_after = _call_row(conn, call_id)
        terminal[leg] = (call_after["status"], call_after["terminal_reason"])

        run_id = conn.execute("SELECT run_id FROM _run").fetchone()["run_id"]
        tables = dump(conn, run_id)["tables"]
        for row in tables["_calls"]:
            row["status"] = "terminal"
            row["terminal_reason"] = "terminal"
        tables["_call_attempts"] = []
        tables["_idempotency"] = []
        dumps[leg] = tables
        conn.close()

    assert len(set(call_ids.values())) == 1

    baseline = dumps["agent_rejected"]
    for leg in ("customer_declined", "unanswered_direct", "unanswered_control"):
        assert dumps[leg] == baseline

    expected = {
        "agent_rejected": ("rejected", "agent_rejected"),
        "customer_declined": ("declined", "customer_declined"),
        "unanswered_direct": ("unanswered", "unanswered"),
        "unanswered_control": ("unanswered", "unanswered"),
    }
    for leg, pair in expected.items():
        assert terminal[leg] == pair


def test_mark_call_unanswered_cancellation_cancels_and_frees_with_zero_deltas(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id, result = _book_via_call(
        conn, controller, cfg, customer_id=customer_id, location_id=location_id,
    )
    appointment_id = result["appointment_id"]
    job_id = result["job_id"]

    assignment_id = conn.execute(
        "SELECT id FROM _appointment_assignments WHERE appointment_id = ? AND status = 'active'",
        (appointment_id,),
    ).fetchone()["id"]
    assert _hold_count(conn, appointment_id) > 0

    cancellation_call_id, manufactured_now = _manufacture_cancellation_call(
        conn, customer_id=customer_id, location_id=location_id,
        address_catalog_id=catalog["id"], target_appointment_id=appointment_id,
        target_job_id=job_id,
    )
    invocation_id = _open_invocation(conn, manufactured_now)

    for n in (1, 2, 3):
        _, attempt_body = _record(
            controller.services, cfg, call_id=cancellation_call_id, attempt_number=n,
        )
        _ok(attempt_body)

    repeat_before = dict(
        conn.execute(
            "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    )
    prospect_before = conn.execute("SELECT COUNT(*) c FROM _prospect_state").fetchone()["c"]
    audit_before = _audit_count(conn)
    notification_before = conn.execute("SELECT COUNT(*) c FROM _notifications").fetchone()["c"]

    _, body = _mark_unanswered(
        controller.services, cfg, call_id=cancellation_call_id, artifact=controller.artifact,
    )
    result = _ok(body)
    controller.invocations.complete(
        invocation_id, request_id=_next_request_id("complete_invocation"),
        services=controller.services,
    )

    assert result["call_id"] == cancellation_call_id
    assert result["outcome"] == "unanswered"
    assert result["appointment_id"] == appointment_id
    assert len(result["released_blocks"]) == 1

    appt = _appointment_row(conn, appointment_id)
    assert appt["status"] == "cancelled"
    assert appt["cancelled_at"] == manufactured_now

    job = _job_row(conn, job_id)
    assert job["status"] == "cancelled"
    assert job["outcome"] == "cancelled_by_customer"

    assignment = conn.execute(
        "SELECT status, unassigned_at FROM _appointment_assignments WHERE id = ?",
        (assignment_id,),
    ).fetchone()
    assert assignment["status"] == "cancelled"
    assert assignment["unassigned_at"] == manufactured_now

    assert _hold_count(conn, appointment_id) == 0

    call = _call_row(conn, cancellation_call_id)
    assert call["status"] == "unanswered"
    assert call["terminal_reason"] == "unanswered"
    assert call["terminal_at"] is not None

    attempts = _attempt_rows(conn, cancellation_call_id)
    assert [r["outcome"] for r in attempts] == ["incomplete", "incomplete", "incomplete"]

    repeat_after = dict(
        conn.execute(
            "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    )
    assert repeat_after == repeat_before
    assert conn.execute("SELECT COUNT(*) c FROM _prospect_state").fetchone()["c"] == prospect_before
    assert _audit_count(conn) == audit_before
    assert (
        conn.execute("SELECT COUNT(*) c FROM _notifications").fetchone()["c"]
        == notification_before
    )


def test_no_public_wrapper_exists_for_either_operation():
    assert not hasattr(control, "record_call_attempt_command")
    assert not hasattr(control, "mark_call_unanswered_command")
    assert not any(name.endswith("_command") for name in control.__all__)


def test_command_rejects_reserved_request_ids(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    _open_invocation(conn, now)
    reserved = privileged_request_id("record_call_attempt", "x")
    assert is_reserved(reserved)
    with pytest.raises(EngineError) as ei:
        command(controller.services, reserved, lambda ctx: None, operation="test.op", args={})
    assert ei.value.code is ErrorCode.UNAUTHORIZED
