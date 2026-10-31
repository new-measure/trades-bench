from __future__ import annotations

import functools
import json
from pathlib import Path

from trades_bench import SPEC_VERSION
from trades_bench.config import RunConfig, load_run_config
from trades_bench.db import in_memory, insert_run, new_run_id
from trades_bench.enums import RunStatus
from trades_bench.events import Events
from trades_bench.geo_artifact import canonical_artifact_sha256
from trades_bench.invocations import Invocations
from trades_bench.ledger import Ledger
from trades_bench.migrations import migrate
from trades_bench.rng import Rng, sqlite_audit_recorder
from trades_bench.txn import Services

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
        spec_version=SPEC_VERSION,
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
    rng = Rng(master_seed, SPEC_VERSION, audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    ledger = Ledger(conn)
    events = Events(conn, rng)
    inv = Invocations(conn)
    if active_invocation:
        conn.execute(
            "INSERT INTO _invocations(type, ts_ms, sequence, status) "
            "VALUES ('call', 0, 1, 'active')"
        )
    sv = Services(conn=conn, rng=rng, ledger=ledger, invocations=inv, events=events)
    return conn, rid, sv, ledger, events, rng


def audit_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]


def next_test_origin(conn) -> str:
    n = conn.execute(
        "SELECT (SELECT COUNT(*) FROM _call_hidden) + (SELECT COUNT(*) FROM _pending_calls) + 1 "
        "AS n"
    ).fetchone()["n"]
    while conn.execute(
        "SELECT 1 FROM _call_hidden WHERE origin_key = :key "
        "UNION ALL SELECT 1 FROM _pending_calls WHERE origin_key = :key "
        "UNION ALL SELECT 1 FROM _customer_repeat_state WHERE origin_key = :key",
        {"key": f"t:{n}"},
    ).fetchone() is not None:
        n += 1
    return f"t:{n}"
