from __future__ import annotations

import json
from datetime import date
from itertools import count
from types import SimpleNamespace

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    audit_count,
    geo_artifact_sha256,
)
from csr_bench import appointments, booking, calls, clock, crm, db
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.enums import EventKind
from csr_bench.errors import ErrorCode
from csr_bench.events import Events
from csr_bench.run_init import initialize_run, open_run_db

SEED = 29
END_DATE = "2026-10-05"
SERVICE_DATE = "2026-10-02"

_ACCEPT_ALWAYS = {
    "acceptance.base_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
    "acceptance.floor_probability_by_urgency": {"standard": 1.0, "urgent": 1.0},
}

_request_seq = count(1)
_arrival_seq = count(0)

_COMMAND_MODULES = (booking, calls, crm, appointments)
_COVERED_COMMANDS = frozenset({
    "booking.book_command",
    "calls.reject_command",
    "crm.create_customer_command",
    "crm.create_location_command",
    "appointments.assign_command",
    "appointments.acknowledge_cancellation_command",
})

_WATCHED_TABLES = (
    "_customers", "_customer_contacts", "_customer_repeat_state",
    "_locations", "_location_contacts",
    "_calls", "_call_hidden",
    "_jobs", "_appointments", "_appointment_blocks", "_appointment_assignments",
    "_appointment_events", "_technician_block_holds", "_job_outcomes",
    "_cancellation_schedule", "_ledger_entries", "_notifications", "_events",
)

_BAD_ID_VALUES = (1.5, "1", True, None)
_BAD_ID_IDS = ("float", "str", "bool", "none")

_NON_CANONICAL_DATES = ("20261002", "2026-1-2", "2026-10-02 ", "garbage")


def _week_spelling(d: date) -> str:
    week = d.isocalendar()
    return f"{week.year}-W{week.week:02d}-{week.weekday}"


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": END_DATE,
        **_ACCEPT_ALWAYS,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _now_ms(conn) -> int:
    return conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]


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
        "service_date": SERVICE_DATE,
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


def _arrival_at():
    return clock.phoenix_instant(2026, 10, 1, 9, 0) + next(_arrival_seq) * 60_000


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


