from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from policies import oracle
from policies.base import DEFAULT_HORIZON_DAYS
from policies.runner import (
    DEFAULT_SCENARIO,
    REPORT_FILENAME,
    PolicyRunPaths,
    PolicyRunResult,
    build_report,
    report_bytes,
)
from trades_bench import clock, db, run_init
from trades_bench.config import RunConfig, config_from_canonical
from trades_bench.controller import Controller, OpenInvocation
from trades_bench.enums import InvocationType
from trades_bench.errors import EngineError, ErrorCode

POLICY_NAME = "oracle_fork_search"

FORKS_PER_CALL = 2
FORKED_DECISIONS_PER_DAY = 1
FORKED_DECISION_LIMIT = 8
LOOKAHEAD_DEPTH = 2
INCLUDE_REJECT = True

BRANCH_PREFIX = "br"
FORK_DIR_PREFIX = "oracle-fork-"
FORK_DB_SUFFIX = "-fork.db"
BASELINE_SCRATCH_DIR = "baseline-mainlines"
REJECT_RANK = -1

NEW_JOB_PAYLOAD_KEY = "address"

LEDGER_TOTAL_SQL = "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM _ledger_entries"
RUN_CONFIG_SQL = "SELECT run_id, config_hash FROM _run"


@dataclass(frozen=True)
class ForkBudget:

    forks_per_call: int = FORKS_PER_CALL
    decisions_per_day: int = FORKED_DECISIONS_PER_DAY
    decision_limit: int = FORKED_DECISION_LIMIT
    lookahead_depth: int = LOOKAHEAD_DEPTH
    include_reject: bool = INCLUDE_REJECT

    def __post_init__(self) -> None:
        for name, value in (
            ("forks_per_call", self.forks_per_call),
            ("decisions_per_day", self.decisions_per_day),
            ("decision_limit", self.decision_limit),
            ("lookahead_depth", self.lookahead_depth),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.include_reject, bool):
            raise ValueError("include_reject must be a boolean")


@dataclass(frozen=True)
class BaselineOutcome:

    baseline: str
    score_cents: int


@dataclass(frozen=True)
class BranchOutcome:

    rank: int
    run_status: str
    score_cents: int
    reachable: bool


@dataclass(frozen=True)
class SearchDecision:

    call_id: int
    service_date: str
    chosen_rank: int
    outcomes: tuple[BranchOutcome, ...]


@dataclass(frozen=True)
class ForkSearchResult:

    run: PolicyRunResult
    decisions: tuple[SearchDecision, ...]
    baseline: str = oracle.BASELINE_GREEDY
    baseline_outcomes: tuple[BaselineOutcome, ...] = ()


@dataclass
class BudgetLedger:

    budget: ForkBudget
    spent: int = 0
    spent_by_date: dict[str, int] = field(default_factory=dict)

    def affordable(self, service_date: str) -> bool:
        if self.spent >= self.budget.decision_limit:
            return False
        return self.spent_by_date.get(service_date, 0) < self.budget.decisions_per_day

    def charge(self, service_date: str) -> None:
        self.spent += 1
        self.spent_by_date[service_date] = self.spent_by_date.get(service_date, 0) + 1


class BranchDispatcher(oracle.OracleDispatcher):

    def __init__(
        self,
        read_conn: sqlite3.Connection,
        *,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        branch: str = "",
        override: tuple[int, int] | None = None,
        baseline: str = oracle.BASELINE_GREEDY,
    ) -> None:
        super().__init__(
            read_conn, horizon_days=horizon_days, branch=branch, baseline=baseline
        )
        self.override = override
        self.override_reachable = False

    def _decide(self, prompt):
        if self.override == (int(prompt.payload["call_id"]), REJECT_RANK):
            call_id = int(prompt.payload["call_id"])
            self.override = None
            self.override_reachable = True
            return self._reject(call_id, "fork override")
        return super()._decide(prompt)

    def rank(
        self, call_id: int, ranked: list[oracle.Candidate]
    ) -> list[oracle.Candidate]:
        ordered = super().rank(call_id, ranked)
        if self.override is None:
            return ordered
        target_call_id, target_rank = self.override
        if call_id != target_call_id:
            return ordered
        self.override = None
        if target_rank >= len(ordered):
            return ordered
        if ordered[target_rank].score_cents <= 0:
            return ordered
        self.override_reachable = True
        if target_rank == 0:
            return ordered
        return [ordered[target_rank], *ordered[:target_rank], *ordered[target_rank + 1 :]]


