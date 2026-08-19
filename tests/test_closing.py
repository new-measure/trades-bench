from __future__ import annotations

import json
from itertools import count

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    register_conn,
)
from trades_bench import appointments, booking, calls, clock, closing, geo_reader
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import InvocationType
from trades_bench.registry import OPERATIONS_BY_NAME
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.txn import privileged, privileged_request_id

SEED = 5
TODAY = "2026-10-01"
TOMORROW = "2026-10-02"
END_DATE = "2026-10-03"
CLOSING_HOUR = 19

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
DESTINATION_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 2)

_id_seq = count(9000)
_request_seq = count(1)


def _request_id(prefix: str) -> str:
    return f"{prefix}.{next(_request_seq)}"


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


def _ms(service_date, hour, minute=0):
    parts = [int(part) for part in service_date.split("-")]
    return clock.phoenix_instant(parts[0], parts[1], parts[2], hour, minute)


def _customer_location_call(conn, now_ms, service_date):
    row_id = next(_id_seq)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (?, 'Acme', 'commercial', 'active', ?, ?)",
        (row_id, now_ms, now_ms),
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


def _book_appointment(conn, cfg, controller, *, service_date, first_block_code, technician_id):
    call_id = _customer_location_call(conn, clock.now(conn), service_date)
    body = json.loads(
        privileged(
            controller.services,
            privileged_request_id("book", next(_request_seq)),
            lambda ctx: appointments.book(
                ctx, cfg, artifact=ARTIFACT,
                call_id=call_id, customer_id=call_id, location_id=call_id,
                job_type_id=1, requested_service_name="HVAC service", priority="normal",
                summary="x", technician_id=technician_id, service_date=service_date,
                first_block_code=first_block_code,
            ),
        )
    )
    assert body["ok"] is True, body
    return int(body["result"]["appointment_id"])


def _open_closing_invocation(conn, controller, closing_at):
    opened = controller.invocations.open(
        InvocationType.CLOSING,
        closing_at,
        request_id=_request_id("closing"),
        services=controller.services,
    )
    assert isinstance(opened, int), opened
    return opened


def _error(response):
    body = json.loads(response)
    assert body["ok"] is False, body
    return body["error"]


