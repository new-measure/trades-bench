from __future__ import annotations

import http.client
import json
import select
import socket
import sqlite3
import struct
import threading
import time
import uuid
from pathlib import Path

import pytest

import trades_bench.http_engine as http_engine
from trades_bench import clock
from trades_bench.config import load_run_config
from trades_bench.db import insert_run
from trades_bench.enums import InvocationType, RunStatus
from trades_bench.events import Events
from trades_bench.http_engine import Engine, open_engine_connection
from trades_bench.invocations import Invocations
from trades_bench.ledger import Ledger
from trades_bench.migrations import migrate
from trades_bench.rng import Rng, sqlite_audit_recorder
from trades_bench.txn import Services
from trades_bench.views import create_views
from trades_bench.wire import parse_engine_response

_SLOW_CUSTOMER_COUNT = 80
_SLOW_SQL = "SELECT COUNT(*) FROM customers a, customers b, customers c, customers d"


def _build_engine(
    dir_path: Path, *, scenario_overrides: dict | None = None, with_slow_customers: bool = False
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
    if with_slow_customers:
        for i in range(_SLOW_CUSTOMER_COUNT):
            conn.execute(
                "INSERT INTO _customers "
                "(id, name, segment, status, created_at, modified_at, origin) "
                "VALUES (?, ?, 'commercial', 'active', 500, 500, 'run')",
                (i + 1, f"Customer {i}"),
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
        headers_out = dict(resp.getheaders())
        status = resp.status
    finally:
        conn.close()
    parsed = json.loads(raw) if raw else None
    return status, parsed, headers_out


def _open_call_session(port: int, control_token: str, ts_ms: int = 1_000_000) -> int:
    status, body, _ = _request(
        port,
        "POST",
        "/v1/control/next_invocation",
        token=control_token,
        body={"type": "call", "ts_ms": ts_ms},
    )
    assert status == 200, body
    return int(body["data"]["invocation_id"])


def _force_reset_close(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _measure_natural_runtime(port: int, token: str, sql: str) -> float:
    start = time.monotonic()
    status, body, _ = _request(port, "POST", "/v1/sql", token=token, body={"sql": sql})
    elapsed = time.monotonic() - start
    assert status == 200, body
    return elapsed


def _instrument_sql_serialization(monkeypatch) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    intervals_lock = threading.Lock()
    original = http_engine._Handler._compute_sql_result

    def instrumented(self, body, deadline_at):
        start = time.monotonic()
        try:
            return original(self, body, deadline_at)
        finally:
            end = time.monotonic()
            with intervals_lock:
                intervals.append((start, end))

    monkeypatch.setattr(http_engine._Handler, "_compute_sql_result", instrumented)
    return intervals


def _assert_no_overlap(intervals: list[tuple[float, float]]) -> None:
    ordered = sorted(intervals)
    for (_, end), (next_start, _) in zip(ordered, ordered[1:], strict=False):
        assert end <= next_start, "concurrent SQL executions overlapped"


def _join_all_or_fail(threads: list[threading.Thread], timeout: float) -> None:
    for t in threads:
        t.join(timeout=timeout)
    if any(t.is_alive() for t in threads):
        pytest.fail("worker thread did not complete within the bound")


@pytest.fixture
def engine(tmp_path):
    eng, conn = _build_engine(tmp_path / "engine")
    eng.start()
    try:
        yield eng
    finally:
        eng.stop()
        conn.close()


def test_start_and_stop(tmp_path):
    eng, conn = _build_engine(tmp_path / "lifecycle")
    eng.start()
    port = eng.port
    assert port > 0
    status, body, _ = _request(port, "GET", "/v1/health")
    assert status == 200
    assert body == {"ok": True}
    eng.stop()
    conn.close()
    with pytest.raises(OSError):
        probe = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        probe.connect()


def test_health_is_unauthenticated_and_exact(engine):
    status, body, _ = _request(engine.port, "GET", "/v1/health")
    assert status == 200
    assert body == {"ok": True}


def test_health_ignores_a_bad_token(engine):
    status, body, _ = _request(engine.port, "GET", "/v1/health", token="garbage")
    assert status == 200
    assert body == {"ok": True}


def test_missing_token_on_sql_route_is_unauthorized(engine):
    status, body, _ = _request(engine.port, "POST", "/v1/sql", body={"sql": "SELECT 1"})
    assert status == 401
    assert body["error"] == {"code": "unauthorized", "message": "unauthorized"}


def test_garbage_token_on_sql_route_is_unauthorized(engine):
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token="not-a-real-token", body={"sql": "SELECT 1"}
    )
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_garbage_token_on_control_route_is_unauthorized(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token="not-a-real-token",
        body={"type": "call"},
    )
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_control_token_on_sql_route_is_unauthorized(engine):
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.control, body={"sql": "SELECT 1"}
    )
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_agent_token_on_control_route_matches_unknown_route_bytes(engine):
    unknown_status, unknown_body, _ = _request(engine.port, "POST", "/v1/does-not-exist")
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.agent,
        body={"type": "call"},
    )
    assert unknown_status == status == 404
    assert unknown_body == body


