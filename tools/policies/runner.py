from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from policies.base import DEFAULT_HORIZON_DAYS, Policy, PolicyAdapter
from trades_bench import clock, run_init
from trades_bench._dump import dump_hash
from trades_bench.config import RunConfig
from trades_bench.constants import BLOCK_GRID_MINUTES
from trades_bench.enums import LedgerCategory
from trades_bench.harness import workspace
from trades_bench.harness.client import ControlClient
from trades_bench.harness.launcher import (
    EngineLaunchSpec,
    EngineTokens,
    agent_env,
    launch_engine,
)
from trades_bench.harness.runner import (
    ControlledRunner,
    HarnessLimits,
    HarnessRunFailed,
    initialize_controlled_run,
)
from trades_bench.harness.trajectory import TrajectoryWriter

REPORT_FILENAME = "policy-report.json"
FAILURE_FILENAME = "run-failure.json"
REPORT_VERSION = "1.0.0"
SCORED_STATUSES: tuple[str, ...] = ("completed", "bankrupt")

DEFAULT_SCENARIO = "defaults"

CALL_KINDS: tuple[str, ...] = ("new_job", "cancellation")
CALL_STATUSES: tuple[str, ...] = (
    "active",
    "booked",
    "declined",
    "rejected",
    "acknowledged",
    "unanswered",
)
APPOINTMENT_TERMINAL_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "no_show",
    "cancelled",
)
URGENCY_CLASSES: tuple[str, ...] = ("standard", "urgent")


