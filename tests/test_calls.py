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
    next_test_origin,
    register_conn,
)
from trades_bench import SPEC_VERSION, calls, clock, db
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.db import connect
from trades_bench.enums import EventKind
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.events import Events
from trades_bench.rng import Rng
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.txn import command

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
        assert row["status"] == "unanswered"
        assert row["terminal_reason"] == "unanswered"
        assert row["call_kind"] == "new_job"
        assert row["customer_id"] is None
        assert row["segment"] == row["true_segment"]
        assert row["requested_service_name"] == row["category_name"]
        assert row["service_street"]
        assert row["service_postal_code"]
    conn.close()


_request_seq = count(1)
_arrival_seq = count(0)


def _next_request_id(prefix: str) -> str:
    return f"{prefix}.{next(_request_seq)}"


def _arrival_at():
    return clock.phoenix_instant(2026, 10, 1, 9, 0) + next(_arrival_seq) * 60_000


def _open_invocation(conn, now, invocation_type="call", *, call_id=None):
    origin_event_id = None
    if call_id is not None:
        origin_event_id = conn.execute(
            "SELECT arrival_event_id FROM _call_hidden WHERE call_id = ?", (call_id,)
        ).fetchone()["arrival_event_id"]
    conn.execute(
        "INSERT INTO _invocations (type, ts_ms, sequence, status, origin_event_id) "
        "VALUES (?, ?, "
        "(SELECT COALESCE(MAX(sequence), 0) + 1 FROM _invocations), 'active', ?)",
        (invocation_type, now, origin_event_id),
    )


def _close_active_invocation(conn):
    conn.execute("UPDATE _invocations SET status = 'completed' WHERE status = 'active'")


def _build(tmp_path, *, seed=SEED, config=None, name="run.db"):
    cfg = config if config is not None else _config()
    conn = _init(tmp_path / name, cfg, seed=seed)
    register_conn(conn)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    return conn, cfg, controller


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


def _new_caller_call(
    conn, controller, *, catalog, segment="residential",
    caller_name="Fixture Caller", caller_phone="4805551234", prospect_id=None,
):
    now = _now_ms(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        channel="new_caller", segment=segment, address_catalog_id=catalog["id"],
        caller_name=caller_name, caller_phone=caller_phone, prospect_id=prospect_id,
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


def _registered_customer_call(conn, controller, *, customer_id, catalog, segment="residential"):
    now = _now_ms(conn)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        channel="existing_customer", segment=segment, customer_id=customer_id,
        address_catalog_id=catalog["id"],
    )
    return _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)


def _insert_bare_call(
    conn, now, *, status="active", call_kind="new_job", customer_id=None,
    address_catalog_id=None, segment="residential",
):
    cur = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, segment, customer_id, requested_service_name, urgent, "
        "address_catalog_id, created_at) VALUES (?, ?, ?, ?, 'Fixture Caller', '4805551234', "
        "?, ?, 'HVAC service', 0, ?, ?)",
        (now, START_DATE, call_kind, status, segment, customer_id, address_catalog_id, now),
    )
    return int(cur.lastrowid)


def _insert_customer_repeat_state(conn, customer_id, propensity_micros, now, last_outcome=None):
    conn.execute(
        "INSERT INTO _customer_repeat_state (customer_id, propensity_micros, last_outcome, "
        "competitor_captured_at, updated_at, origin_key) VALUES (?, ?, ?, NULL, ?, ?)",
        (customer_id, propensity_micros, last_outcome, now, f"t:customer:{customer_id}"),
    )


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


def _competitor_state(conn):
    return conn.execute(
        "SELECT share_micros, capture_count FROM _competitor_state WHERE singleton = 1"
    ).fetchone()


def _customer_repeat_row(conn, customer_id):
    return conn.execute(
        "SELECT * FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
    ).fetchone()


def _ledger_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _ledger_entries").fetchone()["c"]


def _notification_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _notifications").fetchone()["c"]