def test_unknown_control_operation_is_unknown_operation(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/frobnicate",
        token=engine.tokens.control,
        body={},
    )
    assert status == 404
    assert body["error"]["code"] == "unknown_operation"


def test_sql_without_open_session_is_no_active_session(engine):
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1"}
    )
    assert status == 409
    assert body["error"]["code"] == "no_active_session"


def test_control_next_invocation_envelope_shape(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"type": "opening", "ts_ms": 1_000_000},
    )
    assert status == 200
    assert body.keys() == {"data", "meta"}
    assert body["meta"].keys() == {"as_of", "invocation_id", "request_id", "spec_version"}
    assert body["data"]["invocation_id"] > 0
    assert body["meta"]["invocation_id"] == body["data"]["invocation_id"]


def test_control_failure_envelope_shape(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"type": "not-a-real-type"},
    )
    assert status == 400
    assert body.keys() == {"error", "meta"}
    assert body["meta"].keys() == {"as_of"}
    assert body["error"]["code"] == "invalid_request"


def test_complete_invocation_round_trip(engine):
    invocation_id = _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": invocation_id},
    )
    assert status == 200
    assert body["data"] == {"invocation_id": invocation_id, "status": "completed"}


def test_complete_invocation_unknown_id_is_not_found(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": 999999},
    )
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_sql_after_open_session_returns_shaped_result(engine):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1 AS one"}
    )
    assert status == 200
    assert body["data"] == {
        "columns": ["one"],
        "rows": [[1]],
        "row_count": 1,
        "truncated": False,
    }
    assert body["meta"].keys() == {"as_of", "invocation_id", "spec_version"}


def test_sql_denied_statement_is_sql_error_shape(engine):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/sql",
        token=engine.tokens.agent,
        body={"sql": "SELECT * FROM _customers"},
    )
    assert status == 422
    assert body["error"]["code"] == "unknown_relation"


def test_two_engine_instances_get_different_random_ports(tmp_path):
    eng1, conn1 = _build_engine(tmp_path / "a")
    eng2, conn2 = _build_engine(tmp_path / "b")
    eng1.start()
    eng2.start()
    try:
        assert eng1.port != 0
        assert eng2.port != 0
        assert eng1.port != eng2.port
    finally:
        eng1.stop()
        eng2.stop()
        conn1.close()
        conn2.close()


def test_tokens_are_regenerated_per_engine_and_never_share_principals(tmp_path):
    eng1, conn1 = _build_engine(tmp_path / "a")
    eng2, conn2 = _build_engine(tmp_path / "b")
    assert len(eng1.tokens.agent) == 64
    assert len(eng1.tokens.control) == 64
    assert eng1.tokens.agent != eng1.tokens.control
    assert eng1.tokens.agent != eng2.tokens.agent
    assert eng1.tokens.control != eng2.tokens.control
    eng1.stop()
    eng2.stop()
    conn1.close()
    conn2.close()


