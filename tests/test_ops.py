from __future__ import annotations

import dataclasses
import http.client
import json
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    geo_run_config,
)
from trades_bench import clock, db, geo_reader
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.db import insert_run
from trades_bench.enums import EventKind, RunStatus
from trades_bench.events import Events
from trades_bench.http_engine import (
    _MUTATION_DISPATCH,
    Engine,
    _mutation_fingerprint_args,
    open_engine_connection,
)
from trades_bench.invocations import Invocations
from trades_bench.ledger import Ledger
from trades_bench.migrations import migrate
from trades_bench.ops_read import build_view_list_sql, encode_cursor
from trades_bench.registry import (
    OPERATIONS,
    OPERATIONS_BY_NAME,
    FilterOp,
    OperationSpec,
    validate_operation_identity,
)
from trades_bench.rng import Rng, sqlite_audit_recorder
from trades_bench.run_init import initialize_run
from trades_bench.txn import Services
from trades_bench.views import VIEWS, create_views

_FROZEN_OPERATION_NAMES = frozenset(
    {
        "appointments.acknowledge_cancellation",
        "appointments.get",
        "appointments.list",
        "appointments.list_events",
        "calls.get",
        "calls.list",
        "calls.reject",
        "crm.create_customer",
        "crm.create_location",
        "crm.get_customer",
        "crm.get_location",
        "crm.list_customers",
        "crm.list_locations",
        "dispatch.assign",
        "dispatch.get_technician_location",
        "dispatch.list_assignments",
        "finance.get_cash_position",
        "finance.list_invoices",
        "finance.list_ledger",
        "finance.list_payments",
        "jobs.book",
        "jobs.get",
        "jobs.list",
        "maps.distance",
        "query.sql",
        "weather.get",
    }
)

_GARBAGE_OPERATION = "garbage.not_an_operation"

_ABSENT_OPERATIONS = (
    "workforce.list_technicians",
    "workforce.get_technician",
    "workforce.list_shifts",
    "workforce.hire",
    "workforce.terminate",
    "workforce.get_training_rates",
    "workforce.schedule_training",
    "workforce.set_on_call",
    "next_day",
    "dashboard.get",
    "dispatch.get_dashboard",
    "dispatch.get_dispatch_board",
    "notifications.get",
    "maps.estimate_travel_time",
    "workforce.get_hiring_quote",
    "booking.cancel_booking_by_company",
    "jobs.unbook_job",
    "reports.submit_daily_report",
    "appointments.reassign",
    "appointments.create_warranty_rework",
    "crm.update_customer",
    "crm.delete_customer",
    "dispatch.unassign",
    "reports.labor_market",
    "maps.classify_serviceability",
    "workforce.edit_shift",
)

_WEATHER_ALLOWED_WITH_STORM = frozenset(
    {
        "zone_id",
        "date",
        "kind",
        "horizon_days",
        "high_f",
        "low_f",
        "precipitation_probability",
        "storm",
        "storm_severity",
    }
)
_WEATHER_ALLOWED_WITHOUT_STORM = _WEATHER_ALLOWED_WITH_STORM - {"storm_severity"}

_STRUCTURED_ERROR_CODES = frozenset(
    {
        "validation_error",
        "conflict",
        "not_found",
        "invalid_request",
        "no_active_session",
        "insufficient_cash",
        "request_id_conflict",
        "run_not_active",
    }
)

_VIEW_COLUMNS = {spec.name: tuple(col.name for col in spec.columns) for spec in VIEWS}

_PAGINATION_CUSTOMER_COUNT = 37
_PAGINATION_CLAMP_CUSTOMER_COUNT = 250
_PAGINATION_PAGE_SIZE = 7

_ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
_FIRST_ADDRESS = next(row for row in _ARTIFACT.catalog if row.address_id == 1)
_SECOND_ADDRESS = next(row for row in _ARTIFACT.catalog if row.address_id == 2)


def _request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        headers_out = dict(resp.getheaders())
        status = resp.status
    finally:
        conn.close()
    parsed = json.loads(raw) if raw else None
    return status, parsed, headers_out, raw


def _ops_path(name: str) -> str:
    return f"/v1/ops/{name}"


def _open_call_session(port: int, control_token: str, ts_ms: int) -> int:
    status, body, _, _ = _request(
        port,
        "POST",
        "/v1/control/next_invocation",
        token=control_token,
        body={"type": "call", "ts_ms": ts_ms},
    )
    assert status == 200, body
    return int(body["data"]["invocation_id"])


def _build_empty_engine(
    dir_path: Path,
    *,
    scenario_overrides: dict | None = None,
    seed_customers: int = 0,
) -> tuple[Engine, object]:
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    conn = open_engine_connection(path)
    migrate(conn)
    create_views(conn)
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=str(uuid.uuid4()),
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=1_000_000,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.ACTIVE,
    )
    if seed_customers:
        for i in range(seed_customers):
            conn.execute(
                "INSERT INTO _customers "
                "(id, name, segment, status, created_at, modified_at, origin) "
                "VALUES (?, ?, 'commercial', 'active', 500, 500, 'run')",
                (i + 1, f"Paginated Customer {i:03d}"),
            )
    conn.execute("COMMIT")
    rng = Rng(1, "1.0.0", audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    ledger = Ledger(conn)
    events = Events(conn, rng)
    invocations = Invocations(conn)
    services = Services(conn=conn, rng=rng, ledger=ledger, invocations=invocations, events=events)
    scenario_json = None
    if scenario_overrides:
        scenario_json = json.dumps({"keys": scenario_overrides})
    config = load_run_config(scenario_json=scenario_json)
    engine = Engine(
        services=services,
        config=config,
        db_path=path,
        artifact=_ARTIFACT,
    )
    return engine, conn


def _seed_ops_surface(conn) -> None:
    now = conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]
    past = now - 3_600_000
    future = now + 7_200_000
    future_plus_day = future + 86_400_000
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (1, 'Alpha Commercial', 'commercial', 'active', ?, ?), "
        "(2, 'Beta Residential', 'residential', 'active', ?, ?)",
        (past, past, past + 60_000, past + 60_000),
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, zone_id, active, created_at, modified_at, "
        "effective_zone_id) VALUES "
        "(1, 1, 'HQ', '100 Main St', '', 'Phoenix', 'AZ', '85001', 33.5, -112.0, 1, 1, ?, ?, 1), "
        "(2, 2, 'Home', '200 Oak Ave', '', 'Tempe', 'AZ', '85281', 33.4, -111.9, 4, 1, ?, ?, 4)",
        (past, past, past, past),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, segment, customer_id, location_id, requested_service_name, urgent, "
        "created_at) VALUES "
        "(1, ?, '2026-10-01', 'new_job', 'booked', 'Caller A', '4805551111', 'commercial', "
        "1, 1, 'HVAC service', 0, ?), "
        "(2, ?, '2026-10-02', 'new_job', 'active', 'Caller B', '4805552222', 'residential', "
        "2, 2, 'Plumbing', 1, ?), "
        "(3, ?, '2026-10-03', 'cancellation', 'active', 'Caller C', '4805553333', 'commercial', "
        "1, 1, 'HVAC service', 0, ?)",
        (past, past, now, now, now, now),
    )
    conn.execute(
        "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, job_type_id, "
        "business_unit_id, requested_service_name, status, priority, no_charge, summary, "
        "created_at, modified_at) VALUES "
        "(1, 'J-1001', 1, 1, 1, 1, 1, 'HVAC service', 'scheduled', 'normal', 0, 's', ?, ?), "
        "(2, 'J-1002', 2, 2, 2, 2, 2, 'Plumbing', 'completed', 'normal', 0, 's', ?, ?), "
        "(3, 'J-1003', 3, 1, 1, 1, 1, 'HVAC service', 'cancelled', 'normal', 0, 's', ?, ?)",
        (past, past, now, now, future, future),
    )
    scheduled_a = future
    scheduled_b = future + 7_200_000
    conn.execute(
        "INSERT INTO _appointments (id, appointment_number, job_id, status, scheduled_start, "
        "scheduled_end, arrival_window_start, arrival_window_end, created_at, modified_at) "
        "VALUES "
        "(1, 'A-1001', 1, 'scheduled', ?, ?, ?, ?, ?, ?), "
        "(2, 'A-1002', 2, 'completed', ?, ?, ?, ?, ?, ?), "
        "(3, 'A-1003', 3, 'scheduled', ?, ?, ?, ?, ?, ?)",
        (
            scheduled_a, scheduled_a + 7_200_000, scheduled_a, scheduled_a + 7_200_000,
            past, past,
            scheduled_b, scheduled_b + 7_200_000, scheduled_b, scheduled_b + 7_200_000,
            now, now,
            future_plus_day, future_plus_day + 7_200_000,
            future_plus_day, future_plus_day + 7_200_000,
            future, future,
        ),
    )
    conn.execute(
        "INSERT INTO _appointment_events (id, appointment_id, technician_id, event_type, "
        "occurred_at, service_date, detail, created_at) VALUES "
        "(1, 1, 1, 'dispatched', ?, '2026-10-01', 'assigned', ?), "
        "(2, 1, 1, 'work_completed', ?, '2026-10-01', 'done', ?), "
        "(3, 2, 1, 'dispatched', ?, '2026-10-02', 'assigned', ?)",
        (past, past, now, now, past + 30_000, past + 30_000),
    )
    conn.execute(
        "INSERT INTO _appointment_assignments (id, appointment_id, job_id, technician_id, "
        "status, assigned_at, unassigned_at, assigned_by, created_at) VALUES "
        "(1, 1, 1, 1, 'active', ?, NULL, 'agent', ?), "
        "(2, 2, 2, 2, 'active', ?, NULL, 'agent', ?)",
        (past, past, past - 86_400_000, past - 86_400_000),
    )
    conn.execute(
        "INSERT INTO _invoices (id, invoice_number, job_id, customer_id, location_id, "
        "business_unit_id, invoice_date, issued_at, status, no_charge, subtotal_cents, "
        "total_cents, material_cost_cents, material_percent_basis_points, created_at) "
        "VALUES "
        "(1, 'INV-1001', 1, 1, 1, 1, '2026-10-01', ?, 'paid', 0, 10000, 10000, 0, 0, ?), "
        "(2, 'INV-1002', 2, 2, 2, 2, '2026-09-30', ?, 'paid', 0, 5000, 5000, 0, 0, ?)",
        (past, past, past - 86_400_000, past - 86_400_000),
    )
    conn.execute(
        "INSERT INTO _payments (id, payment_number, invoice_id, customer_id, paid_at, "
        "amount_cents, payment_type, status, created_at) VALUES "
        "(1, 'PAY-1001', 1, 1, ?, 10000, 'immediate', 'posted', ?), "
        "(2, 'PAY-1002', 2, 2, ?, 5000, 'immediate', 'posted', ?)",
        (past, past, past - 86_400_000, past - 86_400_000),
    )
    conn.execute(
        "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, amount_cents, "
        "balance_after_cents, description, created_at) VALUES "
        "(100, ?, '2026-10-01', 'revenue', 10000, 10000, 'seed revenue', ?), "
        "(101, ?, '2026-09-30', 'payroll_base', -5000, 5000, 'seed payroll', ?), "
        "(102, ?, '2026-10-02', 'operating_overhead', -1000, 9000, 'seed overhead', ?)",
        (past, past, past - 86_400_000, past - 86_400_000, now, now),
    )


