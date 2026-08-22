from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from policies.accept_all import AcceptAllPolicy
from policies.base import DEFAULT_HORIZON_DAYS
from policies.do_nothing import DoNothingPolicy
from policies.fork_search import ForkBudget, run_fork_search
from policies.reject_heavy import RejectHeavyPolicy
from policies.runner import report_bytes, run_policy
from trades_bench import SPEC_VERSION, run_init
from trades_bench._dump import dump
from trades_bench.config import (
    RunConfig,
    canonical_config_bytes,
    config_from_canonical,
    load_run_config,
)

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
SCENARIO_FILENAMES: tuple[str, ...] = ("oct_default.json", "july_monsoon.json")

CALIBRATION_REPORT_FILENAME = "calibration-report.json"
CALIBRATION_REPORT_VERSION = "1.1.0"

DO_NOTHING_POLICY_NAME = "do_nothing"
ORACLE_POLICY_NAME = "oracle_fork_search"
ACCEPT_ALL_POLICY_NAME = "accept_all"
REJECT_HEAVY_POLICY_NAME = "reject_heavy"

POLICY_NAMES: tuple[str, ...] = (
    DO_NOTHING_POLICY_NAME,
    ACCEPT_ALL_POLICY_NAME,
    REJECT_HEAVY_POLICY_NAME,
    ORACLE_POLICY_NAME,
)

BANKRUPT_RUN_STATUS = "bankrupt"
COMPLETED_RUN_STATUS = "completed"

ACCEPT_ALL_MAX_FINAL_CASH_KEY = "calibration.accept_all_max_final_cash"
SEED_SET_KEY = "calibration.seed_set"

DO_NOTHING_BANKRUPTCY_CHECK = "do_nothing_bankrupt_every_seed"
REJECT_HEAVY_BANKRUPTCY_CHECK = "reject_heavy_bankrupt_every_seed"
ACCEPT_ALL_OUTCOME_CHECK = "accept_all_bankrupt_or_thin_survival"
ORACLE_OUTCOME_CHECK = "oracle_completed_above_accept_all"

CHECK_NAMES: tuple[str, ...] = (
    DO_NOTHING_BANKRUPTCY_CHECK,
    REJECT_HEAVY_BANKRUPTCY_CHECK,
    ACCEPT_ALL_OUTCOME_CHECK,
    ORACLE_OUTCOME_CHECK,
)

LEDGER_TABLE = "_ledger_entries"
RUN_ID_SQL = "SELECT run_id FROM _run"


@dataclass(frozen=True)
class PolicyRow:

    scenario: str
    master_seed: int
    policy: str
    final_cash_cents: int
    run_status: str
    bankruptcy_date: str | None
    dump_hash: str
    ledger_digest: str
    ledger_by_category: Mapping[str, int]
    bookings_by_urgency: Mapping[str, Mapping[str, int]]
    appointments: Mapping[str, int]
    utilization: Mapping[str, int]
    daily_cash_cents: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bankruptcy_date": self.bankruptcy_date,
            "dump_hash": self.dump_hash,
            "final_cash_cents": self.final_cash_cents,
            "ledger_digest": self.ledger_digest,
            "master_seed": self.master_seed,
            "policy": self.policy,
            "run_status": self.run_status,
            "scenario": self.scenario,
            "diagnostics": {
                "appointments": dict(self.appointments),
                "bookings_by_urgency": {
                    urgency: dict(counts)
                    for urgency, counts in self.bookings_by_urgency.items()
                },
                "daily_cash_cents": [list(entry) for entry in self.daily_cash_cents],
                "ledger_by_category": dict(self.ledger_by_category),
                "utilization": dict(self.utilization),
            },
        }


@dataclass(frozen=True)
class Check:

    name: str
    passed: bool
    inputs: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"inputs": dict(self.inputs), "name": self.name, "passed": self.passed}


def scenario_config(path: str | Path) -> tuple[str, RunConfig]:
    scenario_path = Path(path)
    return scenario_path.stem, load_run_config(
        scenario_json=scenario_path.read_text(encoding="utf-8")
    )


def shipped_scenarios() -> tuple[tuple[str, RunConfig], ...]:
    return tuple(scenario_config(SCENARIO_DIR / name) for name in SCENARIO_FILENAMES)


