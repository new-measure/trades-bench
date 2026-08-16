from __future__ import annotations

import json

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
)
from csr_bench import SPEC_VERSION, calls, clock, db
from csr_bench.config import load_run_config
from csr_bench.controller import Controller
from csr_bench.db import connect
from csr_bench.enums import EventKind
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.events import Events
from csr_bench.rng import Rng
from csr_bench.run_init import initialize_run, open_run_db

SEED = 11
START_DATE = "2026-10-01"
END_DATE = "2026-10-02"


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


def _now_ms(conn) -> int:
    return conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]


def _usable_catalog_rows(conn, count):
    rows = conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT ?",
        (count,),
    ).fetchall()
    assert len(rows) == count
    return rows


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
    except BaseException:
        rng.exit_txn()
        db.rollback(conn)
        raise
    rng.exit_txn()
    db.commit(conn)
    return event_row, call_id


@pytest.fixture()
def run_db(tmp_path):
    config = _config()
    conn = _init(tmp_path / "run.db", config)
    rng = Rng(SEED, SPEC_VERSION, conn=conn)
    yield conn, rng
    conn.close()


def test_known_customer_known_address_payload(run_db):
    conn, rng = run_db
    now = _now_ms(conn)
    arrival_at = clock.phoenix_instant(2026, 10, 1, 9, 30)
    catalog = _usable_catalog_rows(conn, 1)[0]
    db.begin(conn)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    pending_id = _insert_pending(
        conn, now, arrival_at, customer_id=customer_id, location_id=location_id
    )
    db.commit(conn)

    event_row, call_id = _arrive(conn, rng, pending_id, arrival_at)
    payload = calls.prompt_payload(conn, event_row, arrival_at)

    assert set(payload) == {
        "time", "call_id", "caller", "phone", "address",
        "customer_id", "location_id", "requested_service", "urgent",
    }
    assert payload["call_id"] == call_id
    assert payload["customer_id"] == customer_id
    assert payload["location_id"] == location_id
    assert payload["address"] == catalog["formatted_address"]
    assert payload["requested_service"] == "HVAC service"
    assert payload["urgent"] is False
    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["segment"] is None
    hidden = conn.execute(
        "SELECT * FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    assert hidden["true_segment"] == "residential"
    assert hidden["arrival_event_id"] == event_row["event_id"]
    assert hidden["acceptance_result"] is None
    assert (
        conn.execute("SELECT 1 FROM _pending_calls WHERE id = ?", (pending_id,)).fetchone()
        is None
    )


def test_known_customer_new_address_payload(run_db):
    conn, rng = run_db
    now = _now_ms(conn)
    arrival_at = clock.phoenix_instant(2026, 10, 1, 11, 5)
    known, fresh = _usable_catalog_rows(conn, 2)
    db.begin(conn)
    customer_id = _insert_customer(conn, now, segment="commercial")
    _insert_location(conn, now, customer_id, known)
    pending_id = _insert_pending(
        conn,
        now,
        arrival_at,
        segment="commercial",
        customer_id=customer_id,
        address_catalog_id=fresh["id"],
    )
    db.commit(conn)

    event_row, call_id = _arrive(conn, rng, pending_id, arrival_at)
    payload = calls.prompt_payload(conn, event_row, arrival_at)

    assert set(payload) == {
        "time", "call_id", "caller", "phone", "address",
        "customer_id", "requested_service", "urgent",
    }
    assert payload["customer_id"] == customer_id
    assert payload["address"] == fresh["formatted_address"]
    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["segment"] is None
    assert call["location_id"] is None
    assert call["service_street"] == f"{fresh['house_number']} {fresh['street']}"
    assert call["service_postal_code"] == fresh["postal_code"]
    assert call["address_catalog_id"] == fresh["id"]


def test_callback_payload_carries_the_previous_job(run_db):
    conn, rng = run_db
    now = _now_ms(conn)
    arrival_at = clock.phoenix_instant(2026, 10, 1, 14, 45)
    completed_at = clock.phoenix_instant(2026, 9, 20, 15, 0)
    catalog = _usable_catalog_rows(conn, 1)[0]
    db.begin(conn)
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    prior_call = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (?, '2026-09-19', 'new_job', 'booked', 'Fixture Caller', '4805551234', "
        "'HVAC service', 0, ?)",
        (clock.phoenix_instant(2026, 9, 19, 10, 0), now),
    )
    prior_call_id = int(prior_call.lastrowid)
    prior_job = conn.execute(
        "INSERT INTO _jobs (job_number, call_id, customer_id, location_id, job_type_id, "
        "requested_service_name, status, priority, no_charge, summary, created_at, "
        "completed_at, modified_at) "
        "VALUES ('9001', ?, ?, ?, 1, 'HVAC service', 'completed', 'normal', 0, '', ?, ?, ?)",
        (prior_call_id, customer_id, location_id, now, completed_at, now),
    )
    job_id = int(prior_job.lastrowid)
    pending_id = _insert_pending(
        conn,
        now,
        arrival_at,
        channel="callback",
        customer_id=customer_id,
        location_id=location_id,
        related_job_id=job_id,
    )
    db.commit(conn)

    event_row, call_id = _arrive(conn, rng, pending_id, arrival_at)
    payload = calls.prompt_payload(conn, event_row, arrival_at)

    assert set(payload) == {
        "time", "call_id", "caller", "phone", "address", "customer_id", "location_id",
        "related_job_id", "related_job_completed_date", "requested_service", "urgent",
    }
    assert payload["related_job_id"] == job_id
    assert payload["related_job_completed_date"] == "2026-09-20"
    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["related_job_id"] == job_id


def test_arrival_with_a_missing_pending_row_fails(run_db):
    conn, rng = run_db
    arrival_at = clock.phoenix_instant(2026, 10, 1, 9, 0)
    with pytest.raises(EngineError) as ei:
        _arrive(conn, rng, 999999, arrival_at)
    assert ei.value.code is ErrorCode.ENTITY_NOT_FOUND
    assert conn.execute("SELECT COUNT(*) c FROM _calls").fetchone()["c"] == 0


class CommitCheckingAdapter(ScriptedNoOpAdapter):

    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.checked = 0

    def send(self, prompt):
        if prompt.kind.value == "call":
            other = connect(self.db_path)
            try:
                call = other.execute(
                    "SELECT c.status, h.arrival_event_id FROM _calls c "
                    "JOIN _call_hidden h ON h.call_id = c.id WHERE c.id = ?",
                    (prompt.payload["call_id"],),
                ).fetchone()
                assert call is not None
                assert call["status"] == "active"
                event = other.execute(
                    "SELECT entity_id FROM _events WHERE event_id = ?",
                    (call["arrival_event_id"],),
                ).fetchone()
                assert (
                    other.execute(
                        "SELECT 1 FROM _pending_calls WHERE id = ?",
                        (event["entity_id"],),
                    ).fetchone()
                    is None
                )
            finally:
                other.close()
            self.checked += 1
        return super().send(prompt)


def test_new_caller_arrival_commits_before_the_prompt_renders(tmp_path):
    path = tmp_path / "run.db"
    config = _config()
    conn = _init(path, config)
    adapter = CommitCheckingAdapter(path)
    Controller(conn, config, adapter).run()
    assert adapter.checked > 0
    assert conn.execute("SELECT COUNT(*) c FROM _pending_calls").fetchone()["c"] == 0
    rows = conn.execute(
        "SELECT c.*, h.true_segment, h.requested_category_id, sc.name AS category_name "
        "FROM _calls c JOIN _call_hidden h ON h.call_id = c.id "
        "JOIN _service_catalog sc ON sc.id = h.requested_category_id "
        "ORDER BY c.arrived_at, c.id"
    ).fetchall()
    assert len(rows) == adapter.checked
    for row in rows:
        assert row["status"] == "active"
        assert row["call_kind"] == "new_job"
        assert row["customer_id"] is None
        assert row["segment"] == row["true_segment"]
        assert row["requested_service_name"] == row["category_name"]
        assert row["service_street"]
        assert row["service_postal_code"]
    conn.close()