def _build_seeded_engine(dir_path: Path) -> tuple[Engine, object]:
    eng, conn = _build_warm_engine(dir_path)
    _seed_ops_surface(conn)
    return eng, conn


def _build_warm_engine(dir_path: Path) -> tuple[Engine, object]:
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    config = geo_run_config()
    initialize_run(path, master_seed=1, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_engine_connection(path)
    rng = Rng(1, "1.0.0", audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    ledger = Ledger(conn)
    events = Events(conn, rng)
    invocations = Invocations(conn)
    services = Services(conn=conn, rng=rng, ledger=ledger, invocations=invocations, events=events)
    engine = Engine(
        services=services,
        config=config,
        db_path=path,
        artifact=_ARTIFACT,
    )
    return engine, conn


def _happy_path_config():
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "acceptance.base_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
        "acceptance.floor_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
        "cancellations.base_probability_by_segment": {
            "residential": 0.0, "commercial": 0.0, "apartment": 0.0,
        },
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _build_happy_path_engine(dir_path: Path) -> tuple[Engine, object]:
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    config = _happy_path_config()
    initialize_run(path, master_seed=1, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_engine_connection(path)
    rng = Rng(1, "1.0.0", audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    ledger = Ledger(conn)
    events = Events(conn, rng)
    invocations = Invocations(conn)
    services = Services(conn=conn, rng=rng, ledger=ledger, invocations=invocations, events=events)
    engine = Engine(services=services, config=config, db_path=path, artifact=_ARTIFACT)
    return engine, conn


def _manufacture_cancellation_call(
    conn, rng, *, customer_id: int, location_id: int, address_catalog_id: int,
    target_appointment_id: int, buffer_ms: int = 60_000,
) -> tuple[int, int]:
    latest = conn.execute(
        "SELECT MAX(applied_at_ms) AS latest FROM _events "
        "WHERE kind = ? AND status = 'applied'",
        (EventKind.CALL_INVOCATION.value,),
    ).fetchone()["latest"]
    now = clock.now(conn)
    if latest is not None:
        now = max(now, int(latest) + buffer_ms)
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
            "created_at) VALUES (?, ?, 'cancellation', 'active', 'Fixture Caller', "
            "'4805551234', 'residential', ?, ?, 'HVAC service', 0, NULL, ?, NULL, ?, ?)",
            (now, clock.service_date_for_ms(now), customer_id, location_id,
             target_appointment_id, address_catalog_id, now),
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


def _usable_catalog_row(conn):
    return conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT 1"
    ).fetchone()


def _insert_customer(conn, now, *, segment="residential", name="Fixture Customer"):
    cur = conn.execute(
        "INSERT INTO _customers (name, segment, status, created_at, modified_at) "
        "VALUES (?, ?, 'active', ?, ?)",
        (name, segment, now, now),
    )
    customer_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO _customer_contacts (customer_id, contact_type, value, do_not_text, "
        "memo, created_at) VALUES (?, 'phone', '4805551234', 0, '', ?)",
        (customer_id, now),
    )
    conn.execute(
        "INSERT INTO _customer_repeat_state (customer_id, propensity_micros, last_outcome, "
        "competitor_captured_at, updated_at) VALUES (?, 0, NULL, NULL, ?)",
        (customer_id, now),
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


def _insert_pending_call(conn, now, arrival_at, *, catalog):
    cur = conn.execute(
        "INSERT INTO _pending_calls (service_date, zone_id, arrival_at, call_kind, channel, "
        "segment, requested_category_id, urgent, customer_id, location_id, address_catalog_id, "
        "caller_name, caller_phone, prospect_id, related_job_id, slot_index, created_at) "
        "VALUES ('2026-10-01', NULL, ?, 'new_job', 'new_caller', 'residential', 2, 0, "
        "NULL, NULL, ?, 'Fixture Caller', '4805551234', NULL, NULL, NULL, ?)",
        (arrival_at, catalog["id"], now),
    )
    return int(cur.lastrowid)


def _arrive_call(conn, rng, pending_id, arrival_at):
    from trades_bench import calls, db

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


def _seed_active_call(conn, rng) -> int:
    now = conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]
    catalog = _usable_catalog_row(conn)
    pending_id = _insert_pending_call(conn, now, now + 60_000, catalog=catalog)
    return _arrive_call(conn, rng, pending_id, now + 60_000)


def _empty_probe_body(spec: OperationSpec) -> dict:
    read = spec.read
    assert read is not None
    if read.kind == "view_get":
        return {read.get_id_arg: 999_999}
    if read.kind == "view_list":
        body: dict[str, object] = {}
        for flt in read.filters:
            if flt.required:
                body[flt.arg_name] = _placeholder_for_type(flt.type)
        return body
    if read.kind == "view_singleton":
        return {}
    if read.kind == "sql":
        return {"statement": "SELECT COUNT(*) AS c FROM customers"}
    if read.kind == "weather":
        return {"zone_id": 1, "date": "2026-10-01"}
    if read.kind == "maps":
        return {
            "origin_address": _FIRST_ADDRESS.formatted_address,
            "destination_address": _SECOND_ADDRESS.formatted_address,
        }
    raise AssertionError(f"unsupported read kind {read.kind!r}")


def _mutation_probe_body(spec: OperationSpec) -> dict:
    body: dict[str, object] = {"request_id": "registry-probe-request-id"}
    for arg in spec.args:
        if arg.name == "request_id":
            continue
        if arg.required:
            body[arg.name] = _placeholder_for_type(arg.type)
    return body


def _placeholder_for_type(arg_type: str) -> object:
    if arg_type == "int":
        return 1
    if arg_type == "str":
        return "probe"
    if arg_type == "date":
        return "2026-10-01"
    if arg_type == "object":
        return {}
    if arg_type == "list":
        return []
    if arg_type == "bool":
        return False
    raise AssertionError(f"unsupported arg type {arg_type!r}")


def _warm_probe_body(conn, spec: OperationSpec) -> dict:
    read = spec.read
    assert read is not None
    if read.kind == "view_get":
        row = conn.execute(
            f"SELECT {read.pk_column} FROM {read.view} LIMIT 1"
        ).fetchone()
        assert row is not None
        return {read.get_id_arg: row[0]}
    if read.kind == "view_list":
        body: dict[str, object] = {}
        for flt in read.filters:
            if flt.required:
                body[flt.arg_name] = _discover_filter_value(conn, spec, flt)
        return body
    if read.kind == "view_singleton":
        return {}
    if read.kind == "sql":
        return {"statement": "SELECT id, name FROM customers LIMIT 3"}
    if read.kind == "weather":
        today = clock.now_service_date(conn)
        return {"zone_id": 1, "date": today}
    if read.kind == "maps":
        return {
            "origin_address": _FIRST_ADDRESS.formatted_address,
            "destination_address": _SECOND_ADDRESS.formatted_address,
        }
    raise AssertionError(f"unsupported read kind {read.kind!r}")


def _discover_filter_value(conn, spec: OperationSpec, flt) -> object:
    if spec.name == "appointments.list" and flt.arg_name == "technician_id":
        row = conn.execute(
            "SELECT technician_id FROM appointment_assignments "
            "WHERE unassigned_at IS NULL ORDER BY appointment_id LIMIT 1"
        ).fetchone()
        assert row is not None
        return int(row["technician_id"])
    if spec.name == "appointments.list_events" and flt.arg_name == "appointment_id":
        row = conn.execute(
            "SELECT appointment_id FROM appointment_events ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None
        return int(row["appointment_id"])
    if spec.name == "finance.list_ledger" and flt.arg_name in {"date_from", "date_to"}:
        return "2026-04-01" if flt.arg_name == "date_from" else "2026-09-30"
    read = spec.read
    assert read is not None and read.view is not None
    column = flt.column
    row = conn.execute(f"SELECT {column} FROM {read.view} LIMIT 1").fetchone()
    assert row is not None
    value = row[0]
    if flt.type == "date" and column.endswith("_iso"):
        return str(value)[:10]
    if flt.type == "date" and isinstance(value, int):
        return clock.to_iso8601(value)[:10]
    if flt.op is FilterOp.LIKE and isinstance(value, str) and len(value) >= 3:
        return value[1:-1]
    return value


def _discover_alternate_filter_value(conn, spec: OperationSpec, flt, current: object) -> object:
    if spec.name == "appointments.list_events" and flt.arg_name == "appointment_id":
        row = conn.execute(
            "SELECT appointment_id FROM appointment_events "
            "WHERE appointment_id != ? ORDER BY id LIMIT 1",
            (current,),
        ).fetchone()
        assert row is not None
        return int(row["appointment_id"])
    read = spec.read
    assert read is not None and read.view is not None
    column = flt.column
    row = conn.execute(
        f"SELECT {column} FROM {read.view} WHERE {column} != ? LIMIT 1",
        (current,),
    ).fetchone()
    assert row is not None, f"{spec.name}.{flt.arg_name} lacks an alternate fixture value"
    value = row[0]
    if flt.type == "date" and column.endswith("_iso"):
        return str(value)[:10]
    if flt.type == "date" and isinstance(value, int):
        return clock.to_iso8601(value)[:10]
    return value


def _filter_value_from_row(row: dict, flt, spec: OperationSpec) -> object | None:
    if spec.name == "appointments.list" and flt.arg_name == "technician_id":
        return None
    if spec.name == "dispatch.list_assignments" and flt.arg_name == "date":
        return row["assigned_at_iso"][:10]
    column = flt.column
    value = row.get(column)
    if value is None:
        return None
    if flt.type == "date" and isinstance(value, int):
        return clock.to_iso8601(value)[:10]
    if flt.type == "date" and isinstance(value, str):
        return value[:10]
    if flt.op is FilterOp.LIKE and isinstance(value, str) and len(value) >= 3:
        return value[1:-1]
    return value


def _row_matches_filter_value(row: dict, flt, spec: OperationSpec, value: object) -> bool:
    try:
        _assert_row_matches_filter(None, row, spec, flt, value)
        return True
    except AssertionError:
        return False


def _discover_date_filter_value(conn, spec: OperationSpec, flt) -> object:
    read = spec.read
    assert read is not None and read.view is not None
    column = flt.column
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {read.view} ORDER BY {column}"
    ).fetchall()
    assert len(rows) >= 2, f"{spec.name}.{flt.arg_name} lacks two distinct dates in fixture"
    if flt.op is FilterOp.DATE_GTE:
        pick = rows[-1][0]
    elif flt.op is FilterOp.DATE_LTE:
        pick = rows[0][0]
    else:
        pick = rows[0][0]
    if flt.type == "date" and isinstance(pick, int):
        if spec.name == "dispatch.list_assignments" and flt.arg_name == "date":
            return clock.to_iso8601(pick)[:10]
        return clock.to_iso8601(pick)[:10]
    if isinstance(pick, str):
        return pick[:10]
    return pick


def _discriminating_filter_value(
    baseline_rows: list[dict], spec: OperationSpec, flt
) -> object | None:
    for row in baseline_rows:
        candidate = _filter_value_from_row(row, flt, spec)
        if candidate is None:
            continue
        matching = sum(
            1 for item in baseline_rows if _row_matches_filter_value(item, flt, spec, candidate)
        )
        if 0 < matching < len(baseline_rows):
            return candidate
    return None


def _assert_row_matches_filter(conn, row: dict, spec: OperationSpec, flt, value: object) -> None:
    if spec.name == "appointments.list" and flt.arg_name == "technician_id":
        match = conn.execute(
            "SELECT 1 FROM appointment_assignments "
            "WHERE appointment_id = ? AND technician_id = ? AND unassigned_at IS NULL",
            (row["id"], value),
        ).fetchone()
        assert match is not None
        return
    if spec.name == "dispatch.list_assignments" and flt.arg_name == "date":
        assert row["assigned_at_iso"][:10] == value
        return
    column = flt.column
    actual = row.get(column)
    if flt.op is FilterOp.EQ:
        assert actual == value
        return
    if flt.op is FilterOp.LIKE:
        assert isinstance(value, str)
        assert isinstance(actual, str)
        assert value in actual
        return
    if flt.type == "date":
        if isinstance(actual, int):
            actual_date = clock.to_iso8601(actual)[:10]
        elif isinstance(actual, str):
            actual_date = actual[:10]
        else:
            raise AssertionError(f"unexpected date column value {actual!r}")
        if flt.op is FilterOp.DATE_GTE:
            assert actual_date >= str(value)
            return
        if flt.op is FilterOp.DATE_LTE:
            assert actual_date <= str(value)
            return
    raise AssertionError(f"unsupported filter {flt.arg_name!r} on {spec.name!r}")


def _page_all(port: int, token: str, op_name: str, *, limit: int, extra: dict | None = None):
    pages: list[list[dict]] = []
    cursors: list[str | None] = [None]
    cursor = None
    while True:
        body = {"limit": limit, **(extra or {})}
        if cursor is not None:
            body["cursor"] = cursor
        status, parsed, _, raw = _request(
            port, "POST", _ops_path(op_name), token=token, body=body
        )
        assert status == 200, parsed
        assert "data" in parsed and "meta" in parsed
        page = parsed["data"]
        assert isinstance(page, list)
        pages.append(page)
        next_cursor = parsed["meta"].get("next_cursor")
        if next_cursor is None:
            cursors.append(None)
            return pages, cursors, raw
        cursors.append(next_cursor)
        cursor = next_cursor


@pytest.fixture
def empty_engine(tmp_path):
    eng, conn = _build_empty_engine(tmp_path / "empty")
    eng.start()
    try:
        yield eng
    finally:
        eng.stop()
        conn.close()


@pytest.fixture
def seeded_engine(tmp_path):
    eng, conn = _build_seeded_engine(tmp_path / "seeded")
    eng.start()
    now_ms = clock.now(conn)
    _open_call_session(eng.port, eng.tokens.control, now_ms)
    try:
        yield eng, conn
    finally:
        eng.stop()
        conn.close()


@pytest.fixture
def warm_engine(seeded_engine):
    return seeded_engine


def test_frozen_operation_names_match_registry():
    registry_names = frozenset(spec.name for spec in OPERATIONS)
    assert _FROZEN_OPERATION_NAMES == registry_names
    assert len(_FROZEN_OPERATION_NAMES) == len(OPERATIONS)


def test_registry_names_match_route_table_keys():
    assert frozenset(OPERATIONS_BY_NAME) == _FROZEN_OPERATION_NAMES


@pytest.mark.parametrize("spec", OPERATIONS, ids=lambda spec: spec.name)
def test_every_registry_operation_is_dispatchable(empty_engine, spec):
    _open_call_session(empty_engine.port, empty_engine.tokens.control, 1_000_000)
    if spec.kind == "read":
        body = _empty_probe_body(spec)
        status, parsed, _, _ = _request(
            empty_engine.port,
            "POST",
            _ops_path(spec.name),
            token=empty_engine.tokens.agent,
            body=body,
        )
        assert parsed is not None
        if status == 200:
            assert parsed.keys() == {"data", "meta"}
            assert parsed["meta"].keys() >= {"as_of", "invocation_id", "spec_version"}
            if spec.read and spec.read.kind == "view_list":
                assert isinstance(parsed["data"], list)
        else:
            assert parsed["error"]["code"] in {"not_found", "validation_error"}
            assert parsed["error"]["code"] != "unknown_operation"
    else:
        body = _mutation_probe_body(spec)
        status, parsed, _, _ = _request(
            empty_engine.port,
            "POST",
            _ops_path(spec.name),
            token=empty_engine.tokens.agent,
            body=body,
        )
        assert parsed is not None
        assert "error" in parsed
        assert parsed["error"]["code"] in _STRUCTURED_ERROR_CODES
        assert parsed["error"]["code"] != "unknown_operation"


def test_route_table_serves_nothing_outside_registry(empty_engine):
    _open_call_session(empty_engine.port, empty_engine.tokens.control, 1_000_000)
    garbage_status, garbage_body, _, garbage_raw = _request(
        empty_engine.port,
        "POST",
        _ops_path(_GARBAGE_OPERATION),
        token=empty_engine.tokens.agent,
        body={},
    )
    assert garbage_status == 404
    assert garbage_body["error"]["code"] == "unknown_operation"

    for name in sorted(_FROZEN_OPERATION_NAMES):
        status, body, _, _ = _request(
            empty_engine.port,
            "POST",
            _ops_path(name),
            token=empty_engine.tokens.agent,
            body={},
        )
        assert status != garbage_status or body != garbage_body

    extra_status, extra_body, _, extra_raw = _request(
        empty_engine.port,
        "POST",
        _ops_path("crm.list_customers.extra"),
        token=empty_engine.tokens.agent,
        body={},
    )
    assert extra_raw == garbage_raw
    assert extra_body == garbage_body
    assert extra_status == garbage_status


@pytest.mark.parametrize("absent_name", _ABSENT_OPERATIONS)
def test_absent_surface_matches_garbage_operation(empty_engine, absent_name):
    _open_call_session(empty_engine.port, empty_engine.tokens.control, 1_000_000)
    garbage_status, garbage_body, _, garbage_raw = _request(
        empty_engine.port,
        "POST",
        _ops_path(_GARBAGE_OPERATION),
        token=empty_engine.tokens.agent,
        body={},
    )
    status, body, _, raw = _request(
        empty_engine.port,
        "POST",
        _ops_path(absent_name),
        token=empty_engine.tokens.agent,
        body={},
    )
    assert status == garbage_status
    assert body == garbage_body
    assert raw == garbage_raw


def test_pagination_is_stable_non_overlapping_and_complete(tmp_path):
    eng, conn = _build_empty_engine(
        tmp_path / "pagination", seed_customers=_PAGINATION_CUSTOMER_COUNT
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        first_run, first_cursors, _ = _page_all(
            eng.port, eng.tokens.agent, "crm.list_customers", limit=_PAGINATION_PAGE_SIZE
        )
        second_run, second_cursors, _ = _page_all(
            eng.port, eng.tokens.agent, "crm.list_customers", limit=_PAGINATION_PAGE_SIZE
        )
        assert first_run == second_run
        assert first_cursors == second_cursors

        seen: set[int] = set()
        ordered: list[int] = []
        for page in first_run:
            for row in page:
                row_id = int(row["id"])
                assert row_id not in seen
                seen.add(row_id)
                ordered.append(row_id)
        assert len(seen) == _PAGINATION_CUSTOMER_COUNT
        assert ordered == sorted(ordered)
        assert first_cursors[-1] is None
        assert all(cursor is not None for cursor in first_cursors[1:-1])
    finally:
        eng.stop()
        conn.close()


def test_pagination_rejects_malformed_cursor(tmp_path):
    eng, conn = _build_empty_engine(tmp_path / "bad-cursor", seed_customers=5)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.list_customers"),
            token=eng.tokens.agent,
            body={"cursor": "!!!not-valid!!!", "limit": 2},
        )
        assert status in {400, 422}
        assert body["error"]["code"] in {"validation_error", "invalid_request"}
    finally:
        eng.stop()
        conn.close()


def test_pagination_clamps_limit_to_config_max(tmp_path):
    eng, conn = _build_empty_engine(
        tmp_path / "limit-clamp", seed_customers=_PAGINATION_CLAMP_CUSTOMER_COUNT
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        max_limit = eng.config.get_int("interface.list_max_page_size")
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.list_customers"),
            token=eng.tokens.agent,
            body={"limit": max_limit + 500},
        )
        assert status == 200
        assert len(body["data"]) == max_limit
    finally:
        eng.stop()
        conn.close()


def test_pagination_default_limit_matches_config(tmp_path):
    default_limit = load_run_config().get_int("interface.list_default_page_size")
    eng, conn = _build_empty_engine(
        tmp_path / "default-limit", seed_customers=default_limit + 10
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        default_limit = eng.config.get_int("interface.list_default_page_size")
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.list_customers"),
            token=eng.tokens.agent,
            body={},
        )
        assert status == 200
        assert len(body["data"]) == default_limit
    finally:
        eng.stop()
        conn.close()


def test_pagination_rejects_out_of_range_cursor_values(tmp_path):
    eng, conn = _build_empty_engine(tmp_path / "range-cursor", seed_customers=5)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        overflow_cursor = encode_cursor(1 << 63)
        huge_cursor = encode_cursor(10**38)
        for cursor in (overflow_cursor, huge_cursor, encode_cursor(-1)):
            status, body, _, _ = _request(
                eng.port,
                "POST",
                _ops_path("crm.list_customers"),
                token=eng.tokens.agent,
                body={"cursor": cursor, "limit": 2},
            )
            assert status in {400, 422}
            assert body["error"]["code"] in {"validation_error", "invalid_request"}
            assert body["error"]["details"]["field"] == "cursor"
    finally:
        eng.stop()
        conn.close()


def test_pagination_rejects_zero_limit(tmp_path):
    eng, conn = _build_empty_engine(tmp_path / "zero-limit", seed_customers=5)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.list_customers"),
            token=eng.tokens.agent,
            body={"limit": 0},
        )
        assert status == 400
        assert body["error"]["code"] == "invalid_request"
    finally:
        eng.stop()
        conn.close()


def test_filtered_list_pages_follow_pk_order(tmp_path):
    eng, conn = _build_empty_engine(tmp_path / "filtered-order", seed_customers=0)
    now = 1_000_000
    conn.executemany(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (?, ?, 'commercial', 'active', ?, ?, 'run')",
        [
            (3, "Filterable Customer C", now, now),
            (1, "Filterable Customer A", now, now),
            (2, "Filterable Customer B", now, now),
        ],
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, 1_000_000)
        spec = OPERATIONS_BY_NAME["crm.list_customers"]
        sql, _ = build_view_list_sql(spec, {"name": "Filterable", "limit": 2}, limit=3)
        assert "ORDER BY id ASC" in sql
        pages, _, _ = _page_all(
            eng.port,
            eng.tokens.agent,
            "crm.list_customers",
            limit=1,
            extra={"name": "Filterable"},
        )
        ordered = [int(row["id"]) for page in pages for row in page]
        assert ordered == [1, 2, 3]
    finally:
        eng.stop()
        conn.close()


@pytest.mark.parametrize(
    "spec",
    [spec for spec in OPERATIONS if spec.mutation],
    ids=lambda spec: spec.name,
)
def test_mutation_dispatcher_fingerprint_matches_registry(spec):
    expected = {arg.name for arg in spec.args if arg.name != "request_id"}
    sample = {arg.name: "probe" for arg in spec.args}
    actual = set(_mutation_fingerprint_args(sample))
    assert actual == expected
    assert spec.name in _MUTATION_DISPATCH


def _perturbed_placeholder(arg_type: str, value: object) -> object:
    if arg_type == "int":
        return int(value) + 1
    if arg_type == "str":
        return str(value) + "-alt"
    if arg_type == "date":
        return "2026-10-02" if value != "2026-10-02" else "2026-10-03"
    if arg_type == "object":
        return {"alt": True}
    if arg_type == "list":
        return ["alt"]
    if arg_type == "bool":
        return not value
    raise AssertionError(f"unsupported arg type {arg_type!r}")


@pytest.mark.parametrize(
    "spec",
    [spec for spec in OPERATIONS if spec.mutation],
    ids=lambda spec: spec.name,
)
def test_mutation_fingerprint_discriminates_every_registry_arg(empty_engine, spec):
    _open_call_session(empty_engine.port, empty_engine.tokens.control, 1_000_000)
    base_body = _mutation_probe_body(spec)
    for arg in spec.args:
        if arg.name == "request_id":
            continue
        request_id = f"fp-discriminate-{spec.name}-{arg.name}"
        first_body = dict(base_body)
        first_body["request_id"] = request_id
        if arg.name not in first_body:
            first_body[arg.name] = _placeholder_for_type(arg.type)
        _request(
            empty_engine.port,
            "POST",
            _ops_path(spec.name),
            token=empty_engine.tokens.agent,
            body=first_body,
        )

        second_body = dict(first_body)
        second_body[arg.name] = _perturbed_placeholder(arg.type, first_body[arg.name])
        second_status, second_parsed, _, _ = _request(
            empty_engine.port,
            "POST",
            _ops_path(spec.name),
            token=empty_engine.tokens.agent,
            body=second_body,
        )
        assert second_status == 409, (spec.name, arg.name, second_parsed)
        assert second_parsed["error"]["code"] == "request_id_conflict"


def test_weather_get_is_read_only_on_audit_and_dump(warm_engine):
    eng, conn = warm_engine
    today = clock.now_service_date(conn)
    forecast_date = (date.fromisoformat(today) + timedelta(days=3)).isoformat()
    run_id = conn.execute("SELECT run_id FROM _run LIMIT 1").fetchone()[0]
    before_audit = conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]
    before_forecast_cache = conn.execute(
        "SELECT COUNT(*) c FROM _weather_forecast_cache"
    ).fetchone()["c"]
    before_hash = dump_hash(conn, run_id)
    for query_date, expected_kind in ((today, "observed"), (forecast_date, "forecast")):
        for _ in range(2):
            status, body, _, _ = _request(
                eng.port,
                "POST",
                _ops_path("weather.get"),
                token=eng.tokens.agent,
                body={"zone_id": 4, "date": query_date},
            )
            assert status == 200, body
            assert body["data"]["kind"] == expected_kind
    after_audit = conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]
    after_forecast_cache = conn.execute(
        "SELECT COUNT(*) c FROM _weather_forecast_cache"
    ).fetchone()["c"]
    after_hash = dump_hash(conn, run_id)
    assert after_audit == before_audit
    assert after_forecast_cache == before_forecast_cache
    assert after_hash == before_hash