def test_engine_busy_when_inflight_gate_is_full(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "busy", scenario_overrides={"interface.max_inflight_requests": 1}
    )
    eng.start()
    try:
        assert eng._gate.try_enter() is True
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
        )
        assert status == 503
        assert body["error"]["code"] == "engine_busy"
        eng._gate.leave()
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
        )
        assert status == 409
    finally:
        eng.stop()
        conn.close()


def test_engine_busy_when_lock_acquire_times_out(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "lockbusy", scenario_overrides={"interface.lock_acquire_timeout_seconds": 1}
    )
    eng.start()
    try:
        assert eng._lock.acquire(1) is True
        start = time.monotonic()
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
        )
        elapsed = time.monotonic() - start
        assert status == 503
        assert body["error"]["code"] == "engine_busy"
        assert elapsed < 3
        eng._lock.release()
    finally:
        eng.stop()
        conn.close()


def test_sql_statement_timeout_returns_sql_timeout(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "sqltimeout",
        scenario_overrides={
            "interface.sql_statement_timeout_seconds": 1,
            "interface.request_deadline_seconds": 30,
        },
        with_slow_customers=True,
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control)
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": _SLOW_SQL}
        )
        assert status == 504
        assert body["error"]["code"] == "sql_timeout"
    finally:
        eng.stop()
        conn.close()


def test_request_deadline_exceeded_for_slow_sql(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "deadline",
        scenario_overrides={
            "interface.sql_statement_timeout_seconds": 30,
            "interface.request_deadline_seconds": 1,
        },
        with_slow_customers=True,
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control)
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": _SLOW_SQL}
        )
        assert status == 504
        assert body["error"]["code"] == "deadline_exceeded"
    finally:
        eng.stop()
        conn.close()


def test_client_disconnect_releases_lock_promptly(tmp_path):
    eng, conn = _build_engine(tmp_path / "disconnect", with_slow_customers=True)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control)
        natural_elapsed = _measure_natural_runtime(eng.port, eng.tokens.agent, _SLOW_SQL)
        assert natural_elapsed > 1.5, "slow query finishes too fast on this machine to discriminate"
        acquire_window = natural_elapsed / 3

        payload = json.dumps({"sql": _SLOW_SQL}).encode("utf-8")
        request_bytes = (
            b"POST /v1/sql HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + eng.tokens.agent.encode("ascii") + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + payload
        )
        sock = socket.create_connection(("127.0.0.1", eng.port), timeout=5)
        sock.sendall(request_bytes)
        time.sleep(0.2)
        sock.close()

        assert eng._lock.acquire(acquire_window) is True
        eng._lock.release()

        start = time.monotonic()
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
        )
        elapsed = time.monotonic() - start
        assert status == 200
        assert elapsed < acquire_window * 2
    finally:
        eng.stop()
        conn.close()


def test_forcefully_reset_client_also_releases_lock_promptly(tmp_path):
    eng, conn = _build_engine(tmp_path / "disconnect-rst", with_slow_customers=True)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control)
        natural_elapsed = _measure_natural_runtime(eng.port, eng.tokens.agent, _SLOW_SQL)
        assert natural_elapsed > 1.5, "slow query finishes too fast on this machine to discriminate"
        acquire_window = natural_elapsed / 3

        payload = json.dumps({"sql": _SLOW_SQL}).encode("utf-8")
        request_bytes = (
            b"POST /v1/sql HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + eng.tokens.agent.encode("ascii") + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + payload
        )
        sock = socket.create_connection(("127.0.0.1", eng.port), timeout=5)
        sock.sendall(request_bytes)
        time.sleep(0.2)
        _force_reset_close(sock)

        assert eng._lock.acquire(acquire_window) is True
        eng._lock.release()
    finally:
        eng.stop()
        conn.close()


