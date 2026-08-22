from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from policies import calibration
from policies.accept_all import AcceptAllPolicy
from policies.do_nothing import DoNothingPolicy
from policies.fork_search import INCLUDE_REJECT, LOOKAHEAD_DEPTH, ForkBudget, run_fork_search
from policies.reject_heavy import RejectHeavyPolicy
from policies.runner import run_policy
from trades_bench.config import RunConfig, load_run_config

EXPERIMENT_REPORT_VERSION = "1.2.0"
DEFAULT_GEO_DIR = Path("data/geography-arizona-v1")
DEFAULT_SEEDS = (101, 202, 303)
DEFAULT_POLICIES = calibration.POLICY_NAMES
CHEAP_POLICIES = (
    calibration.DO_NOTHING_POLICY_NAME,
    calibration.ACCEPT_ALL_POLICY_NAME,
    calibration.REJECT_HEAVY_POLICY_NAME,
)


@dataclass(frozen=True)
class Trial:
    name: str
    overrides: dict[str, Any]


@dataclass(frozen=True)
class ExperimentTask:
    trial: Trial
    scenario_path: str
    master_seed: int
    policy: str
    geo_dir: str
    work_root: str
    horizon_days: int
    budget: ForkBudget


def _scenario_config(path: str | Path, overrides: dict[str, Any]) -> tuple[str, RunConfig]:
    scenario_path = Path(path)
    stored = json.loads(scenario_path.read_text(encoding="utf-8"))
    keys = dict(stored.get("keys", {}))
    keys.update(overrides)
    stored["keys"] = keys
    return scenario_path.stem, load_run_config(
        scenario_json=json.dumps(stored, sort_keys=True, separators=(",", ":"))
    )


def _policy(task: ExperimentTask, config: RunConfig):
    if task.policy == calibration.DO_NOTHING_POLICY_NAME:
        return DoNothingPolicy()
    if task.policy == calibration.ACCEPT_ALL_POLICY_NAME:
        return AcceptAllPolicy()
    if task.policy == calibration.REJECT_HEAVY_POLICY_NAME:
        return RejectHeavyPolicy.from_config(config, master_seed=task.master_seed)
    raise ValueError(f"unsupported non-oracle policy {task.policy!r}")


def _search_payload(result) -> dict[str, Any]:
    return {
        "baseline": result.baseline,
        "baseline_outcomes": [
            {"baseline": outcome.baseline, "score_cents": outcome.score_cents}
            for outcome in result.baseline_outcomes
        ],
        "decisions": [
            {
                "call_id": decision.call_id,
                "chosen_rank": decision.chosen_rank,
                "outcomes": [
                    {
                        "rank": outcome.rank,
                        "reachable": outcome.reachable,
                        "run_status": outcome.run_status,
                        "score_cents": outcome.score_cents,
                    }
                    for outcome in decision.outcomes
                ],
                "service_date": decision.service_date,
            }
            for decision in result.decisions
        ],
    }


def _report_row(
    task: ExperimentTask,
    *,
    scenario: str,
    config: RunConfig,
    report,
    elapsed_seconds: float,
    search: dict[str, Any] | None,
) -> dict[str, Any]:
    row = {
        "appointments": dict(report["appointments"]),
        "bankruptcy_date": report["bankruptcy_date"],
        "bookings_by_urgency": {
            str(urgency): dict(counts)
            for urgency, counts in report["bookings_by_urgency"].items()
        },
        "calls": {
            str(kind): dict(statuses)
            for kind, statuses in report["calls"].items()
        },
        "config_hash": config.config_hash,
        "config_version": config.config_version,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "final_cash_cents": int(report["final_cash_cents"]),
        "ledger_by_category": dict(report["ledger_by_category"]),
        "master_seed": task.master_seed,
        "policy": task.policy,
        "run_status": str(report["run_status"]),
        "scenario": scenario,
        "trial": task.trial.name,
        "utilization": dict(report["utilization"]),
    }
    if search is not None:
        row["search"] = search
    return row


def _error_row(
    task: ExperimentTask,
    *,
    scenario: str,
    config: RunConfig,
    elapsed_seconds: float,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "config_hash": config.config_hash,
        "config_version": config.config_version,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "error": f"{type(exc).__name__}: {exc}",
        "master_seed": task.master_seed,
        "policy": task.policy,
        "scenario": scenario,
        "trial": task.trial.name,
    }


