from __future__ import annotations

import http.client
import json
import uuid
from collections import defaultdict
from pathlib import Path

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config
from trades_bench import clock
from trades_bench.enums import EVENT_PRIORITY, EventKind
from trades_bench.events import Events
from trades_bench.http_engine import Engine, open_engine_connection
from trades_bench.invocations import Invocations
from trades_bench.ledger import Ledger
from trades_bench.rng import Rng, sqlite_audit_recorder
from trades_bench.run_init import initialize_run
from trades_bench.txn import Services

_CORPUS_PATH = Path(__file__).parent / "fixtures" / "sql_attack_corpus.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text())["entries"]

_EMPTY_AT_WARM_START = frozenset(
    {"commercial-hvac-invoicing", "tomorrow-board-one-technician", "notifications-newest-first"}
)

_WORKED_QUERIES = (
    (
        "commercial-hvac-invoicing",
        """
        SELECT c.segment, jt.name, COUNT(*) AS jobs,
               AVG(i.total_cents) AS avg_total_cents,
               MIN(i.total_cents) AS min_total_cents,
               MAX(i.total_cents) AS max_total_cents
        FROM invoices i
        JOIN jobs j       ON j.id = i.job_id
        JOIN job_types jt ON jt.id = j.job_type_id
        JOIN customers c  ON c.id = i.customer_id
        WHERE i.no_charge = 0
        GROUP BY c.segment, jt.name
        ORDER BY avg_total_cents DESC
        """,
        {"segment", "name", "jobs", "avg_total_cents", "min_total_cents", "max_total_cents"},
    ),
    (
        "tomorrow-board-one-technician",
        """
        SELECT a.id, a.scheduled_start, a.scheduled_end, a.status, l.street, l.city, l.zone_id
        FROM appointments a
        JOIN appointment_assignments aa ON aa.appointment_id = a.id AND aa.unassigned_at IS NULL
        JOIN jobs j      ON j.id = a.job_id
        JOIN locations l ON l.id = j.location_id
        WHERE aa.technician_id = 3
          AND date(a.scheduled_start / 1000, 'unixepoch', '-7 hours') = '2026-10-02'
        ORDER BY a.scheduled_start
        """,
        {"id", "scheduled_start", "scheduled_end", "status", "street", "city", "zone_id"},
    ),
    (
        "notifications-newest-first",
        """
        SELECT created_at, notification_type, entity_type, entity_id, message
        FROM notifications
        ORDER BY created_at DESC
        """,
        {"created_at", "notification_type", "entity_type", "entity_id", "message"},
    ),
    (
        "cash-movement-by-category",
        """
        SELECT entry_type, SUM(amount_cents) AS total_cents
        FROM ledger_entries
        WHERE service_date = '2026-09-30'
        GROUP BY entry_type
        """,
        {"entry_type", "total_cents"},
    ),
)