def test_read_omits_request_id_when_client_does_not_supply_one(warm_engine):
    eng, conn = warm_engine
    today = clock.now_service_date(conn)
    status, body, _, raw1 = _request(
        eng.port,
        "POST",
        _ops_path("weather.get"),
        token=eng.tokens.agent,
        body={"zone_id": 4, "date": today},
    )
    assert status == 200, body
    assert "request_id" not in body["meta"]
    _, body2, _, raw2 = _request(
        eng.port,
        "POST",
        _ops_path("weather.get"),
        token=eng.tokens.agent,
        body={"zone_id": 4, "date": today},
    )
    assert raw2 == raw1
    assert body2 == body


def test_list_events_returns_all_rows_without_next_cursor(tmp_path):
    dir_path = tmp_path / "events"
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    config = geo_run_config()
    initialize_run(path, master_seed=1, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_engine_connection(path)
    _seed_ops_surface(conn)
    now = conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]
    appointment_id = 1
    conn.executemany(
        "INSERT INTO _appointment_events "
        "(appointment_id, technician_id, event_type, occurred_at, service_date, detail, created_at) "
        "VALUES (?, 1, 'dispatched', ?, '2026-10-01', 'seed', ?)",
        [(appointment_id, now + offset, now + offset) for offset in range(60)],
    )
    if conn.in_transaction:
        db.commit(conn)
    seeded_count = conn.execute(
        "SELECT COUNT(*) FROM _appointment_events WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()[0]
    assert seeded_count == 62
    rng = Rng(1, "1.0.0", audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    services = Services(
        conn=conn,
        rng=rng,
        ledger=Ledger(conn),
        invocations=Invocations(conn),
        events=Events(conn, rng),
    )
    eng = Engine(services=services, config=config, db_path=path, artifact=_ARTIFACT)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, now + 60)
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("appointments.list_events"),
            token=eng.tokens.agent,
            body={"appointment_id": appointment_id},
        )
        assert status == 200, body
        assert len(body["data"]) == 62
        assert "next_cursor" not in body["meta"]
    finally:
        eng.stop()
        conn.close()


