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
from csr_bench import address as address_mod
from csr_bench import calls, clock, crm, db, demand, geo_reader
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.enums import EventKind
from csr_bench.errors import ErrorCode
from csr_bench.events import Events
from csr_bench.run_init import initialize_run, open_run_db

SEED = 7
START_DATE = "2026-10-01"
END_DATE = "2026-10-02"

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
_ABBR_TABLE = address_mod.load_default_abbreviation_table()
_SUFFIX_LONG_BY_SHORT = {short.upper(): long for long, short in _ABBR_TABLE["suffixes"].items()}

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


def _open_invocation(conn, now, invocation_type="call"):
    conn.execute(
        "INSERT INTO _invocations (type, ts_ms, sequence, status) "
        "VALUES (?, ?, "
        "(SELECT COALESCE(MAX(sequence), 0) + 1 FROM _invocations), 'active')",
        (invocation_type, now),
    )


def _open_call_invocation(conn, now):
    _open_invocation(conn, now, invocation_type="call")


def _usable_catalog_rows(conn, count_):
    rows = conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT ?",
        (count_,),
    ).fetchall()
    assert len(rows) == count_
    return rows


def _insert_pending(conn, now, arrival_at, **overrides):
    fields = {
        "service_date": START_DATE,
        "zone_id": None,
        "arrival_at": arrival_at,
        "call_kind": "new_job",
        "channel": "new_caller",
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


def _arrive(conn, rng, pending_id, arrival_at):
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


_arrival_seq = count(0)


def _arrival_at():
    return clock.phoenix_instant(2026, 10, 1, 9, 0) + next(_arrival_seq) * 60_000


def _new_caller_call(conn, controller, *, catalog, segment="residential", prospect_id=None):
    now = clock.now(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        segment=segment, address_catalog_id=catalog["id"], prospect_id=prospect_id,
    )
    return _arrive(conn, controller.services.rng, pending_id, arrival_at)


def _known_customer_new_address_call(conn, controller, *, customer_id, catalog, segment="residential"):
    now = clock.now(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        segment=segment, channel="existing_customer",
        customer_id=customer_id, address_catalog_id=catalog["id"],
    )
    return _arrive(conn, controller.services.rng, pending_id, arrival_at)


def _known_customer_known_location_call(conn, controller, *, customer_id, location_id, segment="residential"):
    now = clock.now(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        segment=segment, channel="existing_customer",
        customer_id=customer_id, location_id=location_id,
    )
    return _arrive(conn, controller.services.rng, pending_id, arrival_at)


def _insert_customer(conn, now, segment="residential"):
    cur = conn.execute(
        "INSERT INTO _customers (name, segment, status, created_at, modified_at) "
        "VALUES ('Fixture Customer', ?, 'active', ?, ?)",
        (segment, now, now),
    )
    return int(cur.lastrowid)


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
    conn, now, *, status="active", call_kind="new_job", customer_id=None,
    location_id=None, segment="residential", address_catalog_id=None,
):
    cur = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, segment, customer_id, location_id, requested_service_name, urgent, "
        "address_catalog_id, created_at) VALUES (?, ?, ?, ?, 'Fixture Caller', '4805551234', "
        "?, ?, ?, 'HVAC service', 0, ?, ?)",
        (now, START_DATE, call_kind, status, segment, customer_id, location_id, address_catalog_id, now),
    )
    return int(cur.lastrowid)


def _insert_prospect(conn, now, *, propensity_micros, last_outcome="rejected", segment="residential"):
    cur = conn.execute(
        "INSERT INTO _prospect_state (identity_key, caller_name, caller_phone, segment, "
        "home_zone_id, propensity_micros, last_outcome, competitor_captured_at, created_at, "
        "updated_at) VALUES ('prospect-fixture', 'Fixture Caller', '4805551234', ?, NULL, ?, "
        "?, NULL, ?, ?)",
        (segment, propensity_micros, last_outcome, now, now),
    )
    return int(cur.lastrowid)


def _phone_contact(value="480-555-0123"):
    return {"contact_type": "phone", "value": value}


def _create_customer(controller, cfg, **kwargs):
    request_id = f"create_customer.{next(_request_seq)}"
    response = crm.create_customer_command(controller.services, request_id, cfg, **kwargs)
    return json.loads(response)


def _create_location(controller, cfg, **kwargs):
    request_id = f"create_location.{next(_request_seq)}"
    response = crm.create_location_command(
        controller.services, request_id, cfg, artifact=ARTIFACT, **kwargs
    )
    return json.loads(response)


def _ok(body: dict) -> dict:
    assert body["ok"] is True, body
    return body["result"]