def _registered_call(conn, controller, *, customer_id, location_id):
    now = _now_ms(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        customer_id=customer_id, location_id=location_id,
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


def _phone_contact(value="4805551234"):
    return {"contact_type": "phone", "value": value}


def _next_request_id(prefix: str) -> str:
    return f"surface.{prefix}.{next(_request_seq)}"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("surface-hardening")
    cfg = _config()
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=SEED, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    now = _now_ms(conn)
    _open_invocation(conn, now, invocation_type="call")

    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    call_id = _registered_call(conn, controller, customer_id=customer_id, location_id=location_id)

    yield SimpleNamespace(
        conn=conn,
        cfg=cfg,
        controller=controller,
        call_id=call_id,
        customer_id=customer_id,
        location_id=location_id,
        address=catalog["formatted_address"],
    )

    conn.close()


def _world_snapshot(conn):
    counts = {
        table: conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        for table in _WATCHED_TABLES
    }
    return (audit_count(conn), counts)


def _rng_snapshot(conn):
    rows = conn.execute(
        "SELECT stream_name, seed, counter FROM _rng_state ORDER BY stream_name"
    ).fetchall()
    return [tuple(row) for row in rows]


def _err(body: dict) -> dict:
    assert body["ok"] is False, body
    return body["error"]


def _book_kwargs(world):
    return {
        "call_id": world.call_id, "customer_id": world.customer_id,
        "location_id": world.location_id,
        "job_type_id": 1, "service_date": SERVICE_DATE, "block_start": "09:00",
        "technician_id": 1,
    }


def _reject_kwargs(world):
    return {"call_id": world.call_id}


def _create_customer_kwargs(world):
    return {
        "call_id": world.call_id, "name": "Jane Doe", "segment": "residential",
        "contacts": [_phone_contact()],
    }


def _create_location_kwargs(world):
    return {
        "call_id": world.call_id, "customer_id": world.customer_id, "address": world.address,
        "contacts": None,
    }


def _assign_kwargs(world):
    return {"appointment_id": 1, "technician_id": 1}


def _acknowledge_cancellation_kwargs(world):
    return {"call_id": world.call_id}


_ID_CASES = (
    ("booking.book_command", booking.book_command, _book_kwargs, "call_id"),
    ("booking.book_command", booking.book_command, _book_kwargs, "customer_id"),
    ("booking.book_command", booking.book_command, _book_kwargs, "location_id"),
    ("booking.book_command", booking.book_command, _book_kwargs, "job_type_id"),
    ("booking.book_command", booking.book_command, _book_kwargs, "technician_id"),
    ("calls.reject_command", calls.reject_command, _reject_kwargs, "call_id"),
    (
        "crm.create_customer_command", crm.create_customer_command,
        _create_customer_kwargs, "call_id",
    ),
    (
        "crm.create_location_command", crm.create_location_command,
        _create_location_kwargs, "call_id",
    ),
    (
        "crm.create_location_command", crm.create_location_command,
        _create_location_kwargs, "customer_id",
    ),
    (
        "appointments.assign_command", appointments.assign_command,
        _assign_kwargs, "appointment_id",
    ),
    (
        "appointments.assign_command", appointments.assign_command,
        _assign_kwargs, "technician_id",
    ),
    (
        "appointments.acknowledge_cancellation_command",
        appointments.acknowledge_cancellation_command,
        _acknowledge_cancellation_kwargs, "call_id",
    ),
)


@pytest.mark.parametrize("case", _ID_CASES, ids=[f"{c[0]}:{c[3]}" for c in _ID_CASES])
@pytest.mark.parametrize("bad_value", _BAD_ID_VALUES, ids=_BAD_ID_IDS)
def test_int_id_arguments_reject_non_canonical_types(world, case, bad_value):
    label, fn, kwargs_factory, target = case
    kwargs = kwargs_factory(world)
    kwargs[target] = bad_value

    before_world = _world_snapshot(world.conn)
    before_rng = _rng_snapshot(world.conn)

    response = fn(world.controller.services, _next_request_id(label), world.cfg, **kwargs)
    body = json.loads(response)
    error = _err(body)

    assert error["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert target in error["message"]

    assert _world_snapshot(world.conn) == before_world
    assert _rng_snapshot(world.conn) == before_rng


@pytest.mark.parametrize("bad_date", _NON_CANONICAL_DATES, ids=[repr(v) for v in _NON_CANONICAL_DATES])
def test_service_date_argument_rejects_non_canonical_spellings(world, bad_date):
    kwargs = _book_kwargs(world)
    kwargs["service_date"] = bad_date

    before_world = _world_snapshot(world.conn)
    before_rng = _rng_snapshot(world.conn)

    response = booking.book_command(
        world.controller.services, _next_request_id("book_command.date"), world.cfg, **kwargs
    )
    body = json.loads(response)
    error = _err(body)

    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "invalid_service_date"

    assert _world_snapshot(world.conn) == before_world
    assert _rng_snapshot(world.conn) == before_rng


def test_service_date_week_spelling_is_rejected(world):
    target = date.fromisoformat(SERVICE_DATE)
    week_spelling = _week_spelling(target)
    assert date.fromisoformat(week_spelling) == target

    kwargs = _book_kwargs(world)
    kwargs["service_date"] = week_spelling

    before_world = _world_snapshot(world.conn)
    before_rng = _rng_snapshot(world.conn)

    response = booking.book_command(
        world.controller.services, _next_request_id("book_command.week"), world.cfg, **kwargs
    )
    body = json.loads(response)
    error = _err(body)

    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "invalid_service_date"

    assert _world_snapshot(world.conn) == before_world
    assert _rng_snapshot(world.conn) == before_rng


def test_battery_covers_every_public_command_function():
    discovered = set()
    for module in _COMMAND_MODULES:
        for name in module.__all__:
            if name.endswith("_command"):
                discovered.add(f"{module.__name__.rsplit('.', 1)[-1]}.{name}")

    battery_covered = {case[0] for case in _ID_CASES}

    assert discovered == _COVERED_COMMANDS
    assert battery_covered == _COVERED_COMMANDS
