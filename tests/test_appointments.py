from __future__ import annotations

import json
from itertools import count

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    audit_count,
    geo_artifact_sha256,
    register_conn,
)
from csr_bench import appointments, clock, demand, geo_reader
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.enums import EventKind
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.run_init import initialize_run, open_run_db
from csr_bench.txn import privileged, privileged_request_id

SEED = 5
END_DATE = "2026-10-03"
WEEKDAY = "2026-10-01"
WEEKEND = "2026-10-03"

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
DESTINATION_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 2)

_ON_CALL_TECHNICIAN_IDS = (1, 3, 4)
_NON_ON_CALL_TECHNICIAN_ID = 2

_id_seq = count(9000)
_request_seq = count(1)


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": END_DATE,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _build(tmp_path):
    cfg = _config()
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=SEED, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    return conn, cfg, controller


def _customer_location_call(conn, now_ms: int, service_date: str, config=None) -> int:
    row_id = next(_id_seq)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (?, 'Acme', 'commercial', 'active', ?, ?)",
        (row_id, now_ms, now_ms),
    )
    if config is not None:
        demand.register_customer_repeat_state(
            conn, config, customer_id=row_id, segment="commercial", now_ms=now_ms
        )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, active, created_at, modified_at, "
        "effective_zone_id, address_catalog_id) VALUES (?, ?, 'HQ', '1 Main St', '', "
        "'Tempe', 'AZ', '85282', 33.4, -111.9, 1, ?, ?, ?, ?)",
        (
            row_id, row_id, now_ms, now_ms,
            DESTINATION_ADDRESS.effective_zone_id, DESTINATION_ADDRESS.address_id,
        ),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (?, ?, ?, 'new_job', 'booked', 'A', '555', 'HVAC service', 0, ?)",
        (row_id, now_ms, service_date, now_ms),
    )
    return row_id


def _book(services, config, **kwargs):
    request_id = privileged_request_id("book", next(_request_seq))
    response = privileged(
        services, request_id,
        lambda ctx: appointments.book(ctx, config, artifact=ARTIFACT, **kwargs),
    )
    return json.loads(response)


def _assign(services, config, **kwargs):
    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        services, request_id,
        lambda ctx: appointments.assign(ctx, config, artifact=ARTIFACT, **kwargs),
    )
    return json.loads(response)


def _ok(body: dict) -> dict:
    assert body["ok"] is True, body
    return body["result"]


def _err(body: dict) -> dict:
    assert body["ok"] is False, body
    return body["error"]


def _book_ok(conn, controller, cfg, *, service_date=WEEKDAY, **kwargs):
    now = clock.now(conn)
    call_id = _customer_location_call(conn, now, service_date, config=cfg)
    body = _book(
        controller.services, cfg,
        call_id=call_id, customer_id=call_id, location_id=call_id,
        requested_service_name="HVAC service", priority="normal", summary="x",
        service_date=service_date,
        **kwargs,
    )
    return _ok(body)


def _book_err(conn, controller, cfg, *, service_date=WEEKDAY, **kwargs):
    now = clock.now(conn)
    call_id = _customer_location_call(conn, now, service_date, config=cfg)
    body = _book(
        controller.services, cfg,
        call_id=call_id, customer_id=call_id, location_id=call_id,
        requested_service_name="HVAC service", priority="normal", summary="x",
        service_date=service_date,
        **kwargs,
    )
    return _err(body)


def _set_clock(conn, ms: int, service_date: str) -> None:
    conn.execute(
        "UPDATE _clock SET current_time_ms = ?, service_date = ? WHERE singleton = 1",
        (ms, service_date),
    )