def _run_task(task: ExperimentTask) -> dict[str, Any]:
    scenario, config = _scenario_config(task.scenario_path, task.trial.overrides)
    task_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{task.trial.name}-{scenario}-{task.master_seed}-{task.policy}-",
            dir=task.work_root,
        )
    )
    started = time.monotonic()
    try:
        if task.policy == calibration.ORACLE_POLICY_NAME:
            result = run_fork_search(
                config=config,
                master_seed=task.master_seed,
                run_dir=task_dir / task.policy,
                geo_dir=task.geo_dir,
                scenario=scenario,
                horizon_days=task.horizon_days,
                budget=task.budget,
            )
            report = result.run.report
            search = _search_payload(result)
        else:
            result = run_policy(
                _policy(task, config),
                config=config,
                master_seed=task.master_seed,
                run_dir=task_dir / task.policy,
                geo_dir=task.geo_dir,
                scenario=scenario,
                horizon_days=task.horizon_days,
                sleep=lambda _seconds: None,
            )
            report = result.report
            search = None
        return _report_row(
            task,
            scenario=scenario,
            config=config,
            report=report,
            elapsed_seconds=time.monotonic() - started,
            search=search,
        )
    except Exception as exc:
        return _error_row(
            task,
            scenario=scenario,
            config=config,
            elapsed_seconds=time.monotonic() - started,
            exc=exc,
        )
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def _run_tasks(tasks: list[ExperimentTask], workers: int) -> list[dict[str, Any]]:
    if workers == 1:
        rows = [_run_task(task) for task in tasks]
    else:
        rows = []
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run_task, task): task for task in tasks}
            for future in as_completed(futures):
                rows.append(future.result())
    return sorted(
        rows,
        key=lambda row: (
            str(row["trial"]),
            str(row["scenario"]),
            int(row["master_seed"]),
            str(row["policy"]),
        ),
    )


def _valid_bankruptcy(row: dict[str, Any]) -> bool:
    return (
        "error" not in row
        and row["run_status"] == calibration.BANKRUPT_RUN_STATUS
        and row["bankruptcy_date"] is not None
        and int(row["final_cash_cents"]) < 0
    )


def _valid_accept_all(
    row: dict[str, Any], ceiling: int, *, require_survival: bool = False
) -> bool:
    if not require_survival and _valid_bankruptcy(row):
        return True
    return (
        "error" not in row
        and row["run_status"] == calibration.COMPLETED_RUN_STATUS
        and row["bankruptcy_date"] is None
        and 0 < int(row["final_cash_cents"]) <= ceiling
    )


def _row_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str, int, str], dict[str, Any]]:
    return {
        (
            str(row["trial"]),
            str(row["scenario"]),
            int(row["master_seed"]),
            str(row["policy"]),
        ): row
        for row in rows
    }


def _cheap_failures(
    trial: Trial,
    scenario_paths: tuple[Path, ...],
    seeds: tuple[int, ...],
    rows: list[dict[str, Any]],
    *,
    require_accept_all_survival: bool = False,
) -> list[dict[str, Any]]:
    indexed = _row_index(rows)
    failures: list[dict[str, Any]] = []
    for scenario_path in scenario_paths:
        scenario, config = _scenario_config(scenario_path, trial.overrides)
        ceiling = config.get_int(calibration.ACCEPT_ALL_MAX_FINAL_CASH_KEY)
        for seed in seeds:
            checks = (
                (
                    calibration.DO_NOTHING_POLICY_NAME,
                    _valid_bankruptcy(
                        indexed[
                            (
                                trial.name,
                                scenario,
                                seed,
                                calibration.DO_NOTHING_POLICY_NAME,
                            )
                        ]
                    ),
                ),
                (
                    calibration.ACCEPT_ALL_POLICY_NAME,
                    _valid_accept_all(
                        indexed[
                            (
                                trial.name,
                                scenario,
                                seed,
                                calibration.ACCEPT_ALL_POLICY_NAME,
                            )
                        ],
                        ceiling,
                        require_survival=require_accept_all_survival,
                    ),
                ),
                (
                    calibration.REJECT_HEAVY_POLICY_NAME,
                    _valid_bankruptcy(
                        indexed[
                            (
                                trial.name,
                                scenario,
                                seed,
                                calibration.REJECT_HEAVY_POLICY_NAME,
                            )
                        ]
                    ),
                ),
            )
            failures.extend(
                {
                    "master_seed": seed,
                    "policy": policy,
                    "scenario": scenario,
                }
                for policy, passed in checks
                if not passed
            )
    return failures