def test_half_closed_client_is_treated_as_disconnected_and_releases_lock_promptly(tmp_path):
    eng, conn = _build_engine(tmp_path / "halfclose", with_slow_customers=True)
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control)
        natural_elapsed = _measure_natural_runtime(eng.port, eng.tokens.agent, _SLOW_SQL)
        assert natural_elapsed > 1.5, "slow query finishes too fast on this machine to discriminate"
        acquire_window = natural_elapsed / 3

        payload = json.dumps({"sql": _SLOW_SQL}).encode("utf-8")
        request_bytes = (
            b"POST /v1/sql HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + eng.tokens.agent.encode("ascii") + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + payload
        )
        sock = socket.create_connection(("127.0.0.1", eng.port), timeout=15)
        sock.sendall(request_bytes)
        sock.shutdown(socket.SHUT_WR)

        assert eng._lock.acquire(acquire_window) is True
        eng._lock.release()

        response = http.client.HTTPResponse(sock)
        response.begin()
        raw = response.read()
        status = response.status
        sock.close()

        assert status == 504
        body = json.loads(raw)
        assert body["error"]["code"] == "deadline_exceeded"
    finally:
        eng.stop()
        conn.close()


def test_sql_reads_create_no_idempotency_record(engine):
    _open_call_session(engine.port, engine.tokens.control)
    before = engine.services.conn.execute("SELECT COUNT(*) FROM _idempotency").fetchone()[0]
    status, body, headers = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1"}
    )
    assert status == 200
    assert "X-CSR-Idempotent-Replay" not in headers
    after = engine.services.conn.execute("SELECT COUNT(*) FROM _idempotency").fetchone()[0]
    assert after == before


def test_concurrent_requests_are_serialized_without_error(tmp_path, monkeypatch):
    eng, conn = _build_engine(tmp_path / "serial")
    eng.start()
    try:
        intervals = _instrument_sql_serialization(monkeypatch)
        _open_call_session(eng.port, eng.tokens.control)
        results = []
        lock = threading.Lock()

        def worker():
            status, body, _ = _request(
                eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
            )
            with lock:
                results.append((status, body))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        _join_all_or_fail(threads, timeout=10)
        assert len(results) == 8
        assert all(status == 200 for status, _ in results)
        assert len(intervals) == 8
        _assert_no_overlap(intervals)
    finally:
        eng.stop()
        conn.close()


def test_complete_invocation_retry_with_same_request_id_replays(engine):
    invocation_id = _open_call_session(engine.port, engine.tokens.control)
    status1, body1, headers1 = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": invocation_id, "request_id": "complete-retry-1"},
    )
    assert status1 == 200
    assert "X-CSR-Idempotent-Replay" not in headers1

    status2, body2, headers2 = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": invocation_id, "request_id": "complete-retry-1"},
    )
    assert status2 == 200
    assert body2 == body1
    assert headers2["X-CSR-Idempotent-Replay"] == "1"


def test_next_invocation_retry_with_same_request_id_replays(tmp_path):
    eng, conn = _build_engine(tmp_path / "replay-open")
    eng.start()
    try:
        status1, body1, headers1 = _request(
            eng.port,
            "POST",
            "/v1/control/next_invocation",
            token=eng.tokens.control,
            body={"type": "call", "ts_ms": 1_000_000, "request_id": "open-retry-1"},
        )
        assert status1 == 200
        assert "X-CSR-Idempotent-Replay" not in headers1

        status2, body2, headers2 = _request(
            eng.port,
            "POST",
            "/v1/control/next_invocation",
            token=eng.tokens.control,
            body={"type": "call", "ts_ms": 1_000_000, "request_id": "open-retry-1"},
        )
        assert status2 == 200
        assert body2 == body1
        assert headers2["X-CSR-Idempotent-Replay"] == "1"
    finally:
        eng.stop()
        conn.close()


def test_complete_invocation_retry_with_different_args_returns_request_id_conflict(engine):
    invocation_id = _open_call_session(engine.port, engine.tokens.control)
    status1, _, _ = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": invocation_id, "request_id": "dup-request-id"},
    )
    assert status1 == 200

    status2, body2, _ = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": 999999, "request_id": "dup-request-id"},
    )
    assert status2 == 409
    assert body2["error"]["code"] == "request_id_conflict"


def test_double_open_returns_conflict_with_invocation_already_active_rule(engine):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"type": "call", "ts_ms": 1_000_000},
    )
    assert status == 409
    assert body["error"]["code"] == "conflict"
    assert body["error"]["details"]["rule"] == "invocation_already_active"