@pytest.mark.parametrize(
    "spec",
    [
        spec
        for spec in OPERATIONS
        if spec.kind == "read" and spec.read and spec.read.kind == "view_list" and not spec.read.unpaginated
    ],
    ids=lambda spec: spec.name,
)
def test_list_read_matches_equivalent_sql(warm_engine, spec):
    eng, conn = warm_engine
    body = _warm_probe_body(conn, spec)
    status, parsed, _, _ = _request(
        eng.port, "POST", _ops_path(spec.name), token=eng.tokens.agent, body=body
    )
    assert status == 200, parsed
    default_limit = eng.config.get_int("interface.list_default_page_size")
    columns = _VIEW_COLUMNS[spec.read.view]
    pk_column = spec.read.pk_column
    all_rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM {spec.read.view} ORDER BY {pk_column} ASC"
    ).fetchall()
    all_dicts = [
        {name: (value.hex() if isinstance(value, bytes) else value) for name, value in zip(columns, row, strict=True)}
        for row in all_rows
    ]
    for flt in spec.read.filters:
        if flt.arg_name not in body:
            continue
        value = body[flt.arg_name]
        all_dicts = [row for row in all_dicts if _row_matches_filter_value(row, flt, spec, value)]
    expected = all_dicts[:default_limit]
    assert parsed["data"] == expected


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise AssertionError(f"unsupported literal type {type(value)!r}")