def _prospect_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _prospect_state").fetchone()["c"]


def _customer_repeat_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _customer_repeat_state").fetchone()["c"]


def _world_snapshot(conn):
    comp = _competitor_state(conn)
    return (
        audit_count(conn),
        _prospect_count(conn),
        _customer_repeat_count(conn),
        int(comp["share_micros"]),
        int(comp["capture_count"]),
        _ledger_count(conn),
        _notification_count(conn),
    )


def _demand_state(conn):
    return tuple(
        tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())
        for table in ("_customer_repeat_state", "_prospect_state", "_competitor_state")
    )


def test_rejection_leaves_demand_state_untouched_and_draws_nothing(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog_a, catalog_b = _usable_catalog_rows(conn, 2)
    customer_id = _insert_customer(conn, now)
    _insert_customer_repeat_state(conn, customer_id, 2_000_000, now)
    _open_invocation(conn, now)
    for make_call in (
        lambda: _registered_customer_call(
            conn, controller, customer_id=customer_id, catalog=catalog_a
        ),
        lambda: _new_caller_call(conn, controller, catalog=catalog_b),
    ):
        call_id = make_call()
        state_before = _demand_state(conn)
        audit_before = audit_count(conn)
        _, body = _reject(controller, cfg, call_id=call_id)
        _ok(body)
        assert _demand_state(conn) == state_before
        assert audit_count(conn) == audit_before
        assert _call_terminal_snapshot(conn, call_id)[0] == "rejected"


def _call_terminal_snapshot(conn, call_id):
    row = conn.execute(
        "SELECT status, terminal_reason, terminal_at FROM _calls WHERE id = ?", (call_id,)
    ).fetchone()
    return (row["status"], row["terminal_reason"], row["terminal_at"])


def test_rejection_equivalence_across_terminal_reasons(tmp_path):
    reasons = ["agent_rejected", "customer_declined", "unanswered"]
    snapshots = {}
    call_ids = {}
    for reason in reasons:
        config = _config()
        conn, cfg, controller = _build(tmp_path, config=config, name=f"{reason}.db")
        now = _now_ms(conn)
        catalog = _usable_catalog_rows(conn, 1)[0]
        _open_invocation(conn, now)
        call_id = _new_caller_call(conn, controller, catalog=catalog)
        call_ids[reason] = call_id

        def fn(ctx, call_id=call_id, reason=reason, cfg=cfg, conn=conn):
            call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
            calls.ordinary_rejection(ctx, cfg, call, terminal_reason=reason)
            return None

        response = command(
            controller.services,
            _next_request_id("ordinary_rejection"),
            fn,
            operation="test.ordinary_rejection",
            args={"call_id": call_id, "reason": reason},
        )
        body = json.loads(response)
        assert body["ok"] is True, body

        call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
        snapshots[reason] = {
            "demand_state": _demand_state(conn),
            "ledger_count": _ledger_count(conn),
            "status": call["status"],
            "terminal_reason": call["terminal_reason"],
            "terminal_at": call["terminal_at"],
        }
        conn.close()

    assert call_ids["agent_rejected"] == call_ids["customer_declined"] == call_ids["unanswered"]

    baseline = snapshots["agent_rejected"]
    for reason in ("customer_declined", "unanswered"):
        other = snapshots[reason]
        assert other["demand_state"] == baseline["demand_state"]
        assert other["ledger_count"] == baseline["ledger_count"]
        assert other["terminal_at"] == baseline["terminal_at"]

    expected_status = {
        "agent_rejected": "rejected",
        "customer_declined": "declined",
        "unanswered": "unanswered",
    }
    for reason, snap in snapshots.items():
        assert snap["status"] == expected_status[reason]
        assert snap["terminal_reason"] == reason


def test_ordinary_rejection_guards_cancellation_call_directly(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    _open_invocation(conn, now)
    call_id = _insert_bare_call(conn, now, call_kind="cancellation")

    def fn(ctx):
        call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
        calls.ordinary_rejection(ctx, cfg, call, terminal_reason="unanswered")
        return None

    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_id)
    response = command(
        controller.services,
        _next_request_id("ordinary_rejection_guard"),
        fn,
        operation="test.ordinary_rejection",
        args={"call_id": call_id},
    )
    err = _err(json.loads(response))
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "cancellation_call"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_id) == terminal_before