@pytest.fixture
def closing_world(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    today_appointment = _book_appointment(
        conn, cfg, controller, service_date=TODAY, first_block_code="B17_19", technician_id=1
    )
    tomorrow_appointment = _book_appointment(
        conn, cfg, controller, service_date=TOMORROW, first_block_code="B09_11", technician_id=1
    )
    closing_at = _ms(TODAY, CLOSING_HOUR)
    _open_closing_invocation(conn, controller, closing_at)
    yield {
        "conn": conn,
        "config": cfg,
        "controller": controller,
        "closing_at": closing_at,
        "today_appointment": today_appointment,
        "tomorrow_appointment": tomorrow_appointment,
    }
    conn.close()


def test_closing_rejects_a_reassignment_of_the_finished_day(closing_world):
    error = _error(
        appointments.assign_command(
            closing_world["controller"].services,
            _request_id("assign.today"),
            closing_world["config"],
            appointment_id=closing_world["today_appointment"],
            technician_id=2,
            artifact=ARTIFACT,
        )
    )
    assert error["code"] == "closing_restricted"
    assigned = closing_world["conn"].execute(
        "SELECT technician_id FROM _appointment_assignments "
        "WHERE appointment_id = ? AND status = 'active'",
        (closing_world["today_appointment"],),
    ).fetchone()
    assert assigned["technician_id"] == 1


def test_closing_allows_a_reassignment_of_a_future_day(closing_world):
    body = json.loads(
        appointments.assign_command(
            closing_world["controller"].services,
            _request_id("assign.tomorrow"),
            closing_world["config"],
            appointment_id=closing_world["tomorrow_appointment"],
            technician_id=2,
            artifact=ARTIFACT,
        )
    )
    assert body["ok"] is True, body
    assigned = closing_world["conn"].execute(
        "SELECT technician_id FROM _appointment_assignments "
        "WHERE appointment_id = ? AND status = 'active'",
        (closing_world["tomorrow_appointment"],),
    ).fetchone()
    assert assigned["technician_id"] == 2


def test_closing_restriction_discloses_nothing_about_the_closing_time(closing_world):
    response = appointments.assign_command(
        closing_world["controller"].services,
        _request_id("assign.silent"),
        closing_world["config"],
        appointment_id=closing_world["today_appointment"],
        technician_id=2,
        artifact=ARTIFACT,
    )
    closing_at = closing_world["closing_at"]
    assert _error(response) == {
        "code": "closing_restricted",
        "message": closing.RESTRICTED_MESSAGE,
    }
    text = response.decode("utf-8")
    assert str(closing_at) not in text
    assert clock.to_iso8601(closing_at) not in text
    assert "19:00" not in text


def test_a_call_scoped_mutation_is_restricted_during_closing(closing_world):
    error = _error(
        calls.reject_command(
            closing_world["controller"].services,
            _request_id("reject"),
            closing_world["config"],
            call_id=closing_world["today_appointment"],
        )
    )
    assert error["code"] == "closing_restricted"


def test_a_future_dated_booking_passes_the_closing_gate(closing_world):
    error = _error(
        booking.book_command(
            closing_world["controller"].services,
            _request_id("book.future"),
            closing_world["config"],
            call_id=closing_world["today_appointment"],
            customer_id=closing_world["today_appointment"],
            location_id=closing_world["today_appointment"],
            job_type_id=1,
            service_date=TOMORROW,
            block_start="09:00",
            technician_id=1,
        )
    )
    assert error["code"] != "closing_restricted"


def test_a_booking_of_the_finished_day_is_restricted(closing_world):
    error = _error(
        booking.book_command(
            closing_world["controller"].services,
            _request_id("book.today"),
            closing_world["config"],
            call_id=closing_world["today_appointment"],
            customer_id=closing_world["today_appointment"],
            location_id=closing_world["today_appointment"],
            job_type_id=1,
            service_date=TODAY,
            block_start="17:00",
            technician_id=1,
        )
    )
    assert error["code"] == "closing_restricted"


def test_an_unresolvable_target_is_restricted_during_closing(closing_world):
    error = _error(
        appointments.assign_command(
            closing_world["controller"].services,
            _request_id("assign.missing"),
            closing_world["config"],
            appointment_id=987_654,
            technician_id=2,
            artifact=ARTIFACT,
        )
    )
    assert error["code"] == "closing_restricted"


def test_the_same_reassignment_passes_at_opening_and_fails_at_closing(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    try:
        appointment_id = _book_appointment(
            conn, cfg, controller, service_date=TODAY, first_block_code="B09_11", technician_id=1
        )
        opened = controller.invocations.open(
            InvocationType.OPENING,
            clock.now(conn),
            request_id=_request_id("opening"),
            services=controller.services,
        )
        assert isinstance(opened, int), opened
        body = json.loads(
            appointments.assign_command(
                controller.services,
                _request_id("assign.opening"),
                cfg,
                appointment_id=appointment_id,
                technician_id=2,
                artifact=ARTIFACT,
            )
        )
        assert body["ok"] is True, body
        controller.invocations.complete(
            opened, request_id=_request_id("complete"), services=controller.services
        )
        _open_closing_invocation(conn, controller, _ms(TODAY, CLOSING_HOUR))
        error = _error(
            appointments.assign_command(
                controller.services,
                _request_id("assign.closing"),
                cfg,
                appointment_id=appointment_id,
                technician_id=1,
                artifact=ARTIFACT,
            )
        )
        assert error["code"] == "closing_restricted"
    finally:
        conn.close()


def test_every_registry_mutation_declares_its_closing_target():
    mutation_names = {spec.name for spec in OPERATIONS_BY_NAME.values() if spec.mutation}
    declared = set(closing.FUTURE_TARGET_RESOLVERS) | closing.PRESENT_TARGET_OPERATIONS
    assert declared == mutation_names
    assert not set(closing.FUTURE_TARGET_RESOLVERS) & closing.PRESENT_TARGET_OPERATIONS