def ledger_digest(db_path: str | Path, *, geo_dir: str | Path | None = None) -> str:
    conn = run_init.open_run_db(db_path, artifact_dir=geo_dir)
    try:
        run_id = str(conn.execute(RUN_ID_SQL).fetchone()["run_id"])
        entries = dump(conn, run_id)["tables"][LEDGER_TABLE]  # type: ignore[index]
    finally:
        conn.close()
    payload = json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _row(
    *,
    scenario: str,
    master_seed: int,
    policy: str,
    report: Mapping[str, Any],
    db_path: Path,
    geo_dir: str | Path | None,
) -> PolicyRow:
    bankruptcy_date = report["bankruptcy_date"]
    return PolicyRow(
        scenario=scenario,
        master_seed=master_seed,
        policy=policy,
        final_cash_cents=int(report["final_cash_cents"]),
        run_status=str(report["run_status"]),
        bankruptcy_date=None if bankruptcy_date is None else str(bankruptcy_date),
        dump_hash=str(report["dump_hash"]),
        ledger_digest=ledger_digest(db_path, geo_dir=geo_dir),
        ledger_by_category={
            str(category): int(amount)
            for category, amount in report["ledger_by_category"].items()
        },
        bookings_by_urgency={
            str(urgency): {str(name): int(value) for name, value in counts.items()}
            for urgency, counts in report["bookings_by_urgency"].items()
        },
        appointments={
            str(status): int(count) for status, count in report["appointments"].items()
        },
        utilization={
            str(name): int(value) for name, value in report["utilization"].items()
        },
        daily_cash_cents=tuple(
            (str(service_date), int(cash_cents))
            for service_date, cash_cents in report["daily_cash_cents"]
        ),
    )


def run_world(
    *,
    scenario: str,
    config: RunConfig,
    master_seed: int,
    world_dir: Path,
    geo_dir: str | Path | None,
    horizon_days: int,
    budget: ForkBudget,
    sleep: Callable[[float], None],
    retain_run_databases: bool,
) -> tuple[PolicyRow, ...]:
    rows: list[PolicyRow] = []
    for policy in (
        DoNothingPolicy(),
        AcceptAllPolicy(),
        RejectHeavyPolicy.from_config(config, master_seed=master_seed),
    ):
        result = run_policy(
            policy,
            config=config,
            master_seed=master_seed,
            run_dir=world_dir / policy.name,
            geo_dir=geo_dir,
            scenario=scenario,
            horizon_days=horizon_days,
            sleep=sleep,
        )
        rows.append(
            _row(
                scenario=scenario,
                master_seed=master_seed,
                policy=policy.name,
                report=result.report,
                db_path=result.paths.db_path,
                geo_dir=geo_dir,
            )
        )
        if not retain_run_databases:
            result.paths.db_path.unlink()
    searched = run_fork_search(
        config=config,
        master_seed=master_seed,
        run_dir=world_dir / ORACLE_POLICY_NAME,
        geo_dir=geo_dir,
        scenario=scenario,
        horizon_days=horizon_days,
        budget=budget,
    )
    rows.append(
        _row(
            scenario=scenario,
            master_seed=master_seed,
            policy=ORACLE_POLICY_NAME,
            report=searched.run.report,
            db_path=searched.run.paths.db_path,
            geo_dir=geo_dir,
        )
    )
    if not retain_run_databases:
        searched.run.paths.db_path.unlink()
    return tuple(rows)


def _run_world_task(
    *,
    scenario: str,
    config_version: str,
    canonical_json: str,
    master_seed: int,
    world_dir: Path,
    geo_dir: str | Path | None,
    horizon_days: int,
    budget: ForkBudget,
    retain_run_databases: bool,
) -> tuple[int, tuple[PolicyRow, ...]]:
    config = config_from_canonical(config_version, canonical_json)
    rows = run_world(
        scenario=scenario,
        config=config,
        master_seed=master_seed,
        world_dir=world_dir,
        geo_dir=geo_dir,
        horizon_days=horizon_days,
        budget=budget,
        sleep=time.sleep,
        retain_run_databases=retain_run_databases,
    )
    return master_seed, rows