def _inline_sql_params(sql: str, params: list[object]) -> str:
    parts = sql.split("?")
    assert len(parts) - 1 == len(params)
    out = parts[0]
    for value, tail in zip(params, parts[1:], strict=True):
        out += _sql_literal(value) + tail
    return out


@pytest.mark.parametrize(
    "spec",
    [
        spec
        for spec in OPERATIONS
        if spec.kind == "read" and spec.read and spec.read.kind == "view_list"
    ],
    ids=lambda spec: spec.name,
)
def test_list_read_matches_query_sql_endpoint(warm_engine, spec):
    eng, conn = warm_engine
    body = _warm_probe_body(conn, spec)
    status, parsed, _, _ = _request(
        eng.port, "POST", _ops_path(spec.name), token=eng.tokens.agent, body=body
    )
    assert status == 200, parsed
    default_limit = eng.config.get_int("interface.list_default_page_size")
    limit = None if spec.read.unpaginated else default_limit + 1
    sql, params = build_view_list_sql(spec, body, limit=limit)
    literal_sql = _inline_sql_params(sql, params)
    sql_status, sql_body, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("query.sql"),
        token=eng.tokens.agent,
        body={"statement": literal_sql},
    )
    assert sql_status == 200, sql_body
    columns = sql_body["data"]["columns"]
    rows = sql_body["data"]["rows"]
    expected = [dict(zip(columns, row, strict=True)) for row in rows]
    if limit is not None:
        expected = expected[:default_limit]
    assert parsed["data"] == expected