def test_ordinary_rejection_guards_non_active_call_directly(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    _open_invocation(conn, now)
    call_id = _insert_bare_call(conn, now, status="rejected", call_kind="new_job")

    def fn(ctx):
        call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
        calls.ordinary_rejection(ctx, cfg, call, terminal_reason="unanswered")
        return None

    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_id)
    response = command(
        controller.services,
        _next_request_id("ordinary_rejection_guard"),
        fn,
        operation="test.ordinary_rejection",
        args={"call_id": call_id},
    )
    err = _err(json.loads(response))
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_id) == terminal_before


def test_reject_unknown_call_id_fails_entity_not_found(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    _open_invocation(conn, now)
    before = _world_snapshot(conn)

    _, body = _reject(controller, cfg, call_id=999999)
    err = _err(body)
    assert err["code"] == "entity_not_found"
    assert _world_snapshot(conn) == before


def test_no_active_invocation_means_no_current_call(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog)
    assert calls.current_active_call_id(conn) == call_id
    _close_active_invocation(conn)
    assert calls.current_active_call_id(conn) is None


def test_current_call_follows_the_active_invocation_not_the_newest_arrival(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog_a, catalog_b = _usable_catalog_rows(conn, 2)
    call_a = _new_caller_call(
        conn, controller, catalog=catalog_a, caller_name="Caller A", caller_phone="4805550001"
    )
    call_b = _new_caller_call(
        conn, controller, catalog=catalog_b, caller_name="Caller B", caller_phone="4805550002"
    )
    assert call_b > call_a
    _open_invocation(conn, now, call_id=call_a)
    assert calls.current_active_call_id(conn) == call_a


def test_reject_stale_call_while_a_newer_call_is_current(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog_a, catalog_b = _usable_catalog_rows(conn, 2)
    _open_invocation(conn, now)
    call_a = _new_caller_call(
        conn, controller, catalog=catalog_a, caller_name="Caller A", caller_phone="4805550001"
    )
    call_b = _new_caller_call(
        conn, controller, catalog=catalog_b, caller_name="Caller B", caller_phone="4805550002"
    )
    assert calls.current_active_call_id(conn) == call_b

    before = _world_snapshot(conn)
    stale_before = _call_terminal_snapshot(conn, call_a)
    _, body = _reject(controller, cfg, call_id=call_a)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_a) == stale_before


def test_reject_cancellation_call_fails_cancellation_call(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    _open_invocation(conn, now)
    call_id = _insert_bare_call(conn, now, call_kind="cancellation")

    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_id)
    _, body = _reject(controller, cfg, call_id=call_id)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "cancellation_call"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_id) == terminal_before


def test_reject_terminal_call_fails_on_second_attempt(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog)

    _, first_body = _reject(controller, cfg, call_id=call_id)
    _ok(first_body)

    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_id)
    _, second_body = _reject(controller, cfg, call_id=call_id, request_id=_next_request_id("reject_retry"))
    err = _err(second_body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_id) == terminal_before


def test_reject_during_opening_invocation_fails_call_not_active(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog)
    _close_active_invocation(conn)
    _open_invocation(conn, now, invocation_type="opening")

    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_id)
    _, body = _reject(controller, cfg, call_id=call_id)
    err = _err(body)
    assert err["code"] == "state_illegal"
    assert err["details"]["reason"] == "call_not_active"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_id) == terminal_before


def test_reject_with_no_active_invocation_fails_invocation_conflict(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog)
    _close_active_invocation(conn)

    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_id)
    _, body = _reject(controller, cfg, call_id=call_id)
    err = _err(body)
    assert err["code"] == "invocation_conflict"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_id) == terminal_before