def _trial_summary(
    trial: Trial,
    scenario_paths: tuple[Path, ...],
    seeds: tuple[int, ...],
    rows: list[dict[str, Any]],
    oracle_requested: bool,
    oracle_skipped: bool,
    require_accept_all_survival: bool = False,
) -> dict[str, Any]:
    indexed = _row_index(rows)
    cheap_failures = _cheap_failures(
        trial,
        scenario_paths,
        seeds,
        rows,
        require_accept_all_survival=require_accept_all_survival,
    )
    oracle_failures: list[dict[str, Any]] = []
    gaps: list[int] = []
    if oracle_requested and not oracle_skipped:
        for scenario_path in scenario_paths:
            scenario = scenario_path.stem
            for seed in seeds:
                accept_all = indexed[
                    (trial.name, scenario, seed, calibration.ACCEPT_ALL_POLICY_NAME)
                ]
                oracle = indexed[
                    (trial.name, scenario, seed, calibration.ORACLE_POLICY_NAME)
                ]
                valid = (
                    "error" not in oracle
                    and oracle["run_status"] == calibration.COMPLETED_RUN_STATUS
                    and oracle["bankruptcy_date"] is None
                    and int(oracle["final_cash_cents"])
                    > int(accept_all["final_cash_cents"])
                )
                if valid:
                    gaps.append(
                        int(oracle["final_cash_cents"])
                        - int(accept_all["final_cash_cents"])
                    )
                else:
                    oracle_failures.append(
                        {
                            "master_seed": seed,
                            "policy": calibration.ORACLE_POLICY_NAME,
                            "scenario": scenario,
                        }
                    )
    objective = None
    if gaps:
        objective = {
            "median_oracle_gap_cents": int(statistics.median_low(gaps)),
            "min_oracle_gap_cents": min(gaps),
        }
    return {
        "cheap_failures": cheap_failures,
        "feasible": not cheap_failures
        and (
            not oracle_requested
            or (not oracle_failures and not oracle_skipped)
        ),
        "objective": objective,
        "oracle_failures": oracle_failures,
        "oracle_skipped": oracle_skipped,
        "overrides": trial.overrides,
        "trial": trial.name,
    }


def _load_trials(path: Path | None) -> tuple[Trial, ...]:
    if path is None:
        return (Trial(name="baseline", overrides={}),)
    stored = json.loads(path.read_text(encoding="utf-8"))
    entries = stored["trials"] if isinstance(stored, dict) else stored
    trials = tuple(
        Trial(
            name=str(entry["name"]),
            overrides=dict(entry.get("overrides", {})),
        )
        for entry in entries
    )
    if not trials:
        raise ValueError("the experiment needs at least one trial")
    names = [trial.name for trial in trials]
    if len(names) != len(set(names)):
        raise ValueError("trial names must be unique")
    return trials


def _stored_seeds(path: Path | None) -> tuple[int, ...] | None:
    if path is None:
        return None
    stored = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(stored, dict) or "seeds" not in stored:
        return None
    seeds = tuple(int(value) for value in stored["seeds"])
    if not seeds:
        raise ValueError("a trial file seed list needs at least one seed")
    if len(seeds) != len(set(seeds)):
        raise ValueError("trial file seeds must be unique")
    return seeds


def _resolve_seeds(argument: str | None, trials_path: Path | None) -> tuple[int, ...]:
    if argument:
        return _csv_ints(argument)
    stored = _stored_seeds(trials_path)
    if stored is not None:
        return stored
    return DEFAULT_SEEDS