@pytest.mark.parametrize(
    "spec",
    [spec for spec in OPERATIONS if spec.kind == "read"],
    ids=lambda spec: spec.name,
)
def test_read_response_columns_match_backing_view(warm_engine, spec):
    eng, conn = warm_engine
    body = _warm_probe_body(conn, spec)
    status, parsed, _, _ = _request(
        eng.port, "POST", _ops_path(spec.name), token=eng.tokens.agent, body=body
    )
    assert status == 200, parsed
    read = spec.read
    assert read is not None
    if read.kind == "sql":
        expected = {"columns", "rows", "row_count", "truncated"}
        assert set(parsed["data"]) == expected
        if parsed["data"]["rows"]:
            assert len(parsed["data"]["columns"]) == len(parsed["data"]["rows"][0])
        return
    if read.kind in {"weather", "maps"}:
        data = parsed["data"]
        assert set(data) <= set(spec.response_fields)
        assert set(data) >= set(spec.response_fields) - {"storm_severity"}
        return
    if read.kind == "view_singleton":
        expected_columns = set(read.singleton_fields or _VIEW_COLUMNS[read.view])
        assert set(spec.response_fields) == expected_columns
        assert set(parsed["data"]) == expected_columns
        return
    if read.kind == "view_get":
        expected_columns = set(_VIEW_COLUMNS[read.view])
        assert set(spec.response_fields) == expected_columns
        assert set(parsed["data"]) == expected_columns
        return
    if read.kind == "view_list":
        expected_columns = set(_VIEW_COLUMNS[read.view])
        assert set(spec.response_fields) == expected_columns
        assert isinstance(parsed["data"], list)
        assert parsed["data"], f"{spec.name} returned no rows on warm start"
        for row in parsed["data"]:
            assert set(row) == expected_columns


def _seed_cross_day_job_and_appointment(conn) -> None:
    now_ms = clock.now(conn)
    older_day_ms = now_ms - 40 * 86_400_000
    conn.execute(
        "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, job_type_id, "
        "business_unit_id, requested_service_name, status, priority, no_charge, summary, "
        "created_at, modified_at) VALUES "
        "(4, 'J-1004', 1, 1, 1, 1, 1, 'HVAC service', 'scheduled', 'normal', 0, 's', ?, ?)",
        (older_day_ms, older_day_ms),
    )
    conn.execute(
        "INSERT INTO _appointments (id, appointment_number, job_id, status, scheduled_start, "
        "scheduled_end, arrival_window_start, arrival_window_end, created_at, modified_at) "
        "VALUES (4, 'A-1004', 4, 'scheduled', ?, ?, ?, ?, ?, ?)",
        (
            older_day_ms, older_day_ms + 7_200_000,
            older_day_ms, older_day_ms + 7_200_000,
            older_day_ms, older_day_ms,
        ),
    )


@pytest.mark.parametrize(
    "spec",
    [
        spec
        for spec in OPERATIONS
        if spec.kind == "read" and spec.read and spec.read.kind == "view_list"
    ],
    ids=lambda spec: spec.name,
)
def test_list_read_filters_discriminate_on_seeded_data(warm_engine, spec):
    eng, conn = warm_engine
    read = spec.read
    assert read is not None
    if not read.filters:
        return
    if spec.name in {"jobs.list", "appointments.list"}:
        _seed_cross_day_job_and_appointment(conn)
    baseline_body = _warm_probe_body(conn, spec)
    max_limit = eng.config.get_int("interface.list_max_page_size")
    if read.kind == "view_list" and not read.unpaginated:
        baseline_body = dict(baseline_body)
        baseline_body["limit"] = max_limit
    baseline_status, baseline_parsed, _, _ = _request(
        eng.port, "POST", _ops_path(spec.name), token=eng.tokens.agent, body=baseline_body
    )
    assert baseline_status == 200, baseline_parsed
    baseline_rows = baseline_parsed["data"]
    assert baseline_rows, f"{spec.name} has no baseline rows"

    for flt in read.filters:
        if flt.required:
            alt_body = dict(baseline_body)
            if spec.name == "finance.list_ledger" and flt.arg_name in {"date_from", "date_to"}:
                alt_body["date_from"] = "2026-10-01"
                alt_body["date_to"] = "2026-10-01"
            elif spec.name == "appointments.list_events" and flt.arg_name == "appointment_id":
                alt_body["appointment_id"] = 2
            else:
                alt_value = _discover_alternate_filter_value(
                    conn, spec, flt, baseline_body.get(flt.arg_name)
                )
                alt_body[flt.arg_name] = alt_value
            alt_status, alt_parsed, _, _ = _request(
                eng.port,
                "POST",
                _ops_path(spec.name),
                token=eng.tokens.agent,
                body=alt_body,
            )
            assert alt_status == 200, alt_parsed
            assert alt_parsed["data"], f"{spec.name}.{flt.arg_name} alternate value matched no rows"
            assert alt_parsed["data"] != baseline_rows or len(baseline_rows) == 1
            continue
        if spec.name == "appointments.list" and flt.arg_name == "technician_id":
            value = _discover_filter_value(conn, spec, flt)
        elif flt.type == "date":
            value = _discover_date_filter_value(conn, spec, flt)
        else:
            value = _discriminating_filter_value(baseline_rows, spec, flt)
        assert value is not None, f"{spec.name}.{flt.arg_name} lacks a discriminating fixture value"
        filtered_body = dict(baseline_body)
        filtered_body[flt.arg_name] = value
        filtered_status, filtered_parsed, _, _ = _request(
            eng.port,
            "POST",
            _ops_path(spec.name),
            token=eng.tokens.agent,
            body=filtered_body,
        )
        assert filtered_status == 200, filtered_parsed
        filtered_rows = filtered_parsed["data"]
        assert filtered_rows, f"{spec.name}.{flt.arg_name} filtered value matched no rows"
        assert filtered_rows != baseline_rows or len(baseline_rows) == 1
        for row in filtered_rows:
            _assert_row_matches_filter(conn, row, spec, flt, value)