def test_command_with_no_invocation_open_returns_no_active_session(engine):
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1"}
    )
    assert status == 409
    assert body["error"]["code"] == "no_active_session"


def test_native_failure_envelopes_report_current_simulated_time_not_boot(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "asof", scenario_overrides={"interface.max_inflight_requests": 1}
    )
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, ts_ms=5_000_000)
        expected = clock.to_iso8601(clock.now(conn))

        status, body, _ = _request(eng.port, "POST", "/v1/sql", body={"sql": "SELECT 1"})
        assert status == 401
        assert body["meta"]["as_of"] == expected

        status, body, _ = _request(
            eng.port, "POST", "/v1/does-not-exist", token=eng.tokens.agent
        )
        assert status == 404
        assert body["meta"]["as_of"] == expected

        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body=[1, 2, 3]
        )
        assert status == 400
        assert body["meta"]["as_of"] == expected

        assert eng._gate.try_enter() is True
        try:
            status, body, _ = _request(
                eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1"}
            )
        finally:
            eng._gate.leave()
        assert status == 503
        assert body["meta"]["as_of"] == expected
    finally:
        eng.stop()
        conn.close()


def test_agent_token_on_control_route_matches_unknown_route_bytes_after_clock_advance(engine):
    _open_call_session(engine.port, engine.tokens.control, ts_ms=3_000_000)
    unknown_status, unknown_body, _ = _request(engine.port, "POST", "/v1/does-not-exist")
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.agent,
        body={"type": "call"},
    )
    assert unknown_status == status == 404
    assert unknown_body == body


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"])
def test_unsupported_http_methods_return_unknown_operation(engine, method):
    status, body, headers = _request(engine.port, method, "/v1/sql")
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    if method != "HEAD":
        assert body["error"]["code"] == "unknown_operation"


def test_head_response_has_no_body_and_does_not_desync_the_pooled_connection(engine):
    conn = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=5)
    try:
        conn.request("HEAD", "/v1/health")
        head_response = conn.getresponse()
        head_body = head_response.read()
        assert head_response.status == 404
        assert int(head_response.getheader("Content-Length")) > 0
        assert head_body == b""

        conn.request("GET", "/v1/health")
        get_response = conn.getresponse()
        get_body = get_response.read()
        assert get_response.status == 200
        assert json.loads(get_body) == {"ok": True}
    finally:
        conn.close()


def test_sql_without_open_session_and_malformed_body_is_no_active_session(engine):
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": 123}
    )
    assert status == 409
    assert body["error"]["code"] == "no_active_session"


def test_sql_response_meta_invocation_id_matches_open_invocation(engine):
    invocation_id = _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1 AS one"}
    )
    assert status == 200
    assert body["meta"]["invocation_id"] == invocation_id


def test_sql_response_meta_invocation_id_updates_across_session_boundary(engine):
    first_invocation_id = _open_call_session(engine.port, engine.tokens.control)
    status, _, _ = _request(
        engine.port,
        "POST",
        "/v1/control/complete_invocation",
        token=engine.tokens.control,
        body={"invocation_id": first_invocation_id},
    )
    assert status == 200

    second_invocation_id = _open_call_session(
        engine.port, engine.tokens.control, ts_ms=2_000_000
    )
    assert second_invocation_id != first_invocation_id

    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1 AS one"}
    )
    assert status == 200
    assert body["meta"]["invocation_id"] == second_invocation_id


def test_sql_blob_literal_is_hex_encoded_in_response(engine):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT x'41'"}
    )
    assert status == 200
    assert body["data"]["rows"] == [["41"]]


def test_request_id_overlong_is_invalid_request(engine):
    _open_call_session(engine.port, engine.tokens.control)
    max_chars = engine.config.get_int("interface.error_echo_max_chars")
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/sql",
        token=engine.tokens.agent,
        body={"sql": "SELECT 1", "request_id": "x" * (max_chars + 1)},
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "request_id"


def test_request_id_non_string_is_invalid_request(engine):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/sql",
        token=engine.tokens.agent,
        body={"sql": "SELECT 1", "request_id": 12345},
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "request_id"