@dataclass(frozen=True)
class PolicyRunPaths:

    run_dir: Path
    workspace: Path
    db_path: Path

    @classmethod
    def under(
        cls, run_dir: str | Path, *, workspace: str | Path | None = None
    ) -> PolicyRunPaths:
        base = Path(run_dir).resolve()
        chosen = base / "workspace" if workspace is None else Path(workspace).resolve()
        return cls(run_dir=base, workspace=chosen, db_path=base / "run.db")

    def create(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        workspace.create(self.workspace)


@dataclass(frozen=True)
class PolicyRunResult:

    run_id: str
    run_status: str
    paths: PolicyRunPaths
    report: Mapping[str, Any]
    report_path: Path

    @property
    def report_bytes(self) -> bytes:
        return report_bytes(self.report)


class RunNotScored(RuntimeError):

    def __init__(self, run_status: str, reason: str) -> None:
        super().__init__(f"run ended {run_status}: {reason}")
        self.run_status = run_status
        self.reason = reason


def write_run_failure(
    run_dir: str | Path,
    *,
    run_status: str,
    reason: str,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    path = Path(run_dir) / FAILURE_FILENAME
    payload = {"run_status": run_status, "reason": reason, **dict(extra or {})}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_scored_report(
    run_dir: str | Path,
    report: Mapping[str, Any],
    *,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    run_status = str(report["run_status"])
    if run_status not in SCORED_STATUSES:
        reason = f"the run file reports status {run_status}"
        write_run_failure(run_dir, run_status=run_status, reason=reason, extra=extra)
        raise RunNotScored(run_status, reason)
    path = Path(run_dir) / REPORT_FILENAME
    path.write_bytes(report_bytes(report))
    return path


def crash_reason(exc: BaseException) -> str:
    return f"the harness raised {type(exc).__name__}: {exc}"


def drive_scored(runner: ControlledRunner, run_dir: str | Path) -> str:
    try:
        run_status = runner.run()
    except HarnessRunFailed as exc:
        write_run_failure(run_dir, run_status="failed", reason=exc.reason)
        raise RunNotScored("failed", exc.reason) from exc
    except Exception as exc:
        write_run_failure(run_dir, run_status="failed", reason=crash_reason(exc))
        raise
    if run_status not in SCORED_STATUSES:
        reason = f"the engine finished the run as {run_status}"
        write_run_failure(run_dir, run_status=run_status, reason=reason)
        raise RunNotScored(run_status, reason)
    return run_status


def report_bytes(report: Mapping[str, Any]) -> bytes:
    return json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def run_policy(
    policy: Policy,
    *,
    config: RunConfig,
    master_seed: int,
    run_dir: str | Path,
    geo_dir: str | Path | None = None,
    scenario: str = DEFAULT_SCENARIO,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    dataset_version: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> PolicyRunResult:
    paths = PolicyRunPaths.under(run_dir)
    paths.create()
    adapter = PolicyAdapter(policy, horizon_days=horizon_days)
    run_id = initialize_controlled_run(
        paths.db_path,
        master_seed=master_seed,
        config=config,
        adapter=adapter,
        dataset_version=dataset_version,
        artifact_dir=geo_dir,
    )
    tokens = EngineTokens.mint()
    engine = launch_engine(
        EngineLaunchSpec(db_path=paths.db_path, run_dir=paths.run_dir, geo_dir=geo_dir),
        tokens=tokens,
    )
    try:
        runner = ControlledRunner(
            client=ControlClient(port=engine.port, token=tokens.control),
            adapter=adapter,
            trajectory=TrajectoryWriter(paths.run_dir, run_id),
            workspace=paths.workspace,
            limits=HarnessLimits.from_config(config),
            agent_env=agent_env(
                port=engine.port, token=tokens.agent, workspace=paths.workspace
            ),
            engine=engine,
            sleep=sleep,
        )
        run_status = drive_scored(runner, paths.run_dir)
    finally:
        engine.stop()

    report = build_report(
        paths.db_path,
        policy_name=policy.name,
        config=config,
        scenario=scenario,
        geo_dir=geo_dir,
    )
    report_path = write_scored_report(paths.run_dir, report)
    return PolicyRunResult(
        run_id=run_id,
        run_status=run_status,
        paths=paths,
        report=report,
        report_path=report_path,
    )


def build_report(
    db_path: str | Path,
    *,
    policy_name: str,
    config: RunConfig,
    scenario: str = DEFAULT_SCENARIO,
    geo_dir: str | Path | None = None,
) -> dict[str, Any]:
    conn = run_init.open_run_db(db_path, artifact_dir=geo_dir)
    try:
        return _report(conn, policy_name=policy_name, config=config, scenario=scenario)
    finally:
        conn.close()


def _report(
    conn: sqlite3.Connection, *, policy_name: str, config: RunConfig, scenario: str
) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM _run").fetchone()
    if run is None:
        raise ValueError("the run file carries no run record")
    run_id = str(run["run_id"])
    starting_cash_cents = config.get_int("finance.starting_cash")
    final_cash_cents = _final_cash_cents(conn)
    return {
        "report_version": REPORT_VERSION,
        "policy": policy_name,
        "master_seed": int(run["master_seed"]),
        "scenario": scenario,
        "spec_version": str(run["spec_version"]),
        "config_version": str(run["config_version"]),
        "config_hash": str(run["config_hash"]),
        "first_service_date": str(run["first_service_date"]),
        "last_service_date": str(run["last_service_date"]),
        "run_status": str(run["status"]),
        "starting_cash_cents": starting_cash_cents,
        "final_cash_cents": final_cash_cents,
        "score_cents": final_cash_cents - starting_cash_cents,
        "bankruptcy_date": _bankruptcy_date(conn),
        "dump_hash": dump_hash(conn, run_id),
        "calls": _calls(conn),
        "bookings_by_urgency": _bookings_by_urgency(conn),
        "appointments": _appointments(conn),
        "utilization": _utilization(conn),
        "ledger_by_category": _ledger_by_category(conn),
        "daily_cash_cents": _daily_cash_cents(
            conn,
            first_service_date=str(run["first_service_date"]),
            last_service_date=str(run["last_service_date"]),
            starting_cash_cents=starting_cash_cents,
        ),
    }


def _final_cash_cents(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM _ledger_entries"
    ).fetchone()
    return int(row["total"])


def _bankruptcy_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT service_date FROM _ledger_entries "
        "WHERE origin = 'run' AND balance_after_cents < 0 "
        "ORDER BY occurred_at, id LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["service_date"])


def _calls(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    counts = {
        kind: dict.fromkeys(CALL_STATUSES, 0) for kind in CALL_KINDS
    }
    for row in conn.execute(
        "SELECT call_kind, status, COUNT(*) AS total FROM _calls "
        "GROUP BY call_kind, status"
    ).fetchall():
        counts[str(row["call_kind"])][str(row["status"])] = int(row["total"])
    return counts


def _bookings_by_urgency(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    counts = {
        urgency: {"calls": 0, "booked": 0} for urgency in URGENCY_CLASSES
    }
    for row in conn.execute(
        "SELECT urgent, status, COUNT(*) AS total FROM _calls "
        "WHERE call_kind = 'new_job' GROUP BY urgent, status"
    ).fetchall():
        urgency = URGENCY_CLASSES[1] if int(row["urgent"]) else URGENCY_CLASSES[0]
        total = int(row["total"])
        counts[urgency]["calls"] += total
        if str(row["status"]) == "booked":
            counts[urgency]["booked"] += total
    return counts


def _appointments(conn: sqlite3.Connection) -> dict[str, int]:
    counts = dict.fromkeys(APPOINTMENT_TERMINAL_STATUSES, 0)
    for row in conn.execute(
        "SELECT status, COUNT(*) AS total FROM _appointments GROUP BY status"
    ).fetchall():
        status = str(row["status"])
        if status in counts:
            counts[status] = int(row["total"])
    return counts


def _utilization(conn: sqlite3.Connection) -> dict[str, int]:
    assigned = conn.execute(
        "SELECT COUNT(*) AS total FROM _appointment_blocks ab "
        "JOIN _appointment_assignments aa ON aa.appointment_id = ab.appointment_id "
        "WHERE aa.status = 'active'"
    ).fetchone()
    shift_blocks = 0
    for row in conn.execute(
        "SELECT service_date, start_at, end_at FROM _technician_shifts WHERE active = 1"
    ).fetchall():
        shift_blocks += _blocks_in_shift(
            str(row["service_date"]), int(row["start_at"]), int(row["end_at"])
        )
    return {"assigned_blocks": int(assigned["total"]), "shift_blocks": shift_blocks}


def _blocks_in_shift(service_date: str, start_at: int, end_at: int) -> int:
    day = date.fromisoformat(service_date)
    covered = 0
    for start_minutes, end_minutes in BLOCK_GRID_MINUTES:
        block_start = clock.phoenix_instant(
            day.year, day.month, day.day, start_minutes // 60, start_minutes % 60
        )
        block_end = clock.phoenix_instant(
            day.year, day.month, day.day, end_minutes // 60, end_minutes % 60
        )
        if start_at <= block_start and end_at >= block_end:
            covered += 1
    return covered


def _ledger_by_category(conn: sqlite3.Connection) -> dict[str, int]:
    totals = dict.fromkeys((category.value for category in LedgerCategory), 0)
    for row in conn.execute(
        "SELECT entry_type, COALESCE(SUM(amount_cents), 0) AS total FROM _ledger_entries "
        "GROUP BY entry_type"
    ).fetchall():
        totals[str(row["entry_type"])] = int(row["total"])
    return totals


def _daily_cash_cents(
    conn: sqlite3.Connection,
    *,
    first_service_date: str,
    last_service_date: str,
    starting_cash_cents: int,
) -> list[list[object]]:
    last_by_date: dict[str, int] = {}
    for row in conn.execute(
        "SELECT service_date, balance_after_cents FROM _ledger_entries "
        "ORDER BY occurred_at, id"
    ).fetchall():
        last_by_date[str(row["service_date"])] = int(row["balance_after_cents"])
    carried = starting_cash_cents
    for service_date in sorted(last_by_date):
        if service_date < first_service_date:
            carried = last_by_date[service_date]
    series: list[list[object]] = []
    day = date.fromisoformat(first_service_date)
    final_day = date.fromisoformat(last_service_date)
    while day <= final_day:
        service_date = day.isoformat()
        if service_date in last_by_date:
            carried = last_by_date[service_date]
        series.append([service_date, carried])
        day += timedelta(days=1)
    return series


__all__ = [
    "APPOINTMENT_TERMINAL_STATUSES",
    "CALL_KINDS",
    "CALL_STATUSES",
    "DEFAULT_SCENARIO",
    "FAILURE_FILENAME",
    "REPORT_FILENAME",
    "REPORT_VERSION",
    "SCORED_STATUSES",
    "URGENCY_CLASSES",
    "PolicyRunPaths",
    "PolicyRunResult",
    "RunNotScored",
    "build_report",
    "crash_reason",
    "drive_scored",
    "report_bytes",
    "run_policy",
    "write_run_failure",
    "write_scored_report",
]