@pytest.mark.parametrize(
    "job_type_id, first_block_code, expect_ok",
    [
        (1, "B07_09", True),
        (1, "B17_19", True),
        (2, "B07_09", True),
        (2, "B09_11", True),
        (2, "B11_13", True),
        (2, "B13_15", True),
        (2, "B15_17", False),
        (2, "B17_19", False),
    ],
)
def test_grid_generated_candidates_accepted_exactly_when_legal(
    tmp_path, job_type_id, first_block_code, expect_ok
):
    conn, cfg, controller = _build(tmp_path)
    if expect_ok:
        result = _book_ok(
            conn, controller, cfg,
            job_type_id=job_type_id, technician_id=1, first_block_code=first_block_code,
        )
        assert result["block_codes"][0] == first_block_code
    else:
        error = _book_err(
            conn, controller, cfg,
            job_type_id=job_type_id, technician_id=1, first_block_code=first_block_code,
        )
        assert error["code"] == ErrorCode.STATE_ILLEGAL.value
        assert error["details"]["reason"] == "grid_violation"


def test_grid_non_consecutive_install_blocks_rejected():
    with pytest.raises(EngineError) as ei:
        appointments.validate_reservation_shape(2, ("B07_09", "B11_13"))
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    assert ei.value.details["reason"] == "grid_violation"


def test_grid_standard_job_in_after_hours_block_for_on_call_technician_accepted(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=1, technician_id=1, first_block_code="B17_19",
    )
    assert result["block_codes"] == ["B17_19"]