def _err(body: dict) -> dict:
    assert body["ok"] is False, body
    return body["error"]


def _rng_snapshot(conn):
    rows = conn.execute(
        "SELECT stream_name, seed, counter FROM _rng_state ORDER BY stream_name"
    ).fetchall()
    return [tuple(row) for row in rows]


def _abbreviation_variant_address(catalog) -> str:
    street_tokens = catalog["street"].split()
    long_form = _SUFFIX_LONG_BY_SHORT.get(street_tokens[-1].upper())
    assert long_form is not None, f"no long form found for suffix {street_tokens[-1]!r}"
    street_tokens[-1] = long_form
    street = " ".join(street_tokens)
    return (
        f"{catalog['house_number']} {street}, {catalog['city']}, "
        f"{catalog['state']} {catalog['postal_code']}"
    )


def _with_unit_address(catalog, unit_token: str = "Unit 5") -> str:
    return (
        f"{catalog['house_number']} {catalog['street']} {unit_token}, {catalog['city']}, "
        f"{catalog['state']} {catalog['postal_code']}"
    )


def test_happy_path_new_caller_registration(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")

    contacts = [_phone_contact()]
    body = _create_customer(
        controller, cfg,
        call_id=call_id, name="Jane Doe", segment="residential", contacts=contacts,
    )
    result = _ok(body)
    customer_id = result["customer_id"]

    customer = conn.execute(
        "SELECT * FROM _customers WHERE id = ?", (customer_id,)
    ).fetchone()
    assert customer["name"] == "Jane Doe"
    assert customer["segment"] == "residential"
    assert customer["status"] == "active"
    assert customer["created_from_call_id"] == call_id
    assert customer["origin"] == "run"

    contact_rows = conn.execute(
        "SELECT * FROM _customer_contacts WHERE customer_id = ?", (customer_id,)
    ).fetchall()
    assert len(contact_rows) == 1
    assert contact_rows[0]["contact_type"] == "phone"
    assert contact_rows[0]["value"] == "480-555-0123"
    assert contact_rows[0]["do_not_text"] == 0
    assert contact_rows[0]["memo"] == ""

    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["customer_id"] == customer_id

    repeat = conn.execute(
        "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    expected_micros = demand._to_micros(
        cfg.resolved["demand.repeat_propensity_initial_by_segment"]["residential"]
    )
    assert repeat["propensity_micros"] == expected_micros
    assert repeat["last_outcome"] is None

    location_body = _create_location(
        controller, cfg,
        call_id=call_id, customer_id=customer_id, address=catalog["formatted_address"],
    )
    location_result = _ok(location_body)
    location_id = location_result["location_id"]

    location = conn.execute(
        "SELECT * FROM _locations WHERE id = ?", (location_id,)
    ).fetchone()
    expected_street = f"{catalog['house_number']} {catalog['street']}"
    assert location["street"] == expected_street
    assert location["name"] == expected_street
    assert location["unit"] == ""
    assert location["latitude"] == catalog["latitude"]
    assert location["longitude"] == catalog["longitude"]
    assert location["zone_id"] == catalog["zone_id"]
    assert location["effective_zone_id"] == catalog["effective_zone_id"]
    assert location["address_catalog_id"] == catalog["id"]

    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["location_id"] == location_id


def test_abbreviation_variant_and_unit_still_resolve(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)

    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    _ok(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    variant_address = _abbreviation_variant_address(catalog)
    result = _ok(
        _create_location(controller, cfg, call_id=call_id, customer_id=1, address=variant_address)
    )
    assert result["location_id"]

    call_id_2 = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    customer_id_2 = _ok(
        _create_customer(
            controller, cfg,
            call_id=call_id_2, name="John Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]
    unit_address = _with_unit_address(catalog)
    result_2 = _ok(
        _create_location(
            controller, cfg, call_id=call_id_2, customer_id=customer_id_2, address=unit_address
        )
    )
    location_2 = conn.execute(
        "SELECT * FROM _locations WHERE id = ?", (result_2["location_id"],)
    ).fetchone()
    assert location_2["unit"] == "Unit 5"
    assert location_2["address_catalog_id"] == catalog["id"]


def test_segment_mismatch_blocks_registration_with_no_state_change(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")

    error = _err(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="commercial",
            contacts=[_phone_contact()],
        )
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "segment_mismatch"

    assert conn.execute("SELECT COUNT(*) c FROM _customers").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _customer_contacts").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _customer_repeat_state").fetchone()["c"] == 0
    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["customer_id"] is None


def test_address_mismatch_and_unresolvable_address(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog, other_catalog = _usable_catalog_rows(conn, 2)
    _open_call_invocation(conn, now)

    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    customer_id = _ok(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]

    mismatch_error = _err(
        _create_location(
            controller, cfg, call_id=call_id, customer_id=customer_id,
            address=other_catalog["formatted_address"],
        )
    )
    assert mismatch_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert mismatch_error["details"]["reason"] == "address_mismatch"
    assert conn.execute("SELECT COUNT(*) c FROM _locations").fetchone()["c"] == 0
    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["location_id"] is None

    garbage_error = _err(
        _create_location(
            controller, cfg, call_id=call_id, customer_id=customer_id,
            address="999999 Nowhere Trail, Tempe, AZ 85282",
        )
    )
    assert garbage_error["code"] == ErrorCode.ADDRESS_UNRESOLVABLE.value


def test_q30_existing_callers_cannot_be_registered(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    existing_customer_id = _insert_customer(conn, now, segment="residential")
    _open_call_invocation(conn, now)

    known_call_id = _known_customer_new_address_call(
        conn, controller, customer_id=existing_customer_id, catalog=catalog,
    )
    error = _err(
        _create_customer(
            controller, cfg,
            call_id=known_call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert error["details"]["reason"] == "already_registered"

    new_call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    new_customer_id = _ok(
        _create_customer(
            controller, cfg,
            call_id=new_call_id, name="John Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]
    second_error = _err(
        _create_customer(
            controller, cfg,
            call_id=new_call_id, name="Someone Else", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert second_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert second_error["details"]["reason"] == "already_registered"
    assert conn.execute("SELECT COUNT(*) c FROM _customers").fetchone()["c"] == 2
    assert new_customer_id != existing_customer_id


def test_registration_ordering_rules(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)

    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    early_error = _err(
        _create_location(
            controller, cfg, call_id=call_id, customer_id=1, address=catalog["formatted_address"],
        )
    )
    assert early_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert early_error["details"]["reason"] == "customer_not_registered"

    customer_id = _ok(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]
    wrong_customer_id = customer_id + 999
    mismatch_error = _err(
        _create_location(
            controller, cfg, call_id=call_id, customer_id=wrong_customer_id,
            address=catalog["formatted_address"],
        )
    )
    assert mismatch_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert mismatch_error["details"]["reason"] == "customer_mismatch"

    known_customer_id = _insert_customer(conn, now, segment="residential")
    known_location_id = _insert_location(conn, now, known_customer_id, catalog)
    known_call_id = _known_customer_known_location_call(
        conn, controller, customer_id=known_customer_id, location_id=known_location_id,
    )
    already_error = _err(
        _create_location(
            controller, cfg, call_id=known_call_id, customer_id=known_customer_id,
            address=catalog["formatted_address"],
        )
    )
    assert already_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert already_error["details"]["reason"] == "already_registered"


def test_prospect_state_transfers_without_reset(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    distinctive_micros = 2_345_678
    prospect_id = _insert_prospect(
        conn, now, propensity_micros=distinctive_micros, last_outcome="rejected",
        segment="residential",
    )
    _open_call_invocation(conn, now)
    call_id = _new_caller_call(
        conn, controller, catalog=catalog, segment="residential", prospect_id=prospect_id,
    )

    hidden = conn.execute(
        "SELECT prospect_id FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    assert hidden["prospect_id"] == prospect_id

    customer_id = _ok(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]

    repeat = conn.execute(
        "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    assert repeat["propensity_micros"] == distinctive_micros
    assert repeat["last_outcome"] == "rejected"

    prospect_row = conn.execute(
        "SELECT * FROM _prospect_state WHERE id = ?", (prospect_id,)
    ).fetchone()
    assert prospect_row is not None
    assert prospect_row["propensity_micros"] == distinctive_micros


def test_stale_call_from_earlier_invocation_is_not_reachable(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog, catalog_2 = _usable_catalog_rows(conn, 2)
    _open_call_invocation(conn, now)

    stale_call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    current_call_id = _new_caller_call(conn, controller, catalog=catalog_2, segment="residential")

    stale_customer_error = _err(
        _create_customer(
            controller, cfg,
            call_id=stale_call_id, name="Ghost Caller", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert stale_customer_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert stale_customer_error["details"]["reason"] == "call_not_active"

    stale_location_error = _err(
        _create_location(
            controller, cfg, call_id=stale_call_id, customer_id=1,
            address=catalog["formatted_address"],
        )
    )
    assert stale_location_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert stale_location_error["details"]["reason"] == "call_not_active"

    current_customer_id = _ok(
        _create_customer(
            controller, cfg,
            call_id=current_call_id, name="Live Caller", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]

    assert conn.execute(
        "SELECT customer_id FROM _calls WHERE id = ?", (stale_call_id,)
    ).fetchone()["customer_id"] is None
    assert conn.execute(
        "SELECT customer_id FROM _calls WHERE id = ?", (current_call_id,)
    ).fetchone()["customer_id"] == current_customer_id
    assert conn.execute("SELECT COUNT(*) c FROM _customers").fetchone()["c"] == 1


def test_registration_blocked_outside_call_invocation(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")
    conn.execute("UPDATE _invocations SET status = 'completed'")
    _open_invocation(conn, clock.now(conn), invocation_type="opening")

    customer_error = _err(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert customer_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert customer_error["details"]["reason"] == "call_not_active"

    location_error = _err(
        _create_location(
            controller, cfg, call_id=call_id, customer_id=1,
            address=catalog["formatted_address"],
        )
    )
    assert location_error["code"] == ErrorCode.STATE_ILLEGAL.value
    assert location_error["details"]["reason"] == "call_not_active"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "segment": "residential", "contacts": [_phone_contact()]},
        {"name": "Jane Doe", "segment": "business", "contacts": [_phone_contact()]},
        {
            "name": "Jane Doe", "segment": "residential",
            "contacts": [{"contact_type": "phone", "value": "480-555-0123", "unexpected": 1}],
        },
        {
            "name": "Jane Doe", "segment": "residential",
            "contacts": [{"contact_type": "email", "value": "jane@example.com"}],
        },
        {
            "name": "Jane Doe", "segment": "residential",
            "contacts": [{"contact_type": "phone", "value": "12345"}],
        },
        {
            "name": "Jane Doe", "segment": "residential",
            "contacts": [{"contact_type": "phone", "value": "155-555-0100"}],
        },
    ],
)
def test_create_customer_argument_validation(tmp_path, kwargs):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    _open_call_invocation(conn, now)

    error = _err(_create_customer(controller, cfg, call_id=999999, **kwargs))
    assert error["code"] == ErrorCode.INVALID_ARGUMENT.value
    assert conn.execute("SELECT COUNT(*) c FROM _customers").fetchone()["c"] == 0


def test_cancellation_terminal_and_unknown_call(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)

    cancellation_call_id = _insert_bare_call(conn, now, call_kind="cancellation")
    cancel_customer_error = _err(
        _create_customer(
            controller, cfg,
            call_id=cancellation_call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert cancel_customer_error["details"]["reason"] == "cancellation_call"
    cancel_location_error = _err(
        _create_location(
            controller, cfg, call_id=cancellation_call_id, customer_id=1,
            address=catalog["formatted_address"],
        )
    )
    assert cancel_location_error["details"]["reason"] == "cancellation_call"

    terminal_call_id = _insert_bare_call(conn, now, status="booked")
    terminal_customer_error = _err(
        _create_customer(
            controller, cfg,
            call_id=terminal_call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert terminal_customer_error["details"]["reason"] == "call_not_active"
    terminal_location_error = _err(
        _create_location(
            controller, cfg, call_id=terminal_call_id, customer_id=1,
            address=catalog["formatted_address"],
        )
    )
    assert terminal_location_error["details"]["reason"] == "call_not_active"

    unknown_customer_error = _err(
        _create_customer(
            controller, cfg,
            call_id=999999, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert unknown_customer_error["code"] == ErrorCode.ENTITY_NOT_FOUND.value
    unknown_location_error = _err(
        _create_location(
            controller, cfg, call_id=999999, customer_id=1, address=catalog["formatted_address"],
        )
    )
    assert unknown_location_error["code"] == ErrorCode.ENTITY_NOT_FOUND.value


def test_registration_consumes_no_random_draw(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_call_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog, segment="residential")

    before_state = _rng_snapshot(conn)
    before_audit = audit_count(conn)

    customer_id = _ok(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )["customer_id"]
    _ok(
        _create_location(
            controller, cfg, call_id=call_id, customer_id=customer_id,
            address=catalog["formatted_address"],
        )
    )

    assert _rng_snapshot(conn) == before_state
    assert audit_count(conn) == before_audit

    before_state_fail = _rng_snapshot(conn)
    before_audit_fail = audit_count(conn)
    _err(
        _create_customer(
            controller, cfg,
            call_id=call_id, name="Jane Doe", segment="residential",
            contacts=[_phone_contact()],
        )
    )
    assert _rng_snapshot(conn) == before_state_fail
    assert audit_count(conn) == before_audit_fail