def read_run_config(conn: sqlite3.Connection) -> RunConfig:
    run_row = conn.execute(RUN_CONFIG_SQL).fetchone()
    if run_row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "the fork holds no run record")
    stored = db.read_config(conn, str(run_row["run_id"]))
    if stored is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, "the fork holds no configuration record"
        )
    config_version, canonical_json = stored
    config = config_from_canonical(config_version, canonical_json)
    if config.config_hash != str(run_row["config_hash"]):
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            "the fork does not reproduce its recorded configuration hash",
        )
    return config


def score_cents(conn: sqlite3.Connection, config: RunConfig) -> int:
    row = conn.execute(LEDGER_TOTAL_SQL).fetchone()
    return int(row["total"]) - config.get_int("finance.starting_cash")


def new_job_call_id(opened: OpenInvocation) -> int | None:
    if opened.kind is not InvocationType.CALL:
        return None
    if NEW_JOB_PAYLOAD_KEY not in opened.payload:
        return None
    return int(opened.payload["call_id"])


def decidable_call_id(conn: sqlite3.Connection, opened: OpenInvocation) -> int | None:
    call_id = new_job_call_id(opened)
    if call_id is None:
        return None
    job_type = oracle.true_job_type(conn, call_id)
    if job_type is None:
        return None
    qualified = any(
        oracle.arrival_failure_reason(conn, technician.technician_id, job_type) is None
        for technician in oracle.active_technicians(conn)
    )
    return call_id if qualified else None


def play_branch(
    fork_path: Path,
    *,
    geo_dir: str | Path | None,
    horizon_days: int,
    call_id: int,
    rank: int,
    budget: ForkBudget,
    search_depth: int,
    baseline: str = oracle.BASELINE_GREEDY,
) -> BranchOutcome:
    conn = run_init.open_run_db(fork_path, artifact_dir=geo_dir)
    read_conn = oracle.open_read_connection(fork_path)
    try:
        config = read_run_config(conn)
        dispatcher = BranchDispatcher(
            read_conn,
            horizon_days=horizon_days,
            branch=f"{BRANCH_PREFIX}{rank}",
            override=(call_id, rank),
            baseline=baseline,
        )
        controller = Controller(conn, config, dispatcher, artifact_dir=geo_dir)
        dispatcher.controller = controller
        controller.step()
        run_status = play_remaining_search(
            controller,
            dispatcher,
            fork_path,
            geo_dir=geo_dir,
            horizon_days=horizon_days,
            budget=budget,
            remaining_search_decisions=search_depth - 1,
            baseline=baseline,
        )
        return BranchOutcome(
            rank=rank,
            run_status=run_status,
            score_cents=score_cents(conn, config),
            reachable=dispatcher.override_reachable,
        )
    finally:
        read_conn.close()
        conn.close()


def _rank_priority(rank: int) -> int:
    return rank if rank >= 0 else 2**63


def best_outcome(outcomes: Sequence[BranchOutcome]) -> BranchOutcome:
    return max(
        outcomes,
        key=lambda outcome: (outcome.score_cents, -_rank_priority(outcome.rank)),
    )