def _csv_names(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _csv_ints(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _csv_names(value))


def _scenario_paths(names: tuple[str, ...]) -> tuple[Path, ...]:
    available = {
        Path(filename).stem: calibration.SCENARIO_DIR / filename
        for filename in calibration.SCENARIO_FILENAMES
    }
    unknown = sorted(set(names) - set(available))
    if unknown:
        raise ValueError(f"unknown scenarios: {', '.join(unknown)}")
    return tuple(available[name] for name in names)


def _tasks(
    *,
    trials: tuple[Trial, ...],
    scenario_paths: tuple[Path, ...],
    seeds: tuple[int, ...],
    policies: tuple[str, ...],
    geo_dir: Path,
    work_root: Path,
    horizon_days: int,
    budget: ForkBudget,
) -> list[ExperimentTask]:
    return [
        ExperimentTask(
            trial=trial,
            scenario_path=str(scenario_path),
            master_seed=seed,
            policy=policy,
            geo_dir=str(geo_dir),
            work_root=str(work_root),
            horizon_days=horizon_days,
            budget=budget,
        )
        for trial in trials
        for scenario_path in scenario_paths
        for seed in seeds
        for policy in policies
    ]


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    trials = _load_trials(args.trials)
    scenario_paths = _scenario_paths(_csv_names(args.scenarios))
    seeds = _resolve_seeds(args.seeds, args.trials)
    policies = _csv_names(args.policies)
    unknown_policies = sorted(set(policies) - set(DEFAULT_POLICIES))
    if unknown_policies:
        raise ValueError(f"unknown policies: {', '.join(unknown_policies)}")
    if set(CHEAP_POLICIES) - set(policies):
        raise ValueError("experiments must include all three reference policies")
    budget = ForkBudget(
        forks_per_call=args.forks_per_call,
        decisions_per_day=args.decisions_per_day,
        decision_limit=args.decision_limit,
        lookahead_depth=args.lookahead_depth,
        include_reject=args.include_reject,
    )
    geo_dir = args.geo_dir.resolve()
    output_path = args.output.resolve()
    work_root = args.work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    cheap_tasks = _tasks(
        trials=trials,
        scenario_paths=scenario_paths,
        seeds=seeds,
        policies=tuple(policy for policy in policies if policy in CHEAP_POLICIES),
        geo_dir=geo_dir,
        work_root=work_root,
        horizon_days=args.horizon_days,
        budget=budget,
    )
    rows = _run_tasks(cheap_tasks, args.workers)
    feasible_trials = tuple(
        trial
        for trial in trials
        if not _cheap_failures(
            trial,
            scenario_paths,
            seeds,
            rows,
            require_accept_all_survival=args.require_accept_all_survival,
        )
    )
    oracle_requested = calibration.ORACLE_POLICY_NAME in policies
    oracle_trials = (
        trials
        if args.run_oracle_on_infeasible
        else feasible_trials
    )
    if oracle_requested and oracle_trials:
        oracle_tasks = _tasks(
            trials=oracle_trials,
            scenario_paths=scenario_paths,
            seeds=seeds,
            policies=(calibration.ORACLE_POLICY_NAME,),
            geo_dir=geo_dir,
            work_root=work_root,
            horizon_days=args.horizon_days,
            budget=budget,
        )
        rows.extend(_run_tasks(oracle_tasks, args.workers))
        rows.sort(
            key=lambda row: (
                str(row["trial"]),
                str(row["scenario"]),
                int(row["master_seed"]),
                str(row["policy"]),
            )
        )
    summaries = [
        _trial_summary(
            trial,
            scenario_paths,
            seeds,
            rows,
            oracle_requested=oracle_requested,
            oracle_skipped=oracle_requested and trial not in oracle_trials,
            require_accept_all_survival=args.require_accept_all_survival,
        )
        for trial in trials
    ]
    report = {
        "budget": {
            "decision_limit": budget.decision_limit,
            "decisions_per_day": budget.decisions_per_day,
            "forks_per_call": budget.forks_per_call,
            "include_reject": budget.include_reject,
            "lookahead_depth": budget.lookahead_depth,
        },
        "experiment_report_version": EXPERIMENT_REPORT_VERSION,
        "horizon_days": args.horizon_days,
        "policies": list(policies),
        "require_accept_all_survival": args.require_accept_all_survival,
        "revision": os.environ.get("EXPERIMENT_REVISION"),
        "rows": rows,
        "scenarios": [path.stem for path in scenario_paths],
        "seeds": list(seeds),
        "summaries": summaries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m policies.experiment")
    result.add_argument("--trials", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--geo-dir", type=Path, default=DEFAULT_GEO_DIR)
    result.add_argument("--scenarios", default="oct_default,july_monsoon")
    result.add_argument("--seeds")
    result.add_argument("--policies", default=",".join(DEFAULT_POLICIES))
    result.add_argument("--workers", type=int, default=1)
    result.add_argument("--horizon-days", type=int, default=14)
    result.add_argument("--forks-per-call", type=int, default=2)
    result.add_argument("--decisions-per-day", type=int, default=1)
    result.add_argument("--decision-limit", type=int, default=8)
    result.add_argument("--lookahead-depth", type=int, default=LOOKAHEAD_DEPTH)
    result.add_argument(
        "--include-reject",
        action=argparse.BooleanOptionalAction,
        default=INCLUDE_REJECT,
    )
    result.add_argument("--run-oracle-on-infeasible", action="store_true")
    result.add_argument("--require-accept-all-survival", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.workers < 1:
        raise ValueError("workers must be a positive integer")
    report = run_experiment(args)
    print(json.dumps(report["summaries"], sort_keys=True, indent=2))
    return 0 if any(summary["feasible"] for summary in report["summaries"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