def _rows_by_policy(
    rows_by_seed: Mapping[int, Sequence[PolicyRow]], policy: str
) -> dict[int, PolicyRow]:
    return {
        seed: next(row for row in rows if row.policy == policy)
        for seed, rows in rows_by_seed.items()
    }


def _outcomes(rows: Mapping[int, PolicyRow], seeds: Sequence[int]) -> list[dict[str, Any]]:
    return [
        {
            "bankruptcy_date": rows[seed].bankruptcy_date,
            "final_cash_cents": rows[seed].final_cash_cents,
            "master_seed": seed,
            "run_status": rows[seed].run_status,
        }
        for seed in seeds
    ]


def _valid_bankruptcy(row: PolicyRow) -> bool:
    return (
        row.run_status == BANKRUPT_RUN_STATUS
        and row.bankruptcy_date is not None
        and row.final_cash_cents < 0
    )


def evaluate_checks(
    *, config: RunConfig, rows_by_seed: Mapping[int, Sequence[PolicyRow]]
) -> tuple[Check, ...]:
    seeds = sorted(rows_by_seed)
    total = len(seeds)

    do_nothing_rows = _rows_by_policy(rows_by_seed, DO_NOTHING_POLICY_NAME)
    do_nothing_failures = [
        seed for seed in seeds if not _valid_bankruptcy(do_nothing_rows[seed])
    ]

    reject_heavy_rows = _rows_by_policy(rows_by_seed, REJECT_HEAVY_POLICY_NAME)
    reject_heavy_failures = [
        seed for seed in seeds if not _valid_bankruptcy(reject_heavy_rows[seed])
    ]

    accept_all_rows = _rows_by_policy(rows_by_seed, ACCEPT_ALL_POLICY_NAME)
    accept_all_max_final_cash = config.get_int(ACCEPT_ALL_MAX_FINAL_CASH_KEY)
    accept_all_failures = [
        seed
        for seed in seeds
        if not (
            _valid_bankruptcy(accept_all_rows[seed])
            or (
                accept_all_rows[seed].run_status == COMPLETED_RUN_STATUS
                and accept_all_rows[seed].bankruptcy_date is None
                and 0 < accept_all_rows[seed].final_cash_cents
                <= accept_all_max_final_cash
            )
        )
    ]

    oracle_rows = _rows_by_policy(rows_by_seed, ORACLE_POLICY_NAME)
    oracle_failures = [
        seed
        for seed in seeds
        if not (
            oracle_rows[seed].run_status == COMPLETED_RUN_STATUS
            and oracle_rows[seed].bankruptcy_date is None
            and oracle_rows[seed].final_cash_cents > accept_all_rows[seed].final_cash_cents
        )
    ]

    return (
        Check(
            name=DO_NOTHING_BANKRUPTCY_CHECK,
            passed=not do_nothing_failures,
            inputs={
                "failing_seeds": do_nothing_failures,
                "outcomes": _outcomes(do_nothing_rows, seeds),
                "seed_count": total,
            },
        ),
        Check(
            name=REJECT_HEAVY_BANKRUPTCY_CHECK,
            passed=not reject_heavy_failures,
            inputs={
                "failing_seeds": reject_heavy_failures,
                "outcomes": _outcomes(reject_heavy_rows, seeds),
                "seed_count": total,
            },
        ),
        Check(
            name=ACCEPT_ALL_OUTCOME_CHECK,
            passed=not accept_all_failures,
            inputs={
                "accept_all_max_final_cash": accept_all_max_final_cash,
                "failing_seeds": accept_all_failures,
                "outcomes": _outcomes(accept_all_rows, seeds),
                "seed_count": total,
            },
        ),
        Check(
            name=ORACLE_OUTCOME_CHECK,
            passed=not oracle_failures,
            inputs={
                "failing_seeds": oracle_failures,
                "outcomes": [
                    {
                        "accept_all_final_cash_cents": accept_all_rows[seed].final_cash_cents,
                        "master_seed": seed,
                        "oracle_final_cash_cents": oracle_rows[seed].final_cash_cents,
                        "oracle_run_status": oracle_rows[seed].run_status,
                    }
                    for seed in seeds
                ],
                "seed_count": total,
            },
        ),
    )