def test_weather_get_wire_conformance_over_ops(warm_engine):
    eng, conn = warm_engine
    today = clock.now_service_date(conn)
    today_d = date.fromisoformat(today)
    tomorrow = (today_d + timedelta(days=1)).isoformat()
    past = (today_d - timedelta(days=1)).isoformat()
    beyond = (today_d + timedelta(days=8)).isoformat()

    status, body, headers, raw1 = _request(
        eng.port,
        "POST",
        _ops_path("weather.get"),
        token=eng.tokens.agent,
        body={"zone_id": 4, "date": today, "request_id": "weather-wire-1"},
    )
    assert status == 200, body
    assert headers.get("Server") == "sunridge"
    data = body["data"]
    assert data["kind"] == "observed"
    assert data["horizon_days"] == 0
    allowed = (
        _WEATHER_ALLOWED_WITH_STORM if data["storm"] else _WEATHER_ALLOWED_WITHOUT_STORM
    )
    assert set(data) == allowed
    assert "condition_code" not in data
    assert "precipitation_mm" not in data

    status, forecast, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("weather.get"),
        token=eng.tokens.agent,
        body={"zone_id": 4, "date": tomorrow, "request_id": "weather-wire-2"},
    )
    assert status == 200
    assert forecast["data"]["kind"] == "forecast"
    assert forecast["data"]["horizon_days"] == 1

    for bad_date in (past, beyond):
        status, err, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("weather.get"),
            token=eng.tokens.agent,
            body={"zone_id": 4, "date": bad_date},
        )
        assert status == 422
        assert err["error"]["code"] == "validation_error"
        assert err["error"]["details"]["rule"] == "date_out_of_horizon"

    _, body2, _, raw2 = _request(
        eng.port,
        "POST",
        _ops_path("weather.get"),
        token=eng.tokens.agent,
        body={"zone_id": 4, "date": today, "request_id": "weather-wire-1"},
    )
    assert raw2 == raw1


def test_maps_distance_wire_conformance_over_ops(warm_engine):
    eng, _conn = warm_engine
    status, body, headers, raw1 = _request(
        eng.port,
        "POST",
        _ops_path("maps.distance"),
        token=eng.tokens.agent,
        body={
            "origin_address": _FIRST_ADDRESS.formatted_address,
            "destination_address": _SECOND_ADDRESS.formatted_address,
            "request_id": "maps-wire-1",
        },
    )
    assert status == 200, body
    assert headers.get("Server") == "sunridge"
    data = body["data"]
    assert set(data) == {
        "origin_address",
        "destination_address",
        "distance_km",
        "distance_miles",
        "dataset_version",
    }
    assert data["origin_address"] == _FIRST_ADDRESS.formatted_address
    assert data["destination_address"] == _SECOND_ADDRESS.formatted_address
    assert data["distance_km"] == round(data["distance_km"], 1)
    assert data["distance_miles"] == round(data["distance_miles"], 1)
    assert Decimal(str(data["distance_km"])) == Decimal(str(data["distance_km"])).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )

    status, err, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("maps.distance"),
        token=eng.tokens.agent,
        body={
            "origin_address": "99999 Nonexistent Blvd, Tempe, AZ 85282",
            "destination_address": _SECOND_ADDRESS.formatted_address,
        },
    )
    assert status == 422
    assert err["error"]["code"] == "validation_error"
    assert err["error"]["details"]["rule"] == "address_not_resolvable"

    _, body2, _, raw2 = _request(
        eng.port,
        "POST",
        _ops_path("maps.distance"),
        token=eng.tokens.agent,
        body={
            "origin_address": _FIRST_ADDRESS.formatted_address,
            "destination_address": _SECOND_ADDRESS.formatted_address,
            "request_id": "maps-wire-1",
        },
    )
    assert raw2 == raw1
    assert body2 == body


def _set_run_status(conn, status: RunStatus) -> None:
    conn.execute("UPDATE _run SET status = ?", (status.value,))


def test_ops_read_rejects_a_terminal_run_with_a_still_active_invocation(tmp_path):
    eng, conn = _build_seeded_engine(tmp_path / "terminal-read")
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, clock.now(conn))
        _set_run_status(conn, RunStatus.BANKRUPT)
        status, body, _, _ = _request(
            eng.port, "POST", _ops_path("crm.list_customers"), token=eng.tokens.agent, body={}
        )
        assert status == 409, body
        assert body["error"]["code"] == "run_not_active"
    finally:
        eng.stop()
        conn.close()


def test_sql_rejects_a_terminal_run_with_a_still_active_invocation(tmp_path):
    eng, conn = _build_seeded_engine(tmp_path / "terminal-sql")
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, clock.now(conn))
        _set_run_status(conn, RunStatus.BANKRUPT)
        status, body, _, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
        )
        assert status == 409, body
        assert body["error"]["code"] == "run_not_active"
    finally:
        eng.stop()
        conn.close()


def test_ops_mutation_checks_run_status_before_argument_validation(tmp_path):
    eng, conn = _build_seeded_engine(tmp_path / "terminal-mutation-order")
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, clock.now(conn))
        _set_run_status(conn, RunStatus.BANKRUPT)
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("calls.reject"),
            token=eng.tokens.agent,
            body={"call_id": "not-an-int", "request_id": "terminal-order-1"},
        )
        assert status == 409, body
        assert body["error"]["code"] == "run_not_active"
    finally:
        eng.stop()
        conn.close()


def test_maps_distance_hidden_unusable_address_matches_nonexistent_address_byte_for_byte(
    warm_engine,
):
    from trades_bench import address as address_module

    eng, conn = warm_engine
    base = conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT 1"
    ).fetchone()
    hidden_text = "8888 Hidden Fixture Rd, Tempe, AZ 85281"
    normalized = address_module.normalize(hidden_text, _ARTIFACT.abbreviation_table)
    conn.execute(
        "INSERT INTO _address_catalog (dataset_version, house_number, street, city, state, "
        "postal_code, formatted_address, canonical_key, latitude, longitude, zone_id, "
        "effective_zone_id, anchor_id, access_distance_km, segment_eligibility, status, "
        "unusable_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'unusable', 'test fixture', ?)",
        (
            base["dataset_version"], normalized.house_number, normalized.street,
            normalized.city, normalized.state, normalized.postal_code,
            normalized.formatted_address, normalized.canonical_key,
            base["latitude"], base["longitude"], base["zone_id"], base["effective_zone_id"],
            base["anchor_id"], base["access_distance_km"], base["segment_eligibility"],
            clock.now(conn),
        ),
    )
    nonexistent_text = "99999 Nonexistent Blvd, Tempe, AZ 85282"

    status_hidden, body_hidden, _, raw_hidden = _request(
        eng.port,
        "POST",
        _ops_path("maps.distance"),
        token=eng.tokens.agent,
        body={
            "origin_address": hidden_text,
            "destination_address": _SECOND_ADDRESS.formatted_address,
        },
    )
    status_absent, body_absent, _, raw_absent = _request(
        eng.port,
        "POST",
        _ops_path("maps.distance"),
        token=eng.tokens.agent,
        body={
            "origin_address": nonexistent_text,
            "destination_address": _SECOND_ADDRESS.formatted_address,
        },
    )
    assert status_hidden == status_absent == 422
    assert body_hidden["error"]["details"]["rule"] == "address_not_resolvable"
    assert raw_hidden == raw_absent


@pytest.mark.parametrize("bad_date", ["20261001", "2026-W40-4"])
def test_date_filter_rejects_non_canonical_iso_spellings(warm_engine, bad_date):
    eng, conn = warm_engine
    status, body, _, _ = _request(
        eng.port, "POST", _ops_path("calls.list"), token=eng.tokens.agent, body={"date": bad_date}
    )
    assert status == 400, (bad_date, body)
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "date"


