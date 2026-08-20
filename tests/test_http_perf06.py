from __future__ import annotations

import socket
import threading
import time

import pytest

from test_http_engine import (
    _SLOW_SQL,
    _assert_no_overlap,
    _build_engine,
    _instrument_sql_serialization,
    _join_all_or_fail,
    _measure_natural_runtime,
    _open_session,
    _request,
)

_ROW_LIMIT_CUSTOMER_COUNT = 12


def test_sql_statement_timeout_is_clean_structured_and_leaks_nothing(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "perf06-timeout",
        scenario_overrides={
            "interface.sql_statement_timeout_seconds": 1,
            "interface.request_deadline_seconds": 30,
        },
        with_slow_customers=True,
    )
    eng.start()
    try:
        _open_session(eng)
        status, body, _ = _request(
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": _SLOW_SQL}
        )
        assert status == 504
        assert body["error"]["code"] == "sql_timeout"
        assert set(body["error"].keys()) <= {"code", "message"}
        message = body["error"]["message"]
        assert "customers" not in message
        assert "OperationalError" not in message
        assert "interrupted" not in message
        assert "Traceback" not in message
        assert set(body.keys()) == {"error", "meta"}
        assert set(body["meta"].keys()) == {"as_of"}
    finally:
        eng.stop()
        conn.close()


def test_sql_row_limit_truncates_with_explicit_flag(tmp_path):
    dir_path = tmp_path / "perf06-rowlimit"
    eng, conn = _build_engine(
        dir_path,
        scenario_overrides={"interface.sql_max_rows": 3},
    )
    eng.start()
    try:
        conn.execute("BEGIN")
        for i in range(_ROW_LIMIT_CUSTOMER_COUNT):
            conn.execute(
                "INSERT INTO _customers "
                "(id, name, segment, status, created_at, modified_at, origin) "
                "VALUES (?, ?, 'commercial', 'active', 500, 500, 'run')",
                (i + 1, f"Customer {i}"),
            )
        conn.execute("COMMIT")

        _open_session(eng)
        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id FROM customers ORDER BY id"},
        )
        assert status == 200
        data = body["data"]
        assert data["truncated"] is True
        assert data["row_count"] == 3
        assert len(data["rows"]) == 3
        assert [row[0] for row in data["rows"]] == [1, 2, 3]

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id FROM customers ORDER BY id", "max_rows": 1},
        )
        assert status == 200
        data = body["data"]
        assert data["truncated"] is True
        assert data["row_count"] == 1
        assert data["rows"] == [[1]]

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id FROM customers WHERE id <= 2 ORDER BY id"},
        )
        assert status == 200
        data = body["data"]
        assert data["truncated"] is False
        assert data["row_count"] == 2
    finally:
        eng.stop()
        conn.close()


def test_max_rows_may_only_lower_the_configured_ceiling(tmp_path):
    dir_path = tmp_path / "perf06-rowlimit-raise"
    eng, conn = _build_engine(
        dir_path,
        scenario_overrides={"interface.sql_max_rows": 2},
    )
    eng.start()
    try:
        conn.execute("BEGIN")
        for i in range(_ROW_LIMIT_CUSTOMER_COUNT):
            conn.execute(
                "INSERT INTO _customers "
                "(id, name, segment, status, created_at, modified_at, origin) "
                "VALUES (?, ?, 'commercial', 'active', 500, 500, 'run')",
                (i + 1, f"Customer {i}"),
            )
        conn.execute("COMMIT")

        _open_session(eng)
        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id FROM customers ORDER BY id", "max_rows": 10_000},
        )
        assert status == 200
        data = body["data"]
        assert data["row_count"] == 2
        assert data["truncated"] is True
    finally:
        eng.stop()
        conn.close()


def test_concurrent_requests_serialize_without_interleaved_state_corruption(tmp_path, monkeypatch):
    eng, conn = _build_engine(tmp_path / "perf06-concurrency")
    eng.start()
    try:
        intervals = _instrument_sql_serialization(monkeypatch)
        conn.execute("BEGIN")
        for i in range(16):
            conn.execute(
                "INSERT INTO _customers "
                "(id, name, segment, status, created_at, modified_at, origin) "
                "VALUES (?, ?, 'commercial', 'active', 500, 500, 'run')",
                (i + 1, f"Customer {i}"),
            )
        conn.execute("COMMIT")

        _open_session(eng)
        results: dict[int, tuple[int, dict]] = {}
        lock = threading.Lock()

        def worker(customer_id: int) -> None:
            status, body, _ = _request(
                eng.port,
                "POST",
                "/v1/sql",
                token=eng.tokens.agent,
                body={"sql": f"SELECT id, name FROM customers WHERE id = {customer_id}"},
            )
            with lock:
                results[customer_id] = (status, body)

        threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(16)]
        for t in threads:
            t.start()
        _join_all_or_fail(threads, timeout=10)

        assert len(results) == 16
        for customer_id, (status, body) in results.items():
            assert status == 200, (customer_id, body)
            rows = body["data"]["rows"]
            assert rows == [[customer_id, f"Customer {customer_id - 1}"]], (customer_id, rows)
        assert len(intervals) == 16
        _assert_no_overlap(intervals)
    finally:
        eng.stop()
        conn.close()


def test_client_disconnect_mid_slow_query_releases_lock_promptly_and_next_request_succeeds(tmp_path):
    eng, conn = _build_engine(tmp_path / "perf06-wedge", with_slow_customers=True)
    eng.start()
    try:
        _open_session(eng)

        natural_elapsed = _measure_natural_runtime(eng.port, eng.tokens.agent, _SLOW_SQL)
        if natural_elapsed <= 1.5:
            pytest.skip("slow query finishes too fast on this machine to discriminate the wedge")
        acquire_window = natural_elapsed / 3

        payload = b'{"sql": "' + _SLOW_SQL.encode("ascii") + b'"}'
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
            eng.port, "POST", "/v1/sql", token=eng.tokens.agent, body={"sql": "SELECT 1 AS one"}
        )
        elapsed = time.monotonic() - start
        assert status == 200, body
        assert body["data"] == {
            "columns": ["one"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }
        assert elapsed < acquire_window * 2
    finally:
        eng.stop()
        conn.close()