def test_request_id_reserved_prefix_is_invalid_request(engine):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/sql",
        token=engine.tokens.agent,
        body={"sql": "SELECT 1", "request_id": "__control__:whatever"},
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "request_id"


def test_control_request_id_reserved_prefix_is_invalid_request(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"type": "call", "ts_ms": 1_000_000, "request_id": "__control__:x"},
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "request_id"


def test_health_server_header_has_no_interpreter_version(engine):
    status1, _, headers1 = _request(engine.port, "GET", "/v1/health")
    status2, _, headers2 = _request(engine.port, "GET", "/v1/health")
    assert status1 == status2 == 200
    assert headers1["Server"] == "sunridge"
    assert "Python" not in headers1["Server"]
    assert not any(c.isdigit() for c in headers1["Server"])
    assert headers1["Server"] == headers2["Server"]


@pytest.mark.parametrize("value", ["5", True, 2.7])
def test_max_rows_non_int_types_return_invalid_request(engine, value):
    _open_call_session(engine.port, engine.tokens.control)
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/sql",
        token=engine.tokens.agent,
        body={"sql": "SELECT 1", "max_rows": value},
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"


def test_control_type_list_is_invalid_request_not_a_dropped_connection(engine):
    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/control/next_invocation",
        token=engine.tokens.control,
        body={"type": [], "ts_ms": 1_000_000},
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"


def test_unexpected_exception_returns_internal_error_envelope(engine, monkeypatch):
    _open_call_session(engine.port, engine.tokens.control)

    def boom(self, body, deadline_at):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(http_engine._Handler, "_compute_sql_result", boom)
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1"}
    )
    assert status == 500
    assert body["error"]["code"] == "internal_error"
    assert "incident_id" in body["error"]["details"]


class _RaisingSqlConn:
    def __init__(self, real):
        self._real = real

    def set_progress_handler(self, *args, **kwargs):
        self._real.set_progress_handler(*args, **kwargs)

    def execute(self, *args, **kwargs):
        raise sqlite3.DataError("simulated data error")


def test_sql_execution_non_operational_sqlite_error_is_sql_not_permitted(engine, monkeypatch):
    _open_call_session(engine.port, engine.tokens.control)
    monkeypatch.setattr(engine, "sql_conn", _RaisingSqlConn(engine.sql_conn))
    status, body, _ = _request(
        engine.port, "POST", "/v1/sql", token=engine.tokens.agent, body={"sql": "SELECT 1"}
    )
    assert status == 422
    assert body["error"]["code"] == "sql_not_permitted"