def test_overlap_same_technician_same_block_rejected(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    _book_ok(conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11")
    error = _book_err(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "overlap"


def test_overlap_two_technicians_same_block_accepted(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    _book_ok(conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11")
    result = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=3, first_block_code="B09_11"
    )
    assert result["technician_id"] == 3


def test_overlap_install_second_block_colliding_with_existing_hold_rejected(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    _book_ok(conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11")
    error = _book_err(
        conn, controller, cfg, job_type_id=2, technician_id=1, first_block_code="B07_09"
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "overlap"


def test_shift_coverage_non_on_call_technician_rejected_at_after_hours(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    error = _book_err(
        conn, controller, cfg,
        job_type_id=1, technician_id=_NON_ON_CALL_TECHNICIAN_ID, first_block_code="B17_19",
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "shift_coverage"


def test_shift_coverage_non_on_call_technician_rejected_on_weekend(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    error = _book_err(
        conn, controller, cfg,
        job_type_id=1, technician_id=_NON_ON_CALL_TECHNICIAN_ID, first_block_code="B09_11",
        service_date=WEEKEND,
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "shift_coverage"


@pytest.mark.parametrize("technician_id", _ON_CALL_TECHNICIAN_IDS)
def test_shift_coverage_rotation_holder_accepted_at_after_hours(tmp_path, technician_id):
    conn, cfg, controller = _build(tmp_path)
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=1, technician_id=technician_id, first_block_code="B17_19",
    )
    assert result["technician_id"] == technician_id


@pytest.mark.parametrize("technician_id", _ON_CALL_TECHNICIAN_IDS)
def test_shift_coverage_rotation_holder_accepted_on_weekend(tmp_path, technician_id):
    conn, cfg, controller = _build(tmp_path)
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=1, technician_id=technician_id, first_block_code="B09_11",
        service_date=WEEKEND,
    )
    assert result["technician_id"] == technician_id


def test_shift_coverage_beyond_materialization_horizon_office_block_accepted(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    beyond_horizon_weekday = "2026-10-15"
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=1, technician_id=_NON_ON_CALL_TECHNICIAN_ID, first_block_code="B09_11",
        service_date=beyond_horizon_weekday,
    )
    assert result["technician_id"] == _NON_ON_CALL_TECHNICIAN_ID


def test_shift_coverage_beyond_materialization_horizon_after_hours_non_on_call_rejected(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    beyond_horizon_weekday = "2026-10-15"
    error = _book_err(
        conn, controller, cfg,
        job_type_id=1, technician_id=_NON_ON_CALL_TECHNICIAN_ID, first_block_code="B17_19",
        service_date=beyond_horizon_weekday,
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "shift_coverage"


@pytest.mark.parametrize("technician_id", _ON_CALL_TECHNICIAN_IDS)
def test_shift_coverage_beyond_materialization_horizon_weekend_on_call_accepted(
    tmp_path, technician_id
):
    conn, cfg, controller = _build(tmp_path)
    beyond_horizon_weekend = "2026-10-17"
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=1, technician_id=technician_id, first_block_code="B09_11",
        service_date=beyond_horizon_weekend,
    )
    assert result["technician_id"] == technician_id


def test_unqualified_technician_books_successfully(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=4, technician_id=1, first_block_code="B09_11",
    )
    assert result["technician_id"] == 1


def test_unsupported_category_books_successfully(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=5, technician_id=1, first_block_code="B11_13",
    )
    assert result["technician_id"] == 1


def test_booking_writes_correct_rows_in_all_five_tables(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    audit_before = audit_count(conn)
    result = _book_ok(
        conn, controller, cfg,
        job_type_id=2, technician_id=1, first_block_code="B07_09",
    )
    assert audit_count(conn) == audit_before

    job = conn.execute("SELECT * FROM _jobs WHERE id = ?", (result["job_id"],)).fetchone()
    assert job["status"] == "scheduled"
    assert job["no_charge"] == 0
    assert job["job_number"] == result["job_number"]

    appt = conn.execute(
        "SELECT * FROM _appointments WHERE id = ?", (result["appointment_id"],)
    ).fetchone()
    assert appt["status"] == "scheduled"
    assert appt["appointment_number"] == result["appointment_number"]
    start = clock.phoenix_instant(2026, 10, 1, 7, 0)
    end = clock.phoenix_instant(2026, 10, 1, 11, 0)
    first_block_end = clock.phoenix_instant(2026, 10, 1, 9, 0)
    assert appt["scheduled_start"] == start
    assert appt["scheduled_end"] == end
    assert appt["arrival_window_start"] == start
    assert appt["arrival_window_end"] == first_block_end

    blocks = conn.execute(
        "SELECT ordinal, block_code, block_start, block_end FROM _appointment_blocks "
        "WHERE appointment_id = ? ORDER BY ordinal",
        (result["appointment_id"],),
    ).fetchall()
    assert [b["ordinal"] for b in blocks] == [1, 2]
    assert [b["block_code"] for b in blocks] == ["B07_09", "B09_11"]

    assignments = conn.execute(
        "SELECT * FROM _appointment_assignments WHERE appointment_id = ?",
        (result["appointment_id"],),
    ).fetchall()
    assert len(assignments) == 1
    assert assignments[0]["status"] == "active"
    assert assignments[0]["assigned_by"] == "agent"
    assert assignments[0]["technician_id"] == 1

    second_block_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    holds = conn.execute(
        "SELECT technician_id, block_start, hold_kind, appointment_id "
        "FROM _technician_block_holds WHERE appointment_id = ? ORDER BY block_start",
        (result["appointment_id"],),
    ).fetchall()
    assert [h["block_start"] for h in holds] == [start, second_block_start]
    assert all(h["technician_id"] == 1 and h["hold_kind"] == "job" for h in holds)


def test_block_already_started_rejects_booking_into_a_started_unlocked_block(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    started = clock.phoenix_instant(2026, 10, 1, 9, 30)
    _set_clock(conn, started, WEEKDAY)
    assert conn.execute("SELECT 1 FROM _block_locks").fetchone() is None

    error = _book_err(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "block_already_started"


def test_block_already_started_rejects_reassignment_into_a_started_unlocked_block(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    started = clock.phoenix_instant(2026, 10, 1, 9, 30)
    _set_clock(conn, started, WEEKDAY)
    assert conn.execute("SELECT 1 FROM _block_locks").fetchone() is None

    error = _err(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3)
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "block_already_started"


def test_failed_booking_rolls_back_completely_and_consumes_no_draws(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    _book_ok(conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11")

    def _counts():
        return {
            table: conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in (
                "_jobs", "_appointments", "_appointment_blocks",
                "_appointment_assignments", "_technician_block_holds",
            )
        }

    before = _counts()
    audit_before = audit_count(conn)
    error = _book_err(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert _counts() == before
    assert audit_count(conn) == audit_before


def _pause_before_lock(conn, controller, service_date: str, block_code: str) -> None:
    while True:
        locked = conn.execute(
            "SELECT 1 FROM _block_locks WHERE service_date = ? AND block_code = ?",
            (service_date, block_code),
        ).fetchone()
        assert locked is None, f"{block_code} already locked before we could pause"
        upcoming = controller.events.next_pending()
        assert upcoming is not None, "ran out of events before the block locked"
        payload = json.loads(upcoming["payload"] or "{}")
        if payload.get("block_code") == block_code and EventKind(upcoming["kind"]) in (
            EventKind.FIRST_BLOCK_LOCK, EventKind.BLOCK_LOCK,
        ):
            return
        assert controller.step()


def _step_until_locked(conn, controller, service_date: str, block_code: str) -> None:
    while conn.execute(
        "SELECT 1 FROM _block_locks WHERE service_date = ? AND block_code = ?",
        (service_date, block_code),
    ).fetchone() is None:
        assert controller.step(), "ran out of events before the block locked"


def test_lock_b07_09_cannot_be_reassigned_after_opening(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B07_09"
    )
    _step_until_locked(conn, controller, WEEKDAY, "B07_09")

    first_error = _err(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3)
    )
    second_error = _err(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=4)
    )
    for error in (first_error, second_error):
        assert error["code"] == ErrorCode.STATE_ILLEGAL.value
        assert error["details"]["reason"] == "locked_appointment"


def test_lock_b07_09_rejects_new_bookings_before_its_nominal_start(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    _step_until_locked(conn, controller, WEEKDAY, "B07_09")
    assert clock.now(conn) < clock.phoenix_instant(2026, 10, 1, 7, 0)

    error = _book_err(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B07_09"
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "locked_appointment"


def test_lock_b11_13_reassignable_before_11_and_not_at_or_after(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B11_13"
    )
    _pause_before_lock(conn, controller, WEEKDAY, "B11_13")
    assert clock.now(conn) < clock.phoenix_instant(2026, 10, 1, 11, 0)

    before_result = _ok(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3)
    )
    assert before_result["technician_id"] == 3
    assert before_result["previous_technician_id"] == 1

    _step_until_locked(conn, controller, WEEKDAY, "B11_13")

    after_error = _err(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=4)
    )
    assert after_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert after_error["details"]["reason"] == "locked_appointment"


def test_assign_appends_reassigned_event_and_moves_block_holds(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    result = _ok(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3)
    )
    assert result == {
        "appointment_id": booked["appointment_id"],
        "technician_id": 3,
        "previous_technician_id": 1,
        "trip_index": 1,
    }

    events = conn.execute(
        "SELECT event_type, technician_id FROM _appointment_events WHERE appointment_id = ?",
        (booked["appointment_id"],),
    ).fetchall()
    assert [e["event_type"] for e in events] == ["reassigned"]
    assert events[0]["technician_id"] == 3

    active = conn.execute(
        "SELECT technician_id FROM _appointment_assignments "
        "WHERE appointment_id = ? AND status = 'active'",
        (booked["appointment_id"],),
    ).fetchone()
    assert active["technician_id"] == 3

    holds = conn.execute(
        "SELECT technician_id FROM _technician_block_holds WHERE appointment_id = ?",
        (booked["appointment_id"],),
    ).fetchall()
    assert all(h["technician_id"] == 3 for h in holds)


def test_assign_rejects_an_appointment_whose_work_already_started(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    conn.execute(
        "UPDATE _appointments SET status = 'in_progress' WHERE id = ?",
        (booked["appointment_id"],),
    )
    conn.commit()
    error = _err(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3)
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "work_already_started"


def test_trip_index_increments_once_per_reassignment(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    assert appointments.trip_index_for_appointment(conn, booked["appointment_id"]) == 0

    first = _ok(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3)
    )
    assert first["trip_index"] == 1
    assert appointments.trip_index_for_appointment(conn, booked["appointment_id"]) == 1

    second = _ok(
        _assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=4)
    )
    assert second["trip_index"] == 2
    assert appointments.trip_index_for_appointment(conn, booked["appointment_id"]) == 2


def test_assign_command_requires_an_active_invocation(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    response = appointments.assign_command(
        controller.services, "dispatch-assign-1", cfg,
        appointment_id=booked["appointment_id"], technician_id=3,
    )
    body = json.loads(response)
    assert body["ok"] is False
    assert body["error"]["code"] == ErrorCode.INVOCATION_CONFLICT.value

    conn.execute(
        "INSERT INTO _invocations(type, ts_ms, sequence, status, attempt_count) "
        "VALUES ('call', 0, 1, 'active', 1)"
    )
    response2 = appointments.assign_command(
        controller.services, "dispatch-assign-2", cfg,
        appointment_id=booked["appointment_id"], technician_id=3,
    )
    body2 = json.loads(response2)
    assert body2["ok"] is True
    assert body2["result"]["technician_id"] == 3


def _off_hours_marks(conn, appointment_id):
    rows = conn.execute(
        "SELECT scheduled_at_ms, entity_type, payload FROM _events "
        "WHERE kind = ? AND entity_id = ?",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, appointment_id),
    ).fetchall()
    return [(r["scheduled_at_ms"], r["entity_type"], json.loads(r["payload"])) for r in rows]


def test_off_hours_mark_weekday_office_booking_schedules_no_mark(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B09_11"
    )
    assert _off_hours_marks(conn, booked["appointment_id"]) == []


def test_off_hours_mark_weekday_after_hours_block_schedules_at_seventeen_hundred(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B17_19"
    )
    marks = _off_hours_marks(conn, booked["appointment_id"])
    assert len(marks) == 1
    scheduled_at_ms, entity_type, payload = marks[0]
    assert scheduled_at_ms == clock.phoenix_instant(2026, 10, 1, 17, 0)
    assert entity_type == "appointment"
    assert payload == {"appointment_id": booked["appointment_id"], "block_hours": 2}


def test_off_hours_mark_weekend_standard_job_schedules_at_its_block_start(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg,
        job_type_id=1, technician_id=1, first_block_code="B09_11", service_date=WEEKEND,
    )
    marks = _off_hours_marks(conn, booked["appointment_id"])
    assert len(marks) == 1
    scheduled_at_ms, entity_type, payload = marks[0]
    assert scheduled_at_ms == clock.phoenix_instant(2026, 10, 3, 9, 0)
    assert payload == {"appointment_id": booked["appointment_id"], "block_hours": 2}


def test_off_hours_mark_weekend_installation_schedules_at_first_block_with_four_hours(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg,
        job_type_id=2, technician_id=1, first_block_code="B09_11", service_date=WEEKEND,
    )
    marks = _off_hours_marks(conn, booked["appointment_id"])
    assert len(marks) == 1
    scheduled_at_ms, entity_type, payload = marks[0]
    assert scheduled_at_ms == clock.phoenix_instant(2026, 10, 3, 9, 0)
    assert payload == {"appointment_id": booked["appointment_id"], "block_hours": 4}


def test_off_hours_mark_is_never_cancelled_by_reassignment(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    booked = _book_ok(
        conn, controller, cfg, job_type_id=1, technician_id=1, first_block_code="B17_19"
    )
    before = _off_hours_marks(conn, booked["appointment_id"])
    _ok(_assign(controller.services, cfg, appointment_id=booked["appointment_id"], technician_id=3))
    after = _off_hours_marks(conn, booked["appointment_id"])
    assert before == after
