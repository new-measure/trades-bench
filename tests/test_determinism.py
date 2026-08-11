from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from _driver import N_COMMITS, RUN_ID, SCRIPT, create_run, open_run, run_steps
from _helpers import register_conn
from csr_bench._dump import RUN_ID_PLACEHOLDER, dump, dump_hash
from csr_bench.db import connect, in_memory, insert_run
from csr_bench.enums import RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate

DRIVER = str(Path(__file__).resolve().with_name("_driver.py"))
REPO_ROOT = str(Path(__file__).resolve().parents[1])

NON_EMPTY_TABLES = (
    "_events",
    "_ledger_entries",
    "_invocations",
    "_rng_state",
    "_random_audit",
)


def _run_driver(*args: str, hash_seed: str = "0", cwd: str = REPO_ROOT) -> dict:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    proc = subprocess.run(
        [sys.executable, DRIVER, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_det01_two_fresh_processes_produce_identical_logical_dump(tmp_path):
    first = tmp_path / "replay_a.db"
    second = tmp_path / "replay_b.db"
    a = _run_driver("--db", str(first), "--create", hash_seed="0", cwd=REPO_ROOT)
    b = _run_driver("--db", str(second), "--create", hash_seed="987654321", cwd=str(tmp_path))

    assert a["hash"] == b["hash"]
    assert a["observations"] == b["observations"]
    assert a["steps"] == [name for name, _ in SCRIPT]


def test_det01_replayed_dump_is_non_trivial(tmp_path):
    path = tmp_path / "replay.db"
    reported = _run_driver("--db", str(path), "--create")["hash"]

    conn = connect(path)
    register_conn(conn)
    assert dump_hash(conn, RUN_ID) == reported
    tables = dump(conn, RUN_ID)["tables"]

    assert len(tables["_run"]) == 1
    for name in NON_EMPTY_TABLES:
        assert tables[name], f"{name} is empty — an identical-dump assertion would be vacuous"
    assert sum(1 for row in tables["_events"] if row["status"] == "applied") >= 4
    assert any(row["status"] == "pending" for row in tables["_events"])
    assert any(
        row["stream_name"] == "weather_process" and row["counter"] > 0
        for row in tables["_rng_state"]
    ), "no sequential weather_process state — _rng_state would not matter at a resume"
    assert len({row["stream_name"] for row in tables["_random_audit"]}) >= 3


@pytest.fixture(scope="module")
def uninterrupted_hash(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("det02") / "uninterrupted.db"
    return _run_driver("--db", str(path), "--create")["hash"]


@pytest.mark.parametrize("boundary", range(1, N_COMMITS))
def test_det02_resume_at_every_committed_boundary(tmp_path, uninterrupted_hash, boundary):
    interrupted = tmp_path / f"stopped_at_{boundary}.db"
    resumed = tmp_path / f"resumed_from_{boundary}.db"

    _run_driver("--db", str(interrupted), "--create", "--first", "1", "--last", str(boundary))
    shutil.copyfile(interrupted, resumed)
    report = _run_driver(
        "--db", str(resumed), "--first", str(boundary + 1), "--last", str(N_COMMITS)
    )

    assert report["hash"] == uninterrupted_hash
    assert report["steps"] == [name for name, _ in SCRIPT][boundary:]


def test_det04_05_interleaved_noise_moves_no_draw(tmp_path):
    quiet, noisy = tmp_path / "quiet.db", tmp_path / "noisy.db"
    create_run(quiet)
    create_run(noisy)
    with open_run(quiet, variant="A") as a, open_run(noisy, variant="B") as b:
        run_steps(a)
        run_steps(b)
        observations_a, observations_b = a.observations, b.observations
        notes_b = b.notes
        tables_a = dump(a.conn, a.run_id)["tables"]
        tables_b = dump(b.conn, b.run_id)["tables"]

    assert len(tables_b["_idempotency"]) > len(tables_a["_idempotency"])
    rejections = [v for label, v in notes_b if label.startswith("rejected:")]
    assert rejections and set(rejections) == {ErrorCode.INSUFFICIENT_CASH.value}
    retries = [
        (label.removeprefix("retry:"), v) for label, v in notes_b if label.startswith("retry:")
    ]
    assert retries

    assert observations_a == observations_b
    sequential = [v for label, v in observations_a if label.startswith("weather_process/")]
    assert len(sequential) >= 3, "weather_process never advanced — DET-05 would be vacuous"

    label_counts = Counter(label for label, _ in observations_a)
    assert any(count > 1 for count in label_counts.values()), (
        "no label repeated in the script — the same-key-same-value check below would be vacuous"
    )
    by_label: dict[str, set[str]] = defaultdict(set)
    for label, value in observations_a:
        by_label[label].add(value)
    assert all(len(values) == 1 for values in by_label.values())

    for name in ("_run", "_events", "_ledger_entries", "_invocations", "_rng_state", "_random_audit"):
        assert tables_a[name] == tables_b[name], f"{name} diverged under agent-side noise"

    stored = {
        label.removeprefix("response:"): v for label, v in notes_b if label.startswith("response:")
    }
    assert stored
    for request_id, replayed in retries:
        assert replayed == stored[request_id]


def test_dump_refuses_a_database_with_an_unregistered_table():
    conn = in_memory()
    register_conn(conn)
    migrate(conn)
    conn.execute("CREATE TABLE _future_ticket_table (x INTEGER)")
    with pytest.raises(EngineError) as excinfo:
        dump(conn, RUN_ID)
    assert excinfo.value.code is ErrorCode.INVARIANT_VIOLATED


def _minimal_run_db(run_id: str) -> sqlite3.Connection:
    conn = in_memory()
    register_conn(conn)
    migrate(conn)
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=run_id,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.ACTIVE,
    )
    conn.execute("COMMIT")
    return conn


def test_dump_normalizes_run_id_to_placeholder():
    conn_a = _minimal_run_db("run-aaaa")
    conn_b = _minimal_run_db("run-bbbb-a-totally-different-length-and-value")

    assert dump_hash(conn_a, "run-aaaa") == dump_hash(
        conn_b, "run-bbbb-a-totally-different-length-and-value"
    )
    row = dump(conn_a, "run-aaaa")["tables"]["_run"][0]
    assert row["run_id"] == RUN_ID_PLACEHOLDER