def test_pooled_connection_survives_unauthorized_then_valid_request(engine):
    conn = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=5)
    try:
        conn.request(
            "POST",
            "/v1/sql",
            body=json.dumps({"sql": "SELECT 1"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp1 = conn.getresponse()
        body1 = json.loads(resp1.read())
        assert resp1.status == 401
        assert body1["error"]["code"] == "unauthorized"

        _open_call_session(engine.port, engine.tokens.control)
        conn.request(
            "POST",
            "/v1/sql",
            body=json.dumps({"sql": "SELECT 1"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {engine.tokens.agent}",
            },
        )
        resp2 = conn.getresponse()
        body2 = json.loads(resp2.read())
        assert resp2.status == 200
        assert body2["data"]["rows"] == [[1]]
    finally:
        conn.close()


def test_pooled_connection_survives_agent_token_on_control_then_valid_request(engine):
    conn = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=5)
    try:
        conn.request(
            "POST",
            "/v1/control/next_invocation",
            body=json.dumps({"type": "call", "ts_ms": 1_000_000}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {engine.tokens.agent}",
            },
        )
        resp1 = conn.getresponse()
        body1 = json.loads(resp1.read())
        assert resp1.status == 404
        assert body1["error"]["code"] == "unknown_operation"

        conn.request("GET", "/v1/health")
        resp2 = conn.getresponse()
        body2 = json.loads(resp2.read())
        assert resp2.status == 200
        assert body2 == {"ok": True}
    finally:
        conn.close()


def test_chunked_post_is_rejected_as_invalid_request(engine):
    sock = socket.create_connection(("127.0.0.1", engine.port), timeout=5)
    try:
        payload = b'5\r\n{"sql"\r\n0\r\n\r\n'
        request_bytes = (
            b"POST /v1/sql HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer " + engine.tokens.agent.encode("ascii") + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n" + payload
        )
        sock.sendall(request_bytes)
        response = http.client.HTTPResponse(sock)
        response.begin()
        raw = response.read()
        status = response.status
        body = json.loads(raw)
        assert status == 400
        assert body["error"]["code"] == "invalid_request"
        assert body["error"].get("details", {}).get("field") != "sql"
    finally:
        sock.close()


def test_watchdog_latches_after_firing_once():
    watchdog = http_engine._Watchdog(deadline_at=time.monotonic() - 1.0, sql_timeout_at=None, sock=None)
    assert watchdog.progress_handler() == 1
    assert watchdog.fired == "deadline"
    assert watchdog.progress_handler() == 0
    assert watchdog.progress_handler() == 0


def test_expired_watchdog_does_not_escape_privileged_transaction_tail(tmp_path):
    eng, conn = _build_engine(tmp_path / "watchdog-tail")
    try:
        watchdog = http_engine._Watchdog(
            deadline_at=time.monotonic() - 1.0, sql_timeout_at=None, sock=None
        )
        conn.set_progress_handler(watchdog.progress_handler, http_engine._WATCHDOG_OPCODE_INTERVAL)
        try:
            response, _replayed = eng.services.invocations.complete_with_replay(
                999999, request_id="watchdog-tail-1", services=eng.services
            )
        finally:
            conn.set_progress_handler(None, 0)
        ok, _payload = parse_engine_response(response)
        assert ok is False
        assert conn.in_transaction is False

        opened, _replayed = eng.services.invocations.open_with_replay(
            InvocationType.CALL, 1_000_000, request_id="watchdog-tail-2", services=eng.services
        )
        assert isinstance(opened, int)
    finally:
        eng.stop()
        conn.close()


def test_stop_waits_for_in_flight_request_then_returns(tmp_path):
    eng, conn = _build_engine(tmp_path / "stop-inflight", with_slow_customers=True)
    eng.start()
    _open_call_session(eng.port, eng.tokens.control)
    result: dict[str, object] = {}

    def worker():
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": _SLOW_SQL}
        )
        result["status"] = status
        result["body"] = body

    t = threading.Thread(target=worker)
    t.start()
    gate_deadline = time.monotonic() + 5.0
    while eng._gate.count() < 1 and time.monotonic() < gate_deadline:
        time.sleep(0.01)
    assert eng._gate.count() >= 1

    stop_start = time.monotonic()
    eng.stop()
    stop_elapsed = time.monotonic() - stop_start

    _join_all_or_fail([t], timeout=15)
    conn.close()

    assert result.get("status") == 200
    assert stop_elapsed < 25


def test_start_after_stop_raises(tmp_path):
    eng, conn = _build_engine(tmp_path / "stop-restart")
    eng.start()
    eng.stop()
    try:
        with pytest.raises(RuntimeError):
            eng.start()
    finally:
        conn.close()


def test_stop_is_idempotent(tmp_path):
    eng, conn = _build_engine(tmp_path / "stop-idempotent")
    eng.start()
    eng.stop()
    eng.stop()
    conn.close()


def test_idle_silent_connections_are_reaped_within_bounded_time(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "idle-reap", scenario_overrides={"interface.max_inflight_requests": 2}
    )
    eng.start()
    try:
        silent_sockets = [
            socket.create_connection(("127.0.0.1", eng.port), timeout=1) for _ in range(4)
        ]
        pending = list(silent_sockets)
        deadline = time.monotonic() + 8
        while pending and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select(pending, [], [], min(1.0, remaining))
            for sock in ready:
                data = sock.recv(1)
                if data == b"":
                    pending.remove(sock)
        for sock in silent_sockets:
            sock.close()
        assert not pending, "idle connections were not reaped within the bound"

        status, body, _ = _request(eng.port, "GET", "/v1/health")
        assert status == 200
    finally:
        eng.stop()
        conn.close()


def test_engine_binds_to_loopback_only(tmp_path):
    eng, conn = _build_engine(tmp_path / "loopback")
    eng.start()
    try:
        assert eng._httpd.server_address[0] == "127.0.0.1"
    finally:
        eng.stop()
        conn.close()


def test_engine_rejects_non_loopback_host(tmp_path):
    eng, conn = _build_engine(tmp_path / "loopback-reject")
    try:
        with pytest.raises(ValueError):
            Engine(services=eng.services, config=eng.config, db_path=eng.db_path, host="0.0.0.0")
    finally:
        eng.stop()
        conn.close()


class _InterruptingConn:
    def __init__(self, real):
        self._real = real
        self._handler = None

    def set_progress_handler(self, handler, n):
        self._handler = handler
        self._real.set_progress_handler(handler, n)

    def execute(self, *args, **kwargs):
        if self._handler is not None:
            self._handler()
        raise sqlite3.OperationalError("interrupted")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_interrupted_view_read_maps_to_deadline_exceeded_not_internal_error(engine, monkeypatch):
    _open_call_session(engine.port, engine.tokens.control)

    def force_fired(self):
        self.fired = "deadline"
        return 1

    monkeypatch.setattr(http_engine._Watchdog, "progress_handler", force_fired)
    monkeypatch.setattr(engine, "sql_conn", _InterruptingConn(engine.sql_conn))

    status, body, _ = _request(
        engine.port, "POST", "/v1/ops/crm.list_customers", token=engine.tokens.agent, body={}
    )
    assert status == 504
    assert body["error"]["code"] == "deadline_exceeded"


class _ProgressHandlerSpy:
    def __init__(self, real):
        self._real = real
        self.installed_count = 0

    def set_progress_handler(self, handler, n):
        if handler is not None:
            self.installed_count += 1
        self._real.set_progress_handler(handler, n)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_weather_read_installs_a_watchdog(engine, monkeypatch):
    _open_call_session(engine.port, engine.tokens.control, ts_ms=1_000_000)
    today = clock.now_service_date(engine.services.conn)
    spy = _ProgressHandlerSpy(engine.services.conn)
    monkeypatch.setattr(engine.services, "conn", spy)

    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/ops/weather.get",
        token=engine.tokens.agent,
        body={"zone_id": 1, "date": today},
    )
    assert "meta" in body, body
    assert spy.installed_count >= 1


def test_maps_read_installs_a_watchdog(engine, monkeypatch):
    _open_call_session(engine.port, engine.tokens.control, ts_ms=1_000_000)
    spy = _ProgressHandlerSpy(engine.services.conn)
    monkeypatch.setattr(engine.services, "conn", spy)

    status, body, _ = _request(
        engine.port,
        "POST",
        "/v1/ops/maps.distance",
        token=engine.tokens.agent,
        body={"origin_address": "123 Main St", "destination_address": "456 Oak Ave"},
    )
    assert status == 500
    assert body["error"]["code"] == "internal_error"
    assert spy.installed_count >= 1


def test_pre_lock_as_of_reflects_committed_state_not_a_mid_transaction_write(tmp_path):
    from trades_bench import db as db_module

    eng, conn = _build_engine(tmp_path / "prelock-asof")
    eng.start()
    try:
        _open_call_session(eng.port, eng.tokens.control, ts_ms=2_000_000)
        committed = clock.to_iso8601(clock.now(eng.services.conn))

        db_module.begin(eng.services.conn)
        clock.advance(eng.services.conn, 9_000_000)
        try:
            status, body, _ = _request(eng.port, "POST", "/v1/does-not-exist")
            assert status == 404
            assert body["meta"]["as_of"] == committed

            status, body, _ = _request(
                eng.port, "POST", "/v1/sql", token="not-a-real-token", body={"sql": "SELECT 1"}
            )
            assert status == 401
            assert body["meta"]["as_of"] == committed
        finally:
            db_module.rollback(eng.services.conn)
    finally:
        eng.stop()
        conn.close()
