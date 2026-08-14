from __future__ import annotations

import functools
import json
import sqlite3
from pathlib import Path

from csr_bench.config import RunConfig, load_run_config
from csr_bench.db import in_memory, insert_run, new_run_id
from csr_bench.enums import RunStatus
from csr_bench.events import Events
from csr_bench.geo_artifact import canonical_artifact_sha256
from csr_bench.invocations import Invocations
from csr_bench.ledger import Ledger
from csr_bench.migrations import migrate
from csr_bench.rng import Rng
from csr_bench.txn import Services

_OPEN_CONNS: list = []

GEO_ARTIFACT_DIR = Path(__file__).parent / "fixtures" / "geo" / "geography-tempe-fixture"
GEO_ARTIFACT_ID = "tempe-fixture-v1"

GEO_FIXTURE_ZONE_WEIGHTS = [0, 0, 0, 1, 0, 0, 0, 0, 0, 0]


@functools.lru_cache(maxsize=1)
def geo_artifact_sha256() -> str:
    return canonical_artifact_sha256(GEO_ARTIFACT_DIR)


def geo_scenario_json(*, artifact_id: str = GEO_ARTIFACT_ID, artifact_sha256: str | None = None) -> str:
    sha256 = artifact_sha256 if artifact_sha256 is not None else geo_artifact_sha256()
    return json.dumps(
        {
            "keys": {
                "geography.artifact_id": artifact_id,
                "geography.artifact_sha256": sha256,
                "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
            }
        }
    )


def geo_run_config(**kwargs) -> RunConfig:
    return load_run_config(scenario_json=geo_scenario_json(), **kwargs)


def register_conn(conn) -> None:
    _OPEN_CONNS.append(conn)


def audit_recorder(conn: sqlite3.Connection):

    def rec(**kw):
        conn.execute(
            "INSERT INTO _random_audit(stream_name, entity_type, entity_id, scope, "
            "event_kind, draw_index, outcome_id, simulated_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (
                kw["stream_name"],
                kw["entity_type"],
                kw["entity_id"],
                kw["scope"],
                kw["event_kind"],
                kw["draw_index"],
                kw.get("outcome_id"),
            ),
        )

    return rec


def make_services(
    *,
    active_invocation: bool = False,
    status: RunStatus = RunStatus.ACTIVE,
    master_seed: int = 1,
):
    conn = in_memory()
    register_conn(conn)
    migrate(conn)
    rid = new_run_id()
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=master_seed,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=status,
    )
    conn.execute("COMMIT")
    rng = Rng(master_seed, "1.0.0", audit_recorder=audit_recorder(conn), conn=conn)
    ledger = Ledger(conn)
    events = Events(conn, rng)
    inv = Invocations(conn)
    if active_invocation:
        conn.execute(
            "INSERT INTO _invocations(type, ts_ms, sequence, status, attempt_count) "
            "VALUES ('call', 0, 1, 'active', 1)"
        )
    sv = Services(conn=conn, rng=rng, ledger=ledger, invocations=inv, events=events)
    return conn, rid, sv, ledger, events, rng


def audit_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]