def test_reject_non_int_call_id_fails_invalid_argument(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    _open_invocation(conn, now)
    before = _world_snapshot(conn)

    for bad_call_id in ("5", True, 5.0, None):
        _, body = _reject(controller, cfg, call_id=bad_call_id)
        err = _err(body)
        assert err["code"] == "invalid_argument"
    assert _world_snapshot(conn) == before


def test_reject_idempotent_retry_returns_stored_response(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog = _usable_catalog_rows(conn, 1)[0]
    _open_invocation(conn, now)
    call_id = _new_caller_call(conn, controller, catalog=catalog)

    request_id = _next_request_id("reject_idempotent")
    first_response, first_body = _reject(controller, cfg, call_id=call_id, request_id=request_id)
    _ok(first_body)

    state_after_first = _demand_state(conn)
    audit_after_first = audit_count(conn)

    second_response, second_body = _reject(controller, cfg, call_id=call_id, request_id=request_id)
    assert second_response == first_response

    assert _demand_state(conn) == state_after_first
    audit_after_second = audit_count(conn)
    assert audit_after_second == audit_after_first


def test_reject_replayed_request_id_for_different_call_conflicts(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    catalog_a, catalog_b = _usable_catalog_rows(conn, 2)
    _open_invocation(conn, now)
    call_a = _new_caller_call(
        conn, controller, catalog=catalog_a, caller_name="Caller A", caller_phone="4805550001"
    )
    request_id = _next_request_id("reject_replay")
    first_response, body_a = _reject(controller, cfg, call_id=call_a, request_id=request_id)
    _ok(body_a)

    call_b = _new_caller_call(
        conn, controller, catalog=catalog_b, caller_name="Caller B", caller_phone="4805550002"
    )
    before = _world_snapshot(conn)
    terminal_before = _call_terminal_snapshot(conn, call_b)
    assert terminal_before[0] == "active"

    _, body_conflict = _reject(controller, cfg, call_id=call_b, request_id=request_id)
    err = _err(body_conflict)
    assert err["code"] == "request_id_conflict"
    assert _world_snapshot(conn) == before
    assert _call_terminal_snapshot(conn, call_b) == terminal_before

    replay_response, replay_body = _reject(controller, cfg, call_id=call_a, request_id=request_id)
    assert replay_response == first_response
    _ok(replay_body)
    assert _world_snapshot(conn) == before

    _, body_b = _reject(controller, cfg, call_id=call_b)
    _ok(body_b)
    assert _call_terminal_snapshot(conn, call_b)[0] == "rejected"


def test_reject_callback_call_takes_ordinary_path_without_notifications(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = _now_ms(conn)
    completed_at = clock.phoenix_instant(2026, 9, 20, 15, 0)
    catalog = _usable_catalog_rows(conn, 1)[0]
    customer_id = _insert_customer(conn, now)
    location_id = _insert_location(conn, now, customer_id, catalog)
    _insert_customer_repeat_state(conn, customer_id, 2_000_000, now)
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
    _open_invocation(conn, now)
    arrival_at = _arrival_at()
    pending_id = _insert_pending(
        conn, now, arrival_at,
        channel="callback", customer_id=customer_id, location_id=location_id,
        related_job_id=job_id,
    )
    call_id = _arrive_applied(conn, controller.services.rng, pending_id, arrival_at)

    ledger_before = _ledger_count(conn)
    _, body = _reject(controller, cfg, call_id=call_id)
    _ok(body)

    call = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    assert call["status"] == "rejected"
    assert call["terminal_reason"] == "agent_rejected"
    assert call["related_job_id"] == job_id

    row = _customer_repeat_row(conn, customer_id)
    assert row["propensity_micros"] == 2_000_000
    assert row["last_outcome"] is None

    assert _ledger_count(conn) == ledger_before
    assert _notification_count(conn) == 0