@pytest.mark.parametrize("bad_cursor", ["!!!!MQ", "MQ==", "MDE"])
def test_cursor_rejects_junk_padded_and_non_canonical_encodings(warm_engine, bad_cursor):
    eng, conn = warm_engine
    status, body, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("crm.list_customers"),
        token=eng.tokens.agent,
        body={"cursor": bad_cursor},
    )
    assert status == 400, (bad_cursor, body)
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "cursor"


def test_oversized_int_arguments_are_invalid_request_not_a_crash(warm_engine):
    eng, conn = warm_engine
    huge = 10**100

    status, body, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("crm.get_customer"),
        token=eng.tokens.agent,
        body={"customer_id": huge},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    status, body, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("crm.list_locations"),
        token=eng.tokens.agent,
        body={"customer_id": -huge},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"

    status, body, _, _ = _request(
        eng.port,
        "POST",
        _ops_path("calls.reject"),
        token=eng.tokens.agent,
        body={"call_id": huge, "request_id": "oversized-int-mutation"},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"


def test_contacts_argument_rejects_a_dict_not_only_lists(tmp_path):
    eng, conn = _build_seeded_engine(tmp_path / "contacts-dict")
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, clock.now(conn))
        call_id = _seed_active_call(conn, eng.services.rng)
        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.create_customer"),
            token=eng.tokens.agent,
            body={
                "call_id": call_id,
                "name": "Dict Contacts",
                "segment": "residential",
                "contacts": {"contact_type": "phone", "value": "4805551234"},
                "request_id": "contacts-dict-customer",
            },
        )
        assert status == 400, body
        assert body["error"]["code"] == "invalid_request"

        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.create_location"),
            token=eng.tokens.agent,
            body={
                "call_id": call_id,
                "customer_id": 1,
                "address": _FIRST_ADDRESS.formatted_address,
                "contacts": {"contact_type": "phone", "value": "4805551234"},
                "request_id": "contacts-dict-location",
            },
        )
        assert status == 400, body
        assert body["error"]["code"] == "invalid_request"
    finally:
        eng.stop()
        conn.close()


def test_validate_operation_identity_prohibits_the_workforce_namespace():
    base = OPERATIONS_BY_NAME["crm.get_customer"]
    smuggled = dataclasses.replace(
        base, namespace="workforce", operation="get_customer", name="workforce.get_customer"
    )
    with pytest.raises(AssertionError):
        validate_operation_identity(smuggled)


def test_validate_operation_identity_requires_name_equals_namespace_dot_operation():
    base = OPERATIONS_BY_NAME["crm.get_customer"]
    mismatched = dataclasses.replace(base, name="crm.not_the_operation")
    with pytest.raises(AssertionError):
        validate_operation_identity(mismatched)


def test_mutation_happy_path_replay_and_validation_over_ops(tmp_path):
    eng, conn = _build_seeded_engine(tmp_path / "mutation")
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, clock.now(conn))
        call_id = _seed_active_call(conn, eng.services.rng)
        status, bad_arg, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("calls.reject"),
            token=eng.tokens.agent,
            body={"call_id": call_id, "request_id": "ops-reject-replay-2", "extra": 1},
        )
        assert status == 400
        assert bad_arg["error"]["code"] == "invalid_request"

        request_id = "ops-reject-replay-1"
        status, body, headers, raw1 = _request(
            eng.port,
            "POST",
            _ops_path("calls.reject"),
            token=eng.tokens.agent,
            body={"call_id": call_id, "request_id": request_id},
        )
        assert status == 200, body
        assert body.keys() == {"data", "meta"}
        assert body["meta"].keys() == {"as_of", "invocation_id", "request_id", "spec_version"}
        assert body["meta"]["as_of"].endswith("-07:00")
        assert body["data"]["call_id"] == call_id
        assert body["data"]["outcome"] == "rejected"
        assert "message" in body["data"]

        status, body2, headers2, raw2 = _request(
            eng.port,
            "POST",
            _ops_path("calls.reject"),
            token=eng.tokens.agent,
            body={"call_id": call_id, "request_id": request_id},
        )
        assert status == 200
        assert raw2 == raw1
        assert body2 == body
        assert headers2.get("X-CSR-Idempotent-Replay") == "1"

        status, conflict, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("calls.reject"),
            token=eng.tokens.agent,
            body={"call_id": call_id + 1, "request_id": request_id},
        )
        assert status == 409
        assert conflict["error"]["code"] == "request_id_conflict"
    finally:
        eng.stop()
        conn.close()


def test_remaining_mutations_happy_path_over_ops(tmp_path):
    eng, conn = _build_happy_path_engine(tmp_path / "mutation-chain")
    eng.start()
    try:
        rng = eng.services.rng
        _open_call_session(eng.port, eng.tokens.control, clock.now(conn))
        new_job_call_id = _seed_active_call(conn, rng)

        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.create_customer"),
            token=eng.tokens.agent,
            body={
                "call_id": new_job_call_id,
                "name": "Happy Path Customer",
                "segment": "residential",
                "contacts": [{"contact_type": "phone", "value": "4805551234"}],
                "request_id": "happy-create-customer",
            },
        )
        assert status == 200, body
        customer_id = body["data"]["customer_id"]

        call_address = conn.execute(
            "SELECT ac.id, ac.formatted_address FROM _calls c "
            "JOIN _address_catalog ac ON ac.id = c.address_catalog_id WHERE c.id = ?",
            (new_job_call_id,),
        ).fetchone()

        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("crm.create_location"),
            token=eng.tokens.agent,
            body={
                "call_id": new_job_call_id,
                "customer_id": customer_id,
                "address": call_address["formatted_address"],
                "request_id": "happy-create-location",
            },
        )
        assert status == 200, body
        location_id = body["data"]["location_id"]

        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("jobs.book"),
            token=eng.tokens.agent,
            body={
                "call_id": new_job_call_id,
                "customer_id": customer_id,
                "location_id": location_id,
                "job_type_id": 1,
                "service_date": "2026-10-02",
                "block_start": "09:00",
                "technician_id": 1,
                "request_id": "happy-book",
            },
        )
        assert status == 200, body
        assert body["data"]["outcome"] == "accepted"
        appointment_id = body["data"]["appointment_id"]
        assert appointment_id is not None
        assert body["data"]["assignment_id"] is not None

        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("dispatch.assign"),
            token=eng.tokens.agent,
            body={
                "appointment_id": appointment_id,
                "technician_id": 2,
                "request_id": "happy-assign",
            },
        )
        assert status == 200, body
        assert body["data"]["technician_id"] == 2
        assert body["data"]["previous_technician_id"] == 1

        cancel_call_id, _cancel_now = _manufacture_cancellation_call(
            conn,
            rng,
            customer_id=customer_id,
            location_id=location_id,
            address_catalog_id=int(call_address["id"]),
            target_appointment_id=appointment_id,
        )

        status, body, _, _ = _request(
            eng.port,
            "POST",
            _ops_path("appointments.acknowledge_cancellation"),
            token=eng.tokens.agent,
            body={"call_id": cancel_call_id, "request_id": "happy-ack-cancel"},
        )
        assert status == 200, body
        assert body["data"]["outcome"] == "acknowledged"
        assert body["data"]["appointment_id"] == appointment_id
        assert body["data"]["released_blocks"]
    finally:
        eng.stop()
        conn.close()


def test_sunridge_surface_has_no_bench_leaks(warm_engine):
    eng, conn = warm_engine
    samples = [
        ("crm.list_customers", {}),
        ("finance.get_cash_position", {}),
        ("weather.get", {"zone_id": 1, "date": clock.now_service_date(conn)}),
        (
            "maps.distance",
            {
                "origin_address": _FIRST_ADDRESS.formatted_address,
                "destination_address": _SECOND_ADDRESS.formatted_address,
            },
        ),
        (_GARBAGE_OPERATION, {}),
    ]
    for op_name, body in samples:
        status, parsed, headers, raw = _request(
            eng.port,
            "POST",
            _ops_path(op_name),
            token=eng.tokens.agent,
            body=body,
        )
        assert headers.get("Server") == "sunridge"
        serialized = raw.decode("utf-8").lower()
        assert "trades_bench" not in serialized
        assert "bench" not in serialized
        assert status in {200, 404, 422}