def explore(
    db_path: Path,
    *,
    geo_dir: str | Path | None,
    horizon_days: int,
    call_id: int,
    service_date: str,
    budget: ForkBudget,
    search_depth: int | None = None,
    baseline: str = oracle.BASELINE_GREEDY,
) -> SearchDecision:
    outcomes: list[BranchOutcome] = []
    depth = budget.lookahead_depth if search_depth is None else search_depth
    with tempfile.TemporaryDirectory(
        prefix=FORK_DIR_PREFIX, dir=db_path.parent
    ) as fork_dir:
        for rank in range(budget.forks_per_call):
            fork_path = Path(fork_dir) / f"{BRANCH_PREFIX}{rank}{FORK_DB_SUFFIX}"
            shutil.copyfile(db_path, fork_path)
            outcome = play_branch(
                fork_path,
                geo_dir=geo_dir,
                horizon_days=horizon_days,
                call_id=call_id,
                rank=rank,
                budget=budget,
                search_depth=depth,
                baseline=baseline,
            )
            fork_path.unlink()
            outcomes.append(outcome)
            if not outcome.reachable:
                break
        if budget.include_reject:
            fork_path = Path(fork_dir) / f"{BRANCH_PREFIX}{REJECT_RANK}{FORK_DB_SUFFIX}"
            shutil.copyfile(db_path, fork_path)
            outcomes.append(
                play_branch(
                    fork_path,
                    geo_dir=geo_dir,
                    horizon_days=horizon_days,
                    call_id=call_id,
                    rank=REJECT_RANK,
                    budget=budget,
                    search_depth=depth,
                    baseline=baseline,
                )
            )
            fork_path.unlink()
    chosen = best_outcome(outcomes)
    return SearchDecision(
        call_id=call_id,
        service_date=service_date,
        chosen_rank=chosen.rank,
        outcomes=tuple(outcomes),
    )


def play_remaining_search(
    controller: Controller,
    dispatcher: BranchDispatcher,
    db_path: Path,
    *,
    geo_dir: str | Path | None,
    horizon_days: int,
    budget: ForkBudget,
    remaining_search_decisions: int,
    baseline: str,
) -> str:
    while True:
        opened = controller.advance_to_invocation()
        if opened is None:
            return controller.status().value
        call_id = decidable_call_id(dispatcher.read_conn, opened)
        if call_id is not None and remaining_search_decisions > 0:
            decision = explore(
                db_path,
                geo_dir=geo_dir,
                horizon_days=horizon_days,
                call_id=call_id,
                service_date=clock.service_date_for_ms(opened.ts_ms),
                budget=budget,
                search_depth=remaining_search_decisions,
                baseline=baseline,
            )
            dispatcher.override = (decision.call_id, decision.chosen_rank)
            dispatcher.override_reachable = False
            remaining_search_decisions -= 1
        controller.step()


def drive_fork_search(
    db_path: str | Path,
    *,
    config: RunConfig,
    geo_dir: str | Path | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    budget: ForkBudget = ForkBudget(),
    baseline: str = oracle.BASELINE_GREEDY,
) -> tuple[str, tuple[SearchDecision, ...]]:
    path = Path(db_path)
    conn = run_init.open_run_db(path, artifact_dir=geo_dir)
    read_conn = oracle.open_read_connection(path)
    ledger = BudgetLedger(budget=budget)
    decisions: list[SearchDecision] = []
    try:
        dispatcher = BranchDispatcher(
            read_conn, horizon_days=horizon_days, baseline=baseline
        )
        controller = Controller(conn, config, dispatcher, artifact_dir=geo_dir)
        dispatcher.controller = controller
        while True:
            opened = controller.advance_to_invocation()
            if opened is None:
                break
            call_id = decidable_call_id(read_conn, opened)
            service_date = clock.service_date_for_ms(opened.ts_ms)
            if call_id is not None and ledger.affordable(service_date):
                decision = explore(
                    path,
                    geo_dir=geo_dir,
                    horizon_days=horizon_days,
                    call_id=call_id,
                    service_date=service_date,
                    budget=budget,
                    baseline=baseline,
                )
                dispatcher.override = (decision.call_id, decision.chosen_rank)
                dispatcher.override_reachable = False
                decisions.append(decision)
                ledger.charge(service_date)
            controller.step()
        return controller.status().value, tuple(decisions)
    finally:
        read_conn.close()
        conn.close()


def play_baseline(
    baseline: str,
    *,
    config: RunConfig,
    master_seed: int,
    run_dir: Path,
    geo_dir: str | Path | None,
    horizon_days: int,
    dataset_version: str,
) -> BaselineOutcome:
    paths = PolicyRunPaths.under(run_dir)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    run_init.initialize_run(
        paths.db_path,
        master_seed=master_seed,
        config=config,
        dataset_version=dataset_version,
        artifact_dir=geo_dir,
        harness_name=oracle.POLICY_NAME,
        harness_version=oracle.POLICY_VERSION,
        track=oracle.TRACK,
    )
    oracle.drive_oracle(
        paths.db_path,
        config=config,
        geo_dir=geo_dir,
        horizon_days=horizon_days,
        baseline=baseline,
    )
    conn = run_init.open_run_db(paths.db_path, artifact_dir=geo_dir)
    try:
        return BaselineOutcome(baseline=baseline, score_cents=score_cents(conn, config))
    finally:
        conn.close()