def _build_warm_engine(dir_path: Path) -> tuple[Engine, object]:
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    initialize_run(path, master_seed=12_345, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_engine_connection(path)
    rng = Rng(12_345, "1.0.0", audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    ledger = Ledger(conn)
    events = Events(conn, rng)
    invocations = Invocations(conn)
    services = Services(conn=conn, rng=rng, ledger=ledger, invocations=invocations, events=events)
    config = geo_run_config()
    engine = Engine(services=services, config=config, db_path=path)
    return engine, conn


def _request(port: int, method: str, path: str, *, token: str | None = None, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
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
        status = resp.status
    finally:
        conn.close()
    return status, raw, json.loads(raw)


def _open_session(engine, ts_ms: int) -> int:
    conn = engine.services.conn
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO _events (kind, scheduled_at_ms, priority, payload_version, "
        "status, created_at_ms, applied_at_ms) VALUES (?, ?, ?, 1, 'applied', ?, ?)",
        (
            EventKind.OPENING_INVOCATION.value,
            ts_ms,
            EVENT_PRIORITY[EventKind.OPENING_INVOCATION],
            ts_ms,
            ts_ms,
        ),
    )
    conn.execute("COMMIT")
    status, _, body = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"request_id": f"corpus-open-{uuid.uuid4().hex}"},
    )
    assert status == 200, body
    return int(body["data"]["invocation_id"])


@pytest.fixture(scope="module")
def warm_engine(tmp_path_factory):
    eng, conn = _build_warm_engine(tmp_path_factory.mktemp("http-corpus"))
    eng.start()
    now_ms = clock.now(conn)
    _open_session(eng, now_ms)
    try:
        yield eng
    finally:
        eng.stop()
        conn.close()


@pytest.mark.parametrize("entry", _CORPUS, ids=[entry["id"] for entry in _CORPUS])
def test_corpus_entry_is_rejected_with_expected_wire_code(warm_engine, entry):
    status, _, body = _request(
        warm_engine.port,
        "POST",
        "/v1/sql",
        token=warm_engine.tokens.agent,
        body={"sql": entry["sql"], "request_id": str(uuid.uuid4())},
    )
    assert body["error"]["code"] == entry["expect_code"], entry
    from trades_bench.wire import WIRE_STATUS, WireCode

    assert status == WIRE_STATUS[WireCode(entry["expect_code"])], entry
    assert "data" not in body
    assert set(body.keys()) == {"error", "meta"}


def _pair_groups() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in _CORPUS:
        pair = entry.get("pair")
        if pair is not None:
            groups[pair].append(entry)
    return groups


@pytest.mark.parametrize("pair_name", sorted(_pair_groups()))
def test_oracle_pair_is_byte_identical_across_hidden_and_nonexistent(warm_engine, pair_name):
    members = _pair_groups()[pair_name]
    assert len(members) >= 2, pair_name
    raw_bodies = []
    for entry in members:
        status, raw, body = _request(
            warm_engine.port,
            "POST",
            "/v1/sql",
            token=warm_engine.tokens.agent,
            body={"sql": entry["sql"]},
        )
        assert body["error"]["code"] == entry["expect_code"], entry
        raw_bodies.append(raw)
    assert len(set(raw_bodies)) == 1, (pair_name, raw_bodies)


def test_all_denials_in_the_corpus_carry_no_leaked_identifier(warm_engine):
    for entry in _CORPUS:
        status, _, body = _request(
            warm_engine.port,
            "POST",
            "/v1/sql",
            token=warm_engine.tokens.agent,
            body={"sql": entry["sql"]},
        )
        message = body["error"]["message"]
        assert "_" not in message
        assert "sqlite_master" not in message
        assert "no such" not in message


@pytest.mark.parametrize(
    "name,sql,expected_columns", _WORKED_QUERIES, ids=[q[0] for q in _WORKED_QUERIES]
)
def test_worked_example_query_succeeds_with_correct_shape(warm_engine, name, sql, expected_columns):
    status, _, body = _request(
        warm_engine.port,
        "POST",
        "/v1/sql",
        token=warm_engine.tokens.agent,
        body={"sql": sql},
    )
    assert status == 200, body
    data = body["data"]
    assert set(data.keys()) == {"columns", "rows", "row_count", "truncated"}
    assert set(data["columns"]) == expected_columns
    assert data["row_count"] == len(data["rows"])
    assert data["truncated"] is False
    if name in _EMPTY_AT_WARM_START:
        pytest.skip(
            "warm start does not yet seed calls/jobs/appointments/invoices/"
            "notifications history for this example to return rows"
        )
    assert data["row_count"] > 0
    if name == "notifications-newest-first":
        created_at_idx = data["columns"].index("created_at")
        created_ats = [row[created_at_idx] for row in data["rows"]]
        assert created_ats == sorted(created_ats, reverse=True)


def test_cash_movement_worked_query_returns_the_warm_start_opening_balance(warm_engine):
    status, _, body = _request(
        warm_engine.port,
        "POST",
        "/v1/sql",
        token=warm_engine.tokens.agent,
        body={
            "sql": (
                "SELECT entry_type, SUM(amount_cents) AS total_cents FROM ledger_entries "
                "WHERE service_date = '2026-09-30' GROUP BY entry_type"
            )
        },
    )
    assert status == 200, body
    rows = body["data"]["rows"]
    assert rows
    columns = body["data"]["columns"]
    entry_type_idx = columns.index("entry_type")
    assert any(row[entry_type_idx] == "opening_balance_adjustment" for row in rows)
