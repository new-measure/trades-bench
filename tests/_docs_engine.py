from __future__ import annotations

import http.client
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from _helpers import GEO_ARTIFACT_DIR, geo_run_config
from trades_bench import clock, db, geo_reader
from trades_bench.calls import arrive
from trades_bench.enums import EventKind
from trades_bench.events import Events
from trades_bench.http_engine import Engine, open_engine_connection
from trades_bench.invocations import Invocations
from trades_bench.ledger import Ledger
from trades_bench.rng import Rng, sqlite_audit_recorder
from trades_bench.run_init import initialize_run
from trades_bench.txn import Services

MASTER_SEED = 1
SPEC_VERSION = "1.0.0"


@dataclass(frozen=True)
class DocsEngine:
    engine: Engine
    conn: object
    call_id: int

    @property
    def port(self) -> int:
        return self.engine.port

    @property
    def agent_token(self) -> str:
        return self.engine.tokens.agent


def http_call(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = None
    if body is not None:
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
    finally:
        conn.close()
    return status, raw, response_headers


def fire_example(engine: DocsEngine, path: str, body: dict) -> tuple[int, dict]:
    status, raw, _ = http_call(engine.port, "POST", path, token=engine.agent_token, body=body)
    return status, json.loads(raw)


def _build_cold_start_engine(dir_path: Path) -> tuple[Engine, object]:
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    config = geo_run_config()
    initialize_run(path, master_seed=MASTER_SEED, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_engine_connection(path)
    rng = Rng(
        MASTER_SEED,
        SPEC_VERSION,
        audit_recorder=sqlite_audit_recorder(conn),
        conn=conn,
    )
    ledger = Ledger(conn)
    events = Events(conn, rng)
    invocations = Invocations(conn)
    services = Services(conn=conn, rng=rng, ledger=ledger, invocations=invocations, events=events)
    engine = Engine(
        services=services,
        config=config,
        db_path=path,
        artifact=geo_reader.load_artifact(GEO_ARTIFACT_DIR),
    )
    return engine, conn


def _open_call_invocation(engine: Engine, ts_ms: int) -> int:
    status, raw, _ = http_call(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"type": "call", "ts_ms": ts_ms, "request_id": f"docs-open-{ts_ms}"},
    )
    assert status == 200, raw
    return int(json.loads(raw)["data"]["invocation_id"])


def _script_active_call(conn, rng) -> int:
    now = clock.now(conn)
    arrival_at = now + 60000
    catalog = conn.execute(
        "SELECT * FROM _address_catalog WHERE status = 'usable' ORDER BY id LIMIT 1"
    ).fetchone()
    db.begin(conn)
    try:
        pending = conn.execute(
            "INSERT INTO _pending_calls (service_date, zone_id, arrival_at, call_kind, "
            "channel, segment, requested_category_id, urgent, customer_id, location_id, "
            "address_catalog_id, caller_name, caller_phone, prospect_id, related_job_id, "
            "slot_index, created_at) VALUES (?, NULL, ?, 'new_job', 'new_caller', "
            "'residential', 2, 0, NULL, NULL, ?, 'Example Caller', '6025550142', NULL, "
            "NULL, NULL, ?)",
            (
                clock.service_date_for_ms(arrival_at),
                arrival_at,
                catalog["id"],
                now,
            ),
        )
        pending_id = int(pending.lastrowid)
    except BaseException:
        db.rollback(conn)
        raise
    db.commit(conn)

    db.begin(conn)
    rng.enter_txn()
    try:
        event_id = Events(conn, rng).schedule(
            EventKind.CALL_INVOCATION,
            arrival_at,
            entity_type="pending_call",
            entity_id=pending_id,
        )
        event_row = conn.execute("SELECT * FROM _events WHERE event_id = ?", (event_id,)).fetchone()
        call_id = arrive(conn, event_row, now_ms=arrival_at)
        conn.execute(
            "UPDATE _events SET status = 'applied', applied_at_ms = ? WHERE event_id = ?",
            (arrival_at, event_id),
        )
        clock.advance(conn, arrival_at)
    except BaseException:
        rng.exit_txn()
        db.rollback(conn)
        raise
    rng.exit_txn()
    db.commit(conn)
    return int(call_id)


@contextmanager
def docs_engine(dir_path: Path):
    engine, conn = _build_cold_start_engine(dir_path)
    engine.start()
    try:
        _open_call_invocation(engine, clock.now(conn))
        call_id = _script_active_call(conn, engine.services.rng)
        yield DocsEngine(engine=engine, conn=conn, call_id=call_id)
    finally:
        engine.stop()
        conn.close()