def best_baseline(outcomes: Sequence[BaselineOutcome]) -> BaselineOutcome:
    return max(
        outcomes,
        key=lambda outcome: (
            outcome.score_cents,
            -oracle.BASELINE_NAMES.index(outcome.baseline),
        ),
    )


def choose_baseline(
    *,
    config: RunConfig,
    master_seed: int,
    run_dir: Path,
    geo_dir: str | Path | None,
    horizon_days: int,
    dataset_version: str,
) -> tuple[BaselineOutcome, ...]:
    scratch = run_dir / BASELINE_SCRATCH_DIR
    try:
        return tuple(
            play_baseline(
                baseline,
                config=config,
                master_seed=master_seed,
                run_dir=scratch / baseline,
                geo_dir=geo_dir,
                horizon_days=horizon_days,
                dataset_version=dataset_version,
            )
            for baseline in oracle.BASELINE_NAMES
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_fork_search(
    *,
    config: RunConfig,
    master_seed: int,
    run_dir: str | Path,
    geo_dir: str | Path | None = None,
    scenario: str = DEFAULT_SCENARIO,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    dataset_version: str = "",
    budget: ForkBudget = ForkBudget(),
    policy_name: str = POLICY_NAME,
) -> ForkSearchResult:
    paths = PolicyRunPaths.under(run_dir)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    baseline_outcomes = choose_baseline(
        config=config,
        master_seed=master_seed,
        run_dir=paths.run_dir,
        geo_dir=geo_dir,
        horizon_days=horizon_days,
        dataset_version=dataset_version,
    )
    baseline = best_baseline(baseline_outcomes).baseline
    run_id = run_init.initialize_run(
        paths.db_path,
        master_seed=master_seed,
        config=config,
        dataset_version=dataset_version,
        artifact_dir=geo_dir,
        harness_name=oracle.POLICY_NAME,
        harness_version=oracle.POLICY_VERSION,
        track=oracle.TRACK,
    )
    run_status, decisions = drive_fork_search(
        paths.db_path,
        config=config,
        geo_dir=geo_dir,
        horizon_days=horizon_days,
        budget=budget,
        baseline=baseline,
    )
    report = build_report(
        paths.db_path,
        policy_name=policy_name,
        config=config,
        scenario=scenario,
        geo_dir=geo_dir,
    )
    report_path = paths.run_dir / REPORT_FILENAME
    report_path.write_bytes(report_bytes(report))
    return ForkSearchResult(
        run=PolicyRunResult(
            run_id=run_id,
            run_status=run_status,
            paths=paths,
            report=report,
            report_path=report_path,
        ),
        decisions=tuple(decisions),
        baseline=baseline,
        baseline_outcomes=baseline_outcomes,
    )


__all__ = [
    "BASELINE_SCRATCH_DIR",
    "BRANCH_PREFIX",
    "FORKED_DECISIONS_PER_DAY",
    "FORKED_DECISION_LIMIT",
    "FORKS_PER_CALL",
    "INCLUDE_REJECT",
    "LEDGER_TOTAL_SQL",
    "LOOKAHEAD_DEPTH",
    "POLICY_NAME",
    "REJECT_RANK",
    "RUN_CONFIG_SQL",
    "BaselineOutcome",
    "BranchDispatcher",
    "BranchOutcome",
    "BudgetLedger",
    "ForkBudget",
    "ForkSearchResult",
    "SearchDecision",
    "best_baseline",
    "best_outcome",
    "choose_baseline",
    "decidable_call_id",
    "drive_fork_search",
    "explore",
    "new_job_call_id",
    "play_baseline",
    "play_branch",
    "play_remaining_search",
    "read_run_config",
    "run_fork_search",
    "score_cents",
]
