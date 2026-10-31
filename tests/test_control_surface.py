from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
)
from trades_bench import SPEC_VERSION, clock, geo_reader
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.enums import RunStatus
from trades_bench.events import Events
from trades_bench.http_engine import Engine, open_engine_connection
from trades_bench.invocations import PROMPT_TEMPLATE_VERSION, Invocations
from trades_bench.ledger import Ledger
from trades_bench.rng import Rng, sqlite_audit_recorder
from trades_bench.run_init import initialize_run
from trades_bench.txn import Services

SEED = 5
START_DATE = "2026-10-01"
END_DATE = "2026-10-02"
MAX_INVOCATIONS = 400


def _config():
    return load_run_config(
        scenario_json=json.dumps(
            {
                "keys": {
                    "geography.artifact_id": GEO_ARTIFACT_ID,
                    "geography.artifact_sha256": geo_artifact_sha256(),
                    "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
                    "run.start_date": START_DATE,
                    "run.end_date": END_DATE,
                }
            }
        )
    )


class _ControlClient:

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.next_invocation_count = 0
        self.last_raw = b""

    def post(self, operation: str, body: dict, *, token: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.engine.port, timeout=15)
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.engine.tokens.control if token is None else token}",
        }
        try:
            conn.request("POST", f"/v1/control/{operation}", body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            replay = response.getheader("X-CSR-Idempotent-Replay")
        finally:
            conn.close()
        self.last_raw = raw
        return status, json.loads(raw), replay

    def next_invocation(self) -> dict:
        self.next_invocation_count += 1
        status, body, _replay = self.post(
            "next_invocation", {"request_id": f"next-invocation-{self.next_invocation_count}"}
        )
        assert status == 200, body
        return body["data"]

    def complete(self, invocation_id: int):
        return self.post(
            "complete_invocation",
            {
                "request_id": f"complete-invocation-{invocation_id}",
                "invocation_id": invocation_id,
            },
        )

    def drive_to_call(self) -> dict:
        for _ in range(MAX_INVOCATIONS):
            invocation = self.next_invocation()
            assert "run_finished" not in invocation, invocation
            if invocation["kind"] == "call":
                return invocation
            status, body, _replay = self.complete(invocation["invocation_id"])
            assert status == 200, body
        raise AssertionError("the run produced no call invocation")


def _build_engine(dir_path: Path) -> tuple[Engine, object]:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / "run.db"
    config = _config()
    initialize_run(path, master_seed=SEED, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_engine_connection(path)
    rng = Rng(SEED, SPEC_VERSION, audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    services = Services(
        conn=conn,
        rng=rng,
        ledger=Ledger(conn),
        invocations=Invocations(conn),
        clock=clock,
        events=Events(conn, rng),
    )
    engine = Engine(
        services=services,
        config=config,
        db_path=path,
        artifact=geo_reader.load_artifact(GEO_ARTIFACT_DIR),
    )
    return engine, conn


@pytest.fixture
def control(tmp_path):
    engine, conn = _build_engine(tmp_path / "control-surface")
    engine.start()
    try:
        yield _ControlClient(engine)
    finally:
        engine.stop()
        conn.close()


def test_next_invocation_returns_the_opening_invocation_and_the_events_it_applied(control):
    invocation = control.next_invocation()
    assert invocation["kind"] == "opening"
    assert invocation["prompt_version"] == PROMPT_TEMPLATE_VERSION
    assert invocation["payload"]["service_date"] == START_DATE
    assert invocation["payload"]["time"].startswith(f"{START_DATE}T06:45")
    kinds = [event["kind"] for event in invocation["applied_events"]]
    assert kinds[0] == "day_rollover"
    assert "demand_generation" in kinds
    assert all(event["event_id"] > 0 for event in invocation["applied_events"])


def test_acceptance33_next_invocation_rejects_a_caller_supplied_simulated_time(control):
    status, body, _replay = control.post(
        "next_invocation", {"request_id": "next-invocation-1", "ts_ms": 1_000_000}
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "ts_ms"


def test_acceptance33_next_invocation_rejects_a_caller_supplied_kind(control):
    status, body, _replay = control.post(
        "next_invocation", {"request_id": "next-invocation-1", "type": "closing"}
    )
    assert status == 400
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["field"] == "type"


def test_acceptance33_an_uncompleted_invocation_cannot_be_skipped(control):
    first = control.next_invocation()
    second = control.next_invocation()
    assert second["invocation_id"] == first["invocation_id"]
    assert second["kind"] == first["kind"]
    assert second["ts_ms"] == first["ts_ms"]
    count = control.engine.services.conn.execute(
        "SELECT COUNT(*) c FROM _invocations"
    ).fetchone()["c"]
    assert count == 1


def test_acceptance33_an_invented_invocation_id_is_rejected(control):
    control.next_invocation()
    status, body, _replay = control.complete(999_999)
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_acceptance33_completing_an_earlier_invocation_again_is_rejected(control):
    first = control.next_invocation()
    status, _body, _replay = control.complete(first["invocation_id"])
    assert status == 200
    second = control.next_invocation()
    assert second["invocation_id"] != first["invocation_id"]

    status, body, _replay = control.post(
        "complete_invocation",
        {
            "request_id": "complete-invocation-out-of-order",
            "invocation_id": first["invocation_id"],
        },
    )
    assert status == 409
    assert body["error"]["code"] == "conflict"


def test_acceptance33_a_call_invocation_with_an_unsettled_call_cannot_complete(control):
    invocation = control.drive_to_call()
    status, body, _replay = control.complete(invocation["invocation_id"])
    assert status == 409
    assert body["error"]["code"] == "conflict"
    assert body["error"]["details"]["rule"] == "call_still_active"


def test_attempt_ops_record_incrementally_and_replay_by_request_id(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    conn = control.engine.services.conn

    status, body, replay = control.post(
        "record_call_attempt",
        {
            "request_id": f"call-attempt-{call_id}-1",
            "call_id": call_id,
            "attempt_number": 1,
        },
    )
    assert status == 200, body
    assert replay is None
    assert body["data"] == {"call_id": call_id, "attempt_number": 1}

    first_raw = control.last_raw

    status, replayed, replay_header = control.post(
        "record_call_attempt",
        {
            "request_id": f"call-attempt-{call_id}-1",
            "call_id": call_id,
            "attempt_number": 1,
        },
    )
    assert status == 200
    assert replayed["data"] == body["data"]
    assert control.last_raw == first_raw
    assert replay_header == "1"

    recorded = conn.execute(
        "SELECT COUNT(*) c FROM _call_attempts WHERE call_id = ? AND outcome = 'incomplete'",
        (call_id,),
    ).fetchone()["c"]
    assert recorded == 1


def test_mark_call_unanswered_before_three_recorded_attempts_is_rejected(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    status, body, _replay = control.post(
        "mark_call_unanswered", {"request_id": f"unanswered-{call_id}", "call_id": call_id}
    )
    assert status == 409
    assert body["error"]["details"]["rule"] == "three_attempt_limit"


def test_the_final_unanswered_attempt_settles_the_call_and_frees_the_invocation(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    for attempt in (1, 2):
        status, body, _replay = control.post(
            "record_call_attempt",
            {
                "request_id": f"call-attempt-{call_id}-{attempt}",
                "call_id": call_id,
                "attempt_number": attempt,
            },
        )
        assert status == 200, body

    status, body, _replay = control.post(
        "record_final_unanswered_attempt",
        {
            "request_id": f"call-final-{call_id}",
            "call_id": call_id,
            "attempt_number": 3,
        },
    )
    assert status == 200, body
    assert body["data"]["outcome"] == "unanswered"

    status, body, _replay = control.complete(invocation["invocation_id"])
    assert status == 200, body
    assert body["data"] == {"invocation_id": invocation["invocation_id"], "status": "completed"}


def test_record_call_attempt_out_of_order_reports_the_expected_attempt_number(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    status, body, _replay = control.post(
        "record_call_attempt",
        {
            "request_id": f"call-attempt-{call_id}-2",
            "call_id": call_id,
            "attempt_number": 2,
        },
    )
    assert status == 409
    assert body["error"]["details"]["rule"] == "attempt_number_mismatch"
    assert body["error"]["details"]["expected"] == 1

    status, body, _replay = control.post(
        "record_call_attempt",
        {
            "request_id": f"call-attempt-{call_id}-1",
            "call_id": call_id,
            "attempt_number": 1,
        },
    )
    assert status == 200, body

    status, body, _replay = control.post(
        "record_call_attempt",
        {
            "request_id": f"call-attempt-{call_id}-3",
            "call_id": call_id,
            "attempt_number": 3,
        },
    )
    assert status == 409
    assert body["error"]["details"]["expected"] == 2


def test_every_control_operation_replays_a_byte_identical_body(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])

    def assert_replay_is_byte_identical(operation: str, body: dict) -> None:
        status, first, replay = control.post(operation, body)
        assert status == 200, first
        assert replay is None
        first_raw = control.last_raw
        status, again, replay = control.post(operation, body)
        assert status == 200, again
        assert replay == "1"
        assert control.last_raw == first_raw

    assert_replay_is_byte_identical(
        "next_invocation", {"request_id": "replay-next-invocation"}
    )
    for attempt in (1, 2, 3):
        assert_replay_is_byte_identical(
            "record_call_attempt",
            {
                "request_id": f"replay-call-attempt-{call_id}-{attempt}",
                "call_id": call_id,
                "attempt_number": attempt,
            },
        )
    assert_replay_is_byte_identical(
        "mark_call_unanswered",
        {"request_id": f"replay-call-unanswered-{call_id}", "call_id": call_id},
    )
    assert_replay_is_byte_identical(
        "complete_invocation",
        {
            "request_id": f"replay-complete-invocation-{invocation['invocation_id']}",
            "invocation_id": invocation["invocation_id"],
        },
    )
    assert_replay_is_byte_identical(
        "fail_run", {"request_id": "replay-fail-run-1", "reason": "replay check"}
    )


def test_record_final_unanswered_attempt_replays_a_byte_identical_body(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    for attempt in (1, 2):
        status, body, _replay = control.post(
            "record_call_attempt",
            {
                "request_id": f"call-attempt-{call_id}-{attempt}",
                "call_id": call_id,
                "attempt_number": attempt,
            },
        )
        assert status == 200, body

    request = {
        "request_id": f"call-final-{call_id}",
        "call_id": call_id,
        "attempt_number": 3,
    }
    status, body, replay = control.post("record_final_unanswered_attempt", request)
    assert status == 200, body
    assert replay is None
    first_raw = control.last_raw

    status, again, replay = control.post("record_final_unanswered_attempt", request)
    assert status == 200, again
    assert replay == "1"
    assert control.last_raw == first_raw


def test_fail_run_is_idempotent_and_later_control_ops_report_the_terminal_run(control):
    invocation = control.next_invocation()
    status, body, replay = control.post(
        "fail_run", {"request_id": "fail-run-1", "reason": "model retries exhausted"}
    )
    assert status == 200, body
    assert body["data"]["run_status"] == RunStatus.FAILED.value
    assert replay is None

    first_raw = control.last_raw

    status, again, replay = control.post(
        "fail_run", {"request_id": "fail-run-1", "reason": "model retries exhausted"}
    )
    assert status == 200
    assert again["data"] == body["data"]
    assert control.last_raw == first_raw
    assert replay == "1"

    status = control.engine.services.conn.execute("SELECT status FROM _run").fetchone()["status"]
    assert status == RunStatus.FAILED.value

    status, body, _replay = control.complete(invocation["invocation_id"])
    assert status == 409
    assert body["error"]["code"] == "run_not_active"

    finished = control.next_invocation()
    assert finished == {"run_finished": True, "run_status": RunStatus.FAILED.value}


def _wait_for_ready_file(ready_file: Path, process: subprocess.Popen) -> int:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if ready_file.is_file():
            return int(json.loads(ready_file.read_text(encoding="utf-8"))["port"])
        if process.poll() is not None:
            raise AssertionError(f"engine process exited with {process.returncode}")
        time.sleep(0.02)
    raise AssertionError("the engine process never published its ready file")


def test_the_engine_process_serves_an_existing_run_file_with_injected_tokens(tmp_path):
    run_dir = tmp_path / "process"
    run_dir.mkdir()
    path = run_dir / "run.db"
    initialize_run(path, master_seed=SEED, config=_config(), artifact_dir=GEO_ARTIFACT_DIR)
    ready_file = run_dir / "ready.json"
    log_file = run_dir / "engine.log"
    control_token = "c" * 64
    agent_token = "a" * 64
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TRADES_BENCH_CONTROL_TOKEN": control_token,
        "TRADES_BENCH_AGENT_TOKEN": agent_token,
    }
    with log_file.open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "trades_bench",
                "--db",
                str(path),
                "--ready-file",
                str(ready_file),
                "--geo",
                str(GEO_ARTIFACT_DIR),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            port = _wait_for_ready_file(ready_file, process)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request(
                    "POST",
                    "/v1/control/next_invocation",
                    body=json.dumps({"request_id": "next-invocation-1"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {control_token}",
                    },
                )
                response = conn.getresponse()
                status = response.status
                body = json.loads(response.read())
            finally:
                conn.close()
            assert status == 200, body
            assert body["data"]["kind"] == "opening"
        finally:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
    assert process.returncode == 0
    assert control_token not in log_file.read_text(encoding="utf-8", errors="replace")


def test_agent_token_on_every_control_operation_matches_the_unknown_route_bytes(control):
    conn = http.client.HTTPConnection("127.0.0.1", control.engine.port, timeout=15)
    try:
        conn.request(
            "POST",
            "/v1/does-not-exist",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        unknown_status = response.status
        unknown_body = json.loads(response.read())
    finally:
        conn.close()

    for operation in (
        "next_invocation",
        "complete_invocation",
        "record_call_attempt",
        "record_final_unanswered_attempt",
        "mark_call_unanswered",
        "fail_run",
        "call_status",
    ):
        status, body, _replay = control.post(
            operation,
            {"request_id": "agent-token-1"},
            token=control.engine.tokens.agent,
        )
        assert status == unknown_status
        assert body == unknown_body


def _side_effect_fingerprint(conn) -> tuple[str, int]:
    run_id = str(conn.execute("SELECT run_id FROM _run").fetchone()["run_id"])
    stored = int(conn.execute("SELECT COUNT(*) c FROM _idempotency").fetchone()["c"])
    return dump_hash(conn, run_id), stored


def test_call_status_reports_an_active_call_and_changes_nothing(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    conn = control.engine.services.conn
    before = _side_effect_fingerprint(conn)

    for _ in range(2):
        status, body, replay = control.post(
            "call_status", {"request_id": f"call-status-{call_id}", "call_id": call_id}
        )
        assert status == 200, body
        assert replay is None
        assert body["data"] == {"call_id": call_id, "status": "active", "active": True}

    assert _side_effect_fingerprint(conn) == before


def test_call_status_reports_a_settled_call_as_inactive(control):
    invocation = control.drive_to_call()
    call_id = int(invocation["payload"]["call_id"])
    for attempt in (1, 2):
        status, body, _replay = control.post(
            "record_call_attempt",
            {
                "request_id": f"call-attempt-{call_id}-{attempt}",
                "call_id": call_id,
                "attempt_number": attempt,
            },
        )
        assert status == 200, body
    status, body, _replay = control.post(
        "record_final_unanswered_attempt",
        {"request_id": f"call-final-{call_id}", "call_id": call_id, "attempt_number": 3},
    )
    assert status == 200, body
    before = _side_effect_fingerprint(control.engine.services.conn)

    status, body, _replay = control.post(
        "call_status", {"request_id": f"call-status-{call_id}", "call_id": call_id}
    )

    assert status == 200, body
    assert body["data"] == {"call_id": call_id, "status": "unanswered", "active": False}
    assert _side_effect_fingerprint(control.engine.services.conn) == before


def test_call_status_for_an_unknown_call_is_not_found_and_changes_nothing(control):
    control.drive_to_call()
    before = _side_effect_fingerprint(control.engine.services.conn)

    status, body, _replay = control.post(
        "call_status", {"request_id": "call-status-999999", "call_id": 999999}
    )

    assert status == 404, body
    assert body["error"]["code"] == "not_found"
    assert _side_effect_fingerprint(control.engine.services.conn) == before