def run_matrix(
    *,
    scenarios: Sequence[tuple[str, RunConfig]],
    seeds: Sequence[int] | None = None,
    out_dir: str | Path,
    geo_dir: str | Path | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    budget: ForkBudget = ForkBudget(),
    sleep: Callable[[float], None] = time.sleep,
    retain_run_databases: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    if not scenarios:
        raise ValueError("the calibration matrix needs at least one scenario")
    if workers < 1:
        raise ValueError("workers must be a positive integer")
    if workers > 1 and sleep is not time.sleep:
        raise ValueError("parallel calibration requires the default sleep function")
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    if seeds is None:
        seed_list = list(scenarios[0][1].get_int_list(SEED_SET_KEY))
    else:
        seed_list = [int(seed) for seed in seeds]
    if not seed_list:
        raise ValueError("the calibration matrix needs at least one seed")

    rows_by_scenario: dict[str, dict[int, tuple[PolicyRow, ...]]] = {
        name: {} for name, _ in scenarios
    }
    if len(rows_by_scenario) != len(scenarios):
        raise ValueError("calibration scenario names must be unique")

    if workers == 1:
        for name, config in scenarios:
            for seed in seed_list:
                rows_by_scenario[name][seed] = run_world(
                    scenario=name,
                    config=config,
                    master_seed=seed,
                    world_dir=base / name / str(seed),
                    geo_dir=geo_dir,
                    horizon_days=horizon_days,
                    budget=budget,
                    sleep=sleep,
                    retain_run_databases=retain_run_databases,
                )
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for name, config in scenarios:
                canonical_json = canonical_config_bytes(
                    config.config_version, config.resolved
                ).decode("utf-8")
                for seed in seed_list:
                    future = executor.submit(
                        _run_world_task,
                        scenario=name,
                        config_version=config.config_version,
                        canonical_json=canonical_json,
                        master_seed=seed,
                        world_dir=base / name / str(seed),
                        geo_dir=geo_dir,
                        horizon_days=horizon_days,
                        budget=budget,
                        retain_run_databases=retain_run_databases,
                    )
                    futures[future] = name
            for future in as_completed(futures):
                seed, rows = future.result()
                rows_by_scenario[futures[future]][seed] = rows

    scenario_reports: list[dict[str, Any]] = []
    for name, config in scenarios:
        rows_by_seed = rows_by_scenario[name]
        scenario_reports.append(
            {
                "checks": [
                    check.as_dict()
                    for check in evaluate_checks(config=config, rows_by_seed=rows_by_seed)
                ],
                "config_hash": config.config_hash,
                "config_version": config.config_version,
                "rows": [
                    row.as_dict() for seed in seed_list for row in rows_by_seed[seed]
                ],
                "scenario": name,
            }
        )

    report = {
        "primary_seed": seed_list[0],
        "report_version": CALIBRATION_REPORT_VERSION,
        "scenarios": scenario_reports,
        "seeds": seed_list,
        "spec_version": SPEC_VERSION,
    }
    (base / CALIBRATION_REPORT_FILENAME).write_bytes(report_bytes(report))
    return report


__all__ = [
    "ACCEPT_ALL_MAX_FINAL_CASH_KEY",
    "ACCEPT_ALL_OUTCOME_CHECK",
    "ACCEPT_ALL_POLICY_NAME",
    "BANKRUPT_RUN_STATUS",
    "CALIBRATION_REPORT_FILENAME",
    "CALIBRATION_REPORT_VERSION",
    "CHECK_NAMES",
    "COMPLETED_RUN_STATUS",
    "DO_NOTHING_BANKRUPTCY_CHECK",
    "DO_NOTHING_POLICY_NAME",
    "LEDGER_TABLE",
    "ORACLE_OUTCOME_CHECK",
    "ORACLE_POLICY_NAME",
    "POLICY_NAMES",
    "REJECT_HEAVY_BANKRUPTCY_CHECK",
    "REJECT_HEAVY_POLICY_NAME",
    "RUN_ID_SQL",
    "SCENARIO_DIR",
    "SCENARIO_FILENAMES",
    "SEED_SET_KEY",
    "Check",
    "PolicyRow",
    "evaluate_checks",
    "ledger_digest",
    "run_matrix",
    "run_world",
    "scenario_config",
    "shipped_scenarios",
]
