from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    register_conn,
)
from policies import fork_search, oracle
from policies.accept_all import AcceptAllPolicy
from policies.runner import (
    PolicyRunPaths,
    PolicyRunResult,
    report_bytes,
    run_policy,
)
from trades_bench import booking, clock, run_init
from trades_bench._dump import CORE_TABLES, dump
from trades_bench.appointments import BookingPlan
from trades_bench.config import RunConfig, load_run_config
from trades_bench.controller import Controller
from trades_bench.run_init import open_run_db

SEED = 5

TWO_WEEKS = ("2026-10-01", "2026-10-14")
THREE_DAYS = ("2026-10-01", "2026-10-03")

BOOKING_HORIZON_DAYS = 14

CONTROLLED_TRACK = "controlled"

CONTROLLED_SEARCH_BUDGET = fork_search.ForkBudget(
    forks_per_call=2,
    decisions_per_day=1,
    decision_limit=8,
    lookahead_depth=1,
    include_reject=False,
)
NEUTRAL_BUDGET = fork_search.ForkBudget(
    forks_per_call=1,
    lookahead_depth=1,
    include_reject=False,
)

ORACLE_MODULE_NAMES = ("oracle", "fork_search")

ORACLE_SQL_LITERAL_TABLES = frozenset(
    {
        "_acceptance_parameters",
        "_call_hidden",
        "_calls",
        "_customers",
        "_job_types",
        "_service_catalog",
        "_technician_skills",
        "_technicians",
    }
)
FORK_SEARCH_SQL_LITERAL_TABLES = frozenset({"_ledger_entries", "_run"})
SQL_LITERAL_TABLES_BY_MODULE = {
    "oracle": ORACLE_SQL_LITERAL_TABLES,
    "fork_search": FORK_SEARCH_SQL_LITERAL_TABLES,
}

RUNTIME_READ_TABLES = frozenset(
    {
        "_acceptance_parameters",
        "_block_locks",
        "_call_hidden",
        "_calls",
        "_customers",
        "_job_types",
        "_on_call_rotations",
        "_service_catalog",
        "_technician_block_holds",
        "_technician_shifts",
        "_technician_skills",
        "_technicians",
    }
)

FORBIDDEN_TABLES = (
    "_rng_state",
    "_random_audit",
    "_cancellation_schedule",
    "_pending_calls",
    "_events",
    "_zone_weather_daily",
    "_address_catalog",
)
FORBIDDEN_RUNTIME_READ_TABLES = frozenset(
    {
        "_rng_state",
        "_random_audit",
        "_cancellation_schedule",
        "_pending_calls",
        "_events",
        "_zone_weather_daily",
    }
)
FORBIDDEN_RUN_WINDOW_COLUMNS = ("first_service_date", "last_service_date")

TABLE_REFERENCE_PATTERN = re.compile(r'(?:FROM|JOIN)["\s]+(_\w+)')

INTERNAL_READ_PREFIX = "sqlite_"

ORACLE_FIXTURE_ECONOMICS = {
    "demand.base_new_caller_rate_per_day": 2,
    "finance.daily_operating_overhead": 220_000,
    "finance.starting_cash": 4_200_000,
    "payroll.off_hours_bonus_per_job": 125_000,
}

REPORT_KEYS: tuple[str, ...] = (
    "report_version",
    "policy",
    "master_seed",
    "scenario",
    "spec_version",
    "config_version",
    "config_hash",
    "first_service_date",
    "last_service_date",
    "run_status",
    "starting_cash_cents",
    "final_cash_cents",
    "score_cents",
    "bankruptcy_date",
    "dump_hash",
    "calls",
    "bookings_by_urgency",
    "appointments",
    "utilization",
    "ledger_by_category",
    "daily_cash_cents",
)

BRANCH_ONLY_DUMP_TABLE = "_idempotency"

PINNED_SHORT_GREEDY_SCORE_CENTS = -943_552
PINNED_SHORT_SEARCHED_SCORE_CENTS = -943_552
PINNED_REJECTED_CALL_ID = 1
PINNED_REJECTED_RANK = 1
PINNED_REJECTED_BRANCH_SCORE_CENTS = -943_552

PINNED_TWO_WEEK_GREEDY_SCORE_CENTS = -4_425_247
PINNED_TWO_WEEK_DATE_MAJOR_SCORE_CENTS = -4_425_247

ADOPTING_SEED = 28
PINNED_ADOPTING_GREEDY_SCORE_CENTS = -762_306
PINNED_ADOPTING_DATE_MAJOR_SCORE_CENTS = -762_306
PINNED_ADOPTING_SEARCHED_SCORE_CENTS = -480_155
PINNED_ADOPTED_PROMOTIONS = {1: 1}

MILLISECONDS_PER_DAY = 86_400_000
WEEKDAY_SERVICE_DATE = "2026-10-01"
WEEKEND_SERVICE_DATE = "2026-10-03"
PAST_END_WEEKDAY_SERVICE_DATE = "2026-10-05"
PAST_END_WEEKEND_SERVICE_DATE = "2026-10-04"
DAY_BLOCK_CODE = "B09_11"
OFF_HOURS_BLOCK_CODE = "B17_19"

RUN_ROW_SQL = "SELECT track, status, last_service_date FROM _run"
RUN_ID_SQL = "SELECT run_id FROM _run"
APPOINTMENT_ROWS_SQL = (
    "SELECT a.id AS id, a.status AS status, MIN(ab.block_start) AS first_block_start "
    "FROM _appointments a JOIN _appointment_blocks ab ON ab.appointment_id = a.id "
    "GROUP BY a.id ORDER BY a.id"
)


def oracle_config(dates: tuple[str, str]) -> RunConfig:
    keys = {
        **ORACLE_FIXTURE_ECONOMICS,
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.start_date": dates[0],
        "run.end_date": dates[1],
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def drive_oracle_run(config: RunConfig, run_dir: Path, **kwargs) -> PolicyRunResult:
    return oracle.run_oracle(
        config=config,
        master_seed=SEED,
        run_dir=run_dir,
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=BOOKING_HORIZON_DAYS,
        **kwargs,
    )


def drive_fork_search_run(
    config: RunConfig, run_dir: Path, **kwargs
) -> fork_search.ForkSearchResult:
    kwargs.setdefault("budget", CONTROLLED_SEARCH_BUDGET)
    return fork_search.run_fork_search(
        config=config,
        master_seed=SEED,
        run_dir=run_dir,
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=BOOKING_HORIZON_DAYS,
        **kwargs,
    )


def drive_accept_all(config: RunConfig, run_dir: Path, horizon_days: int) -> PolicyRunResult:
    return run_policy(
        AcceptAllPolicy(),
        config=config,
        master_seed=SEED,
        run_dir=run_dir,
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=horizon_days,
        sleep=lambda seconds: None,
    )


def drive_branch_run(config: RunConfig, run_dir: Path) -> Path:
    paths = PolicyRunPaths.under(run_dir)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    run_init.initialize_run(
        paths.db_path,
        master_seed=SEED,
        config=config,
        dataset_version="",
        artifact_dir=GEO_ARTIFACT_DIR,
        harness_name=oracle.POLICY_NAME,
        harness_version=oracle.POLICY_VERSION,
        track=oracle.TRACK,
    )
    conn = run_init.open_run_db(paths.db_path, artifact_dir=GEO_ARTIFACT_DIR)
    read_conn = oracle.open_read_connection(paths.db_path)
    try:
        dispatcher = fork_search.BranchDispatcher(
            read_conn,
            horizon_days=BOOKING_HORIZON_DAYS,
            branch=f"{fork_search.BRANCH_PREFIX}0",
        )
        controller = Controller(conn, config, dispatcher, artifact_dir=GEO_ARTIFACT_DIR)
        dispatcher.controller = controller
        controller.run()
    finally:
        read_conn.close()
        conn.close()
    return paths.db_path


def open_world(db_path: Path):
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    return conn


def logical_dump(db_path: Path) -> dict[str, object]:
    conn = open_world(db_path)
    run_id = str(conn.execute(RUN_ID_SQL).fetchone()["run_id"])
    dumped = dump(conn, run_id)
    conn.close()
    return dict(dumped["tables"])


def run_row(db_path: Path) -> tuple[str | None, str, str]:
    conn = open_world(db_path)
    row = conn.execute(RUN_ROW_SQL).fetchone()
    conn.close()
    track = row["track"]
    return (
        None if track is None else str(track),
        str(row["status"]),
        str(row["last_service_date"]),
    )


def appointment_rows(db_path: Path) -> list[tuple[int, str, str]]:
    conn = open_world(db_path)
    rows = [
        (
            int(row["id"]),
            str(row["status"]),
            clock.service_date_for_ms(int(row["first_block_start"])),
        )
        for row in conn.execute(APPOINTMENT_ROWS_SQL)
    ]
    conn.close()
    return rows


def module_path(name: str) -> Path:
    return Path(oracle.__file__).parent / f"{name}.py"


def module_source(name: str) -> str:
    return module_path(name).read_text(encoding="utf-8")


def referenced_tables(source: str) -> set[str]:
    return set(TABLE_REFERENCE_PATTERN.findall(source))


def chosen_ranks(result: fork_search.ForkSearchResult) -> list[tuple[int, int]]:
    return [(decision.call_id, decision.chosen_rank) for decision in result.decisions]


def sample_pricing(
    *,
    expected_margin_cents: Decimal = Decimal(100_000),
    acceptance_base: Decimal = Decimal(1),
    acceptance_decay: Decimal = Decimal(0),
    acceptance_floor: Decimal = Decimal(0),
    cancellation_base: Decimal = Decimal(0),
    cancellation_urgent_multiplier: Decimal = Decimal("0.5"),
    cancellation_lead_days_coefficient: Decimal = Decimal("0.10"),
    off_hours_bonus_cents: int = 5_000,
) -> oracle.Pricing:
    return oracle.Pricing(
        expected_margin_cents=expected_margin_cents,
        acceptance_base=acceptance_base,
        acceptance_decay=acceptance_decay,
        acceptance_floor=acceptance_floor,
        cancellation_base=cancellation_base,
        cancellation_urgent_multiplier=cancellation_urgent_multiplier,
        cancellation_lead_days_coefficient=cancellation_lead_days_coefficient,
        off_hours_bonus_cents=off_hours_bonus_cents,
    )


def sample_plan(
    *,
    service_date: str = WEEKDAY_SERVICE_DATE,
    block_code: str = DAY_BLOCK_CODE,
    scheduled_blocks: int = 1,
    lead_days: int = 2,
) -> BookingPlan:
    start_ms = lead_days * MILLISECONDS_PER_DAY
    return BookingPlan(
        job_type_id=1,
        job_type_code="hvac_service",
        business_unit_id=1,
        scheduled_blocks=scheduled_blocks,
        technician_id=1,
        service_date=service_date,
        block_codes=(block_code,) * scheduled_blocks,
        windows=((start_ms, start_ms + 7_200_000),) * scheduled_blocks,
    )


def sample_score(
    *, plan: BookingPlan, pricing: oracle.Pricing, urgent: bool = False, wage: int = 1_000
) -> int:
    return oracle.score_cents(
        plan,
        pricing,
        urgent=urgent,
        arrived_at_ms=0,
        hourly_wage_cents=wage,
        failure_reason=None,
    )


class ReadRecorder:

    def __init__(self) -> None:
        self.tables: set[str] = set()

    def __call__(self, action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table is not None:
            if not str(table).startswith(INTERNAL_READ_PREFIX):
                self.tables.add(str(table))
        return sqlite3.SQLITE_OK


@pytest.fixture(scope="module")
def two_week_config() -> RunConfig:
    return oracle_config(TWO_WEEKS)


@pytest.fixture(scope="module")
def short_config() -> RunConfig:
    return oracle_config(THREE_DAYS)


@pytest.fixture(scope="module")
def short_oracle_twins(tmp_path_factory, short_config) -> tuple[PolicyRunResult, ...]:
    base = tmp_path_factory.mktemp("oracle-short")
    return tuple(
        drive_oracle_run(short_config, base / label) for label in ("a", "b")
    )


@pytest.fixture(scope="module")
def short_search_twins(
    tmp_path_factory, short_config
) -> tuple[fork_search.ForkSearchResult, ...]:
    base = tmp_path_factory.mktemp("oracle-search")
    return tuple(
        drive_fork_search_run(short_config, base / label) for label in ("a", "b")
    )


@pytest.fixture(scope="module")
def neutral_search(tmp_path_factory, short_config) -> fork_search.ForkSearchResult:
    base = tmp_path_factory.mktemp("oracle-search-neutral")
    return drive_fork_search_run(
        short_config,
        base / "neutral",
        budget=NEUTRAL_BUDGET,
        policy_name=oracle.POLICY_NAME,
    )


@pytest.fixture(scope="module")
def expanded_search(tmp_path_factory, short_config) -> fork_search.ForkSearchResult:
    base = tmp_path_factory.mktemp("oracle-search-expanded")
    return drive_fork_search_run(
        short_config,
        base / "expanded",
        budget=fork_search.ForkBudget(
            forks_per_call=1,
            decisions_per_day=1,
            decision_limit=1,
            lookahead_depth=2,
            include_reject=True,
        ),
    )


@pytest.fixture(scope="module")
def short_branch_db(tmp_path_factory, short_config) -> Path:
    base = tmp_path_factory.mktemp("oracle-branch")
    return drive_branch_run(short_config, base / "branch")


@pytest.fixture(scope="module")
def two_week_runs(tmp_path_factory, two_week_config) -> tuple[PolicyRunResult, PolicyRunResult]:
    base = tmp_path_factory.mktemp("oracle-two-week")
    clairvoyant = drive_oracle_run(two_week_config, base / "oracle")
    blind = drive_accept_all(two_week_config, base / "accept_all", BOOKING_HORIZON_DAYS)
    return clairvoyant, blind


@pytest.fixture(scope="module")
def two_week_search(tmp_path_factory, two_week_config) -> fork_search.ForkSearchResult:
    base = tmp_path_factory.mktemp("oracle-two-week-search")
    return drive_fork_search_run(two_week_config, base / "search")


@pytest.fixture(scope="module")
def adopting_search(tmp_path_factory, short_config) -> fork_search.ForkSearchResult:
    base = tmp_path_factory.mktemp("oracle-adopting-search")
    return fork_search.run_fork_search(
        config=short_config,
        master_seed=ADOPTING_SEED,
        run_dir=base / "search",
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=BOOKING_HORIZON_DAYS,
        budget=CONTROLLED_SEARCH_BUDGET,
    )


@pytest.fixture(scope="module")
def adopting_baseline(tmp_path_factory, short_config) -> PolicyRunResult:
    base = tmp_path_factory.mktemp("oracle-adopting-baseline")
    return oracle.run_oracle(
        config=short_config,
        master_seed=ADOPTING_SEED,
        run_dir=base / "greedy",
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=BOOKING_HORIZON_DAYS,
        baseline=oracle.BASELINE_GREEDY,
    )


def baseline_scores(result: fork_search.ForkSearchResult) -> dict[str, int]:
    return {outcome.baseline: outcome.score_cents for outcome in result.baseline_outcomes}


def test_the_default_search_budget_uses_nested_reject_aware_search():
    budget = fork_search.ForkBudget()

    assert budget.lookahead_depth == 2
    assert budget.include_reject is True


def test_the_oracle_drives_a_short_window_run_to_completed_with_a_deterministic_dump(
    short_oracle_twins,
):
    first, second = short_oracle_twins

    assert first.paths.db_path != second.paths.db_path
    assert first.run_status == "completed"
    assert second.run_status == "completed"
    assert first.report["dump_hash"] == second.report["dump_hash"]
    assert first.report_bytes == second.report_bytes
    assert first.report["calls"]["new_job"]["booked"] >= 1


def test_twin_fork_searches_on_one_seed_choose_the_same_branches_and_the_same_dump(
    short_search_twins,
):
    first, second = short_search_twins

    assert first.run.paths.db_path != second.run.paths.db_path
    assert first.run.run_status == "completed"
    assert second.run.run_status == "completed"
    assert first.decisions
    assert chosen_ranks(first) == chosen_ranks(second)
    assert first.decisions == second.decisions
    assert first.run.report["dump_hash"] == second.run.report["dump_hash"]
    assert first.run.report_bytes == second.run.report_bytes


def test_every_forked_branch_plays_to_a_completed_status_and_is_scored_against_the_others(
    short_search_twins,
):
    result = short_search_twins[0]

    for decision in result.decisions:
        assert decision.outcomes
        assert [outcome.rank for outcome in decision.outcomes] == list(
            range(len(decision.outcomes))
        )
        for outcome in decision.outcomes:
            assert outcome.run_status == "completed"
        best = max(outcome.score_cents for outcome in decision.outcomes)
        chosen = next(
            outcome
            for outcome in decision.outcomes
            if outcome.rank == decision.chosen_rank
        )
        assert chosen.score_cents == best


def test_a_fork_search_whose_branches_all_reproduce_the_greedy_action_matches_it_exactly(
    neutral_search, short_oracle_twins
):
    straight = short_oracle_twins[0]

    assert neutral_search.decisions
    assert {decision.chosen_rank for decision in neutral_search.decisions} == {0}
    assert neutral_search.run.run_status == straight.run_status
    assert neutral_search.run.report["dump_hash"] == straight.report["dump_hash"]
    assert neutral_search.run.report["score_cents"] == straight.report["score_cents"]
    assert neutral_search.run.report_bytes == straight.report_bytes


def test_expanded_search_evaluates_rejection_and_nested_decisions(expanded_search):
    assert expanded_search.run.run_status == "completed"
    assert len(expanded_search.decisions) == 1
    assert [outcome.rank for outcome in expanded_search.decisions[0].outcomes] == [
        0,
        fork_search.REJECT_RANK,
    ]
    assert all(outcome.reachable for outcome in expanded_search.decisions[0].outcomes)


def test_a_played_branch_reproduces_the_straight_run_on_every_table_but_idempotency(
    short_branch_db, short_oracle_twins
):
    branch = logical_dump(short_branch_db)
    straight = logical_dump(short_oracle_twins[0].paths.db_path)

    assert set(branch) == set(straight)
    assert branch[BRANCH_ONLY_DUMP_TABLE] != straight[BRANCH_ONLY_DUMP_TABLE]
    for table in CORE_TABLES:
        if table.name == BRANCH_ONLY_DUMP_TABLE:
            continue
        assert branch[table.name] == straight[table.name], table.name


def test_the_fork_search_leaves_no_fork_copy_behind_in_its_own_run_directory(
    short_search_twins,
):
    result = short_search_twins[0]
    run_dir = result.run.paths.run_dir
    strays = [
        str(path.relative_to(run_dir))
        for path in run_dir.rglob("*")
        if path.name.endswith(fork_search.FORK_DB_SUFFIX)
        or path.name.startswith(fork_search.FORK_DIR_PREFIX)
    ]

    assert result.decisions
    assert run_dir.is_dir()
    assert strays == []


def test_the_fork_search_never_scores_worse_than_the_greedy_dispatcher_it_searches_over(
    short_search_twins, short_oracle_twins
):
    searched = short_search_twins[0].run.report["score_cents"]
    greedy = short_oracle_twins[0].report["score_cents"]

    assert searched >= greedy


def test_the_fork_search_keeps_the_greedy_action_where_no_reachable_promotion_pays(
    short_search_twins, short_oracle_twins
):
    result = short_search_twins[0]
    searched = result.run
    greedy = short_oracle_twins[0]
    promotions = {
        decision.call_id: decision.chosen_rank
        for decision in result.decisions
        if decision.chosen_rank > 0
    }
    rejected = next(
        outcome
        for decision in result.decisions
        if decision.call_id == PINNED_REJECTED_CALL_ID
        for outcome in decision.outcomes
        if outcome.rank == PINNED_REJECTED_RANK
    )

    assert promotions == {}
    assert rejected.reachable is True
    assert rejected.score_cents == PINNED_REJECTED_BRANCH_SCORE_CENTS
    assert rejected.score_cents <= PINNED_SHORT_GREEDY_SCORE_CENTS
    assert greedy.report["score_cents"] == PINNED_SHORT_GREEDY_SCORE_CENTS
    assert searched.report["score_cents"] == PINNED_SHORT_SEARCHED_SCORE_CENTS
    assert searched.report["score_cents"] >= greedy.report["score_cents"]
    assert searched.report["dump_hash"] == greedy.report["dump_hash"]


def test_the_fork_search_adopts_promotions_that_beat_the_winning_baseline_world(
    adopting_search, adopting_baseline
):
    searched = adopting_search.run
    scores = baseline_scores(adopting_search)
    promotions = {
        decision.call_id: decision.chosen_rank
        for decision in adopting_search.decisions
        if decision.chosen_rank > 0
    }

    assert searched.run_status == "completed"
    assert adopting_baseline.run_status == "completed"
    assert adopting_search.baseline == oracle.BASELINE_GREEDY
    assert scores[oracle.BASELINE_GREEDY] == PINNED_ADOPTING_GREEDY_SCORE_CENTS
    assert scores[oracle.BASELINE_DATE_MAJOR] == PINNED_ADOPTING_DATE_MAJOR_SCORE_CENTS
    assert adopting_baseline.report["score_cents"] == PINNED_ADOPTING_GREEDY_SCORE_CENTS
    assert promotions == PINNED_ADOPTED_PROMOTIONS
    assert searched.report["score_cents"] == PINNED_ADOPTING_SEARCHED_SCORE_CENTS
    assert searched.report["score_cents"] > PINNED_ADOPTING_GREEDY_SCORE_CENTS
    assert searched.report["score_cents"] > scores[oracle.BASELINE_DATE_MAJOR]
    assert searched.report["dump_hash"] != adopting_baseline.report["dump_hash"]


def test_every_adopted_promotion_is_the_best_branch_of_its_decision(adopting_search):
    adopted = [
        decision for decision in adopting_search.decisions if decision.chosen_rank > 0
    ]

    assert adopted
    for decision in adopted:
        chosen = next(
            outcome
            for outcome in decision.outcomes
            if outcome.rank == decision.chosen_rank
        )
        greedy_branch = next(
            outcome for outcome in decision.outcomes if outcome.rank == 0
        )
        assert chosen.reachable is True
        assert chosen.run_status == "completed"
        assert chosen.score_cents > greedy_branch.score_cents


def test_the_branch_comparator_breaks_an_exact_score_tie_toward_the_lower_rank():
    tied = (
        fork_search.BranchOutcome(
            rank=0, run_status="completed", score_cents=1_000, reachable=True
        ),
        fork_search.BranchOutcome(
            rank=1, run_status="completed", score_cents=1_000, reachable=True
        ),
    )
    richer_high_rank = (
        tied[0],
        fork_search.BranchOutcome(
            rank=1, run_status="completed", score_cents=1_001, reachable=True
        ),
    )
    tied_reject = (
        tied[0],
        fork_search.BranchOutcome(
            rank=fork_search.REJECT_RANK,
            run_status="completed",
            score_cents=1_000,
            reachable=True,
        ),
    )

    assert fork_search.best_outcome(tied).rank == 0
    assert fork_search.best_outcome(tuple(reversed(tied))).rank == 0
    assert fork_search.best_outcome(richer_high_rank).rank == 1
    assert fork_search.best_outcome(tied_reject).rank == 0


def test_a_branch_dispatcher_refuses_to_call_a_non_positive_promotion_reachable():
    dispatcher = fork_search.BranchDispatcher(None, override=(7, 1))
    ranked = [
        oracle.Candidate(
            service_date=WEEKDAY_SERVICE_DATE,
            block_start="09:00",
            technician_id=1,
            score_cents=100,
        ),
        oracle.Candidate(
            service_date=WEEKDAY_SERVICE_DATE,
            block_start="11:00",
            technician_id=1,
            score_cents=0,
        ),
    ]

    assert dispatcher.rank(7, ranked) == ranked
    assert dispatcher.override_reachable is False


def test_a_branch_dispatcher_promotes_a_profitable_lower_ranked_candidate_and_reports_it():
    dispatcher = fork_search.BranchDispatcher(None, override=(7, 1))
    ranked = [
        oracle.Candidate(
            service_date=WEEKDAY_SERVICE_DATE,
            block_start="09:00",
            technician_id=1,
            score_cents=100,
        ),
        oracle.Candidate(
            service_date=WEEKDAY_SERVICE_DATE,
            block_start="11:00",
            technician_id=1,
            score_cents=40,
        ),
    ]

    promoted = dispatcher.rank(7, ranked)

    assert [candidate.score_cents for candidate in promoted] == [40, 100]
    assert dispatcher.override_reachable is True


def test_the_oracle_and_its_branches_touch_exactly_the_pinned_runtime_table_allowlist(
    monkeypatch, tmp_path, short_config
):
    recorder = ReadRecorder()
    plain_opener = oracle.open_read_connection

    def recording_opener(db_path):
        conn = plain_opener(db_path)
        conn.set_authorizer(recorder)
        return conn

    monkeypatch.setattr(oracle, "open_read_connection", recording_opener)
    drive_oracle_run(short_config, tmp_path / "greedy")
    drive_fork_search_run(short_config, tmp_path / "searched")

    assert recorder.tables == set(RUNTIME_READ_TABLES)
    assert recorder.tables.isdisjoint(FORBIDDEN_RUNTIME_READ_TABLES)


@pytest.mark.parametrize("name", ORACLE_MODULE_NAMES)
def test_no_oracle_module_sql_literal_names_a_future_draw_or_an_unarrived_call_table(name):
    source = module_source(name)

    offenders = [table for table in FORBIDDEN_TABLES if table in source]

    assert offenders == []


@pytest.mark.parametrize("name", ORACLE_MODULE_NAMES)
def test_each_oracle_module_sql_literals_reference_only_its_pinned_table_allowlist(name):
    scanned = [module_path(scanned_name).name for scanned_name in ORACLE_MODULE_NAMES]

    assert scanned == ["oracle.py", "fork_search.py"]
    assert referenced_tables(module_source(name)) == SQL_LITERAL_TABLES_BY_MODULE[name]


def test_the_dispatcher_module_never_names_a_run_window_column():
    source = module_source("oracle")

    offenders = [column for column in FORBIDDEN_RUN_WINDOW_COLUMNS if column in source]

    assert offenders == []


def test_the_expected_margin_is_the_price_triangle_mean_net_of_the_mean_material_share(
    two_week_config,
):
    service = oracle.expected_margin_cents(two_week_config, "hvac_service", "residential")
    installation = oracle.expected_margin_cents(
        two_week_config, "hvac_installation", "commercial"
    )
    service_amounts = two_week_config.resolved["invoices.amount"]["hvac_service"]["residential"]
    service_range = two_week_config.resolved["materials.percent_range"]["hvac_service"]
    mean_service_invoice = Decimal(sum(service_amounts.values())) / Decimal(3)
    mean_service_basis_points = Decimal(sum(service_range.values())) / Decimal(2)
    installation_amounts = two_week_config.resolved["invoices.amount"]["hvac_installation"][
        "commercial"
    ]
    installation_range = two_week_config.resolved["materials.percent_range"][
        "hvac_installation"
    ]
    mean_installation_invoice = Decimal(sum(installation_amounts.values())) / Decimal(3)
    mean_installation_basis_points = Decimal(sum(installation_range.values())) / Decimal(2)

    assert service < mean_service_invoice
    assert service == mean_service_invoice * (
        Decimal(1) - mean_service_basis_points / Decimal(10_000)
    )
    assert installation == mean_installation_invoice * (
        Decimal(1) - mean_installation_basis_points / Decimal(10_000)
    )
    assert installation > service


def test_the_off_hours_cost_charges_block_payroll_plus_the_bonus_only_outside_day_shifts():
    day = oracle.off_hours_cost_cents(
        sample_plan(), hourly_wage_cents=5_000, bonus_cents=5_000
    )
    evening = oracle.off_hours_cost_cents(
        sample_plan(block_code=OFF_HOURS_BLOCK_CODE),
        hourly_wage_cents=5_000,
        bonus_cents=5_000,
    )
    weekend = oracle.off_hours_cost_cents(
        sample_plan(service_date=WEEKEND_SERVICE_DATE, scheduled_blocks=2),
        hourly_wage_cents=5_000,
        bonus_cents=5_000,
    )

    assert day == 0
    assert evening == 15_000
    assert weekend == 25_000


def test_the_cancellation_probability_scales_with_urgency_and_lead_days_inside_zero_to_one():
    pricing = sample_pricing(cancellation_base=Decimal("0.08"))
    standard = oracle.cancellation_probability(
        pricing, urgent=False, lead_days=Decimal(2)
    )
    urgent = oracle.cancellation_probability(pricing, urgent=True, lead_days=Decimal(2))
    over_one = oracle.cancellation_probability(
        sample_pricing(
            cancellation_base=Decimal(1), cancellation_lead_days_coefficient=Decimal(1)
        ),
        urgent=False,
        lead_days=Decimal(5),
    )
    under_zero = oracle.cancellation_probability(
        sample_pricing(
            cancellation_base=Decimal(1), cancellation_lead_days_coefficient=Decimal(-1)
        ),
        urgent=False,
        lead_days=Decimal(2),
    )

    assert standard == Decimal("0.096")
    assert urgent == Decimal("0.048")
    assert over_one == Decimal(1)
    assert under_zero == Decimal(0)


def test_the_score_wires_the_engine_acceptance_curve_including_its_probability_floor():
    decayed = sample_score(
        plan=sample_plan(),
        pricing=sample_pricing(acceptance_decay=Decimal("0.5")),
    )
    floored = sample_score(
        plan=sample_plan(),
        pricing=sample_pricing(
            acceptance_decay=Decimal(100), acceptance_floor=Decimal("0.25")
        ),
    )
    engine_probability = booking._acceptance_probability(
        Decimal(1), Decimal("0.5"), Decimal(2), Decimal(0)
    )

    assert decayed == 36_788
    assert floored == 25_000
    assert round(engine_probability * Decimal(100_000)) == decayed
    assert decayed < 100_000


def test_the_score_composes_acceptance_keep_margin_and_off_hours_cost_into_one_number():
    pricing = sample_pricing(
        acceptance_base=Decimal("0.5"), cancellation_base=Decimal("0.25")
    )
    evening_plan = sample_plan(block_code=OFF_HOURS_BLOCK_CODE)
    composed = sample_score(plan=evening_plan, pricing=pricing)
    urgent = sample_score(plan=evening_plan, pricing=pricing, urgent=True)
    day_shift = sample_score(plan=sample_plan(), pricing=pricing)
    unqualified = oracle.score_cents(
        evening_plan,
        pricing,
        urgent=False,
        arrived_at_ms=0,
        hourly_wage_cents=1_000,
        failure_reason="unqualified",
    )

    assert composed == 32_550
    assert urgent == 39_525
    assert day_shift == 35_000
    assert unqualified == -2_450


def test_the_end_aware_dispatcher_books_nothing_past_the_run_end_and_finalizes_clean(
    short_oracle_twins,
):
    first, second = short_oracle_twins
    _, status, last_service_date = run_row(first.paths.db_path)
    rows = appointment_rows(first.paths.db_path)
    beyond_end = [row for row in rows if row[2] > last_service_date]

    assert last_service_date == THREE_DAYS[1]
    assert status == "completed"
    assert rows
    assert beyond_end == []
    assert appointment_rows(second.paths.db_path) == rows


def test_a_candidate_dated_past_the_run_end_earns_no_margin(short_config):
    end_service_date = short_config.get_string(oracle.RUN_END_DATE_KEY)
    pricing = sample_pricing()
    inside_plan = sample_plan(service_date=WEEKEND_SERVICE_DATE, block_code=DAY_BLOCK_CODE)
    past_end_plan = sample_plan(
        service_date=PAST_END_WEEKDAY_SERVICE_DATE, block_code=DAY_BLOCK_CODE
    )
    past_end_off_hours_plan = sample_plan(
        service_date=PAST_END_WEEKEND_SERVICE_DATE, block_code=OFF_HOURS_BLOCK_CODE
    )

    def end_aware(plan):
        return oracle.score_cents(
            plan,
            pricing,
            urgent=False,
            arrived_at_ms=0,
            hourly_wage_cents=1_000,
            failure_reason=None,
            end_service_date=end_service_date,
        )

    assert end_service_date == THREE_DAYS[1]
    assert oracle.settles_inside_run(WEEKEND_SERVICE_DATE, end_service_date) is True
    assert oracle.settles_inside_run(PAST_END_WEEKDAY_SERVICE_DATE, end_service_date) is False
    assert sample_score(plan=past_end_plan, pricing=pricing) > 0
    assert end_aware(inside_plan) > 0
    assert end_aware(past_end_plan) == 0
    assert end_aware(past_end_off_hours_plan) < 0


def test_the_date_major_baseline_keeps_its_pinned_two_week_cash(two_week_search):
    scores = baseline_scores(two_week_search)

    assert scores[oracle.BASELINE_DATE_MAJOR] == PINNED_TWO_WEEK_DATE_MAJOR_SCORE_CENTS


def test_the_greedy_baseline_wins_the_tied_race_and_the_search_never_undercuts_it(
    two_week_search, two_week_runs
):
    clairvoyant, _blind = two_week_runs
    scores = baseline_scores(two_week_search)

    assert set(scores) == set(oracle.BASELINE_NAMES)
    assert two_week_search.baseline == oracle.BASELINE_GREEDY
    assert scores[oracle.BASELINE_GREEDY] == scores[oracle.BASELINE_DATE_MAJOR]
    assert scores[oracle.BASELINE_GREEDY] == clairvoyant.report["score_cents"]
    assert scores[oracle.BASELINE_GREEDY] == PINNED_TWO_WEEK_GREEDY_SCORE_CENTS
    assert two_week_search.run.report["score_cents"] >= scores[oracle.BASELINE_GREEDY]


def test_the_baseline_race_breaks_an_exact_tie_toward_the_greedy_shape():
    tied = (
        fork_search.BaselineOutcome(baseline=oracle.BASELINE_GREEDY, score_cents=1_000),
        fork_search.BaselineOutcome(baseline=oracle.BASELINE_DATE_MAJOR, score_cents=1_000),
    )
    richer_date_major = (
        tied[0],
        fork_search.BaselineOutcome(
            baseline=oracle.BASELINE_DATE_MAJOR, score_cents=1_001
        ),
    )

    assert fork_search.best_baseline(tied).baseline == oracle.BASELINE_GREEDY
    assert fork_search.best_baseline(tuple(reversed(tied))).baseline == oracle.BASELINE_GREEDY
    assert (
        fork_search.best_baseline(richer_date_major).baseline == oracle.BASELINE_DATE_MAJOR
    )


def test_the_date_major_order_keeps_only_positive_candidates_earliest_date_first():
    ranked = [
        oracle.Candidate(
            service_date="2026-10-03", block_start="09:00", technician_id=1, score_cents=900
        ),
        oracle.Candidate(
            service_date="2026-10-01", block_start="11:00", technician_id=1, score_cents=500
        ),
        oracle.Candidate(
            service_date="2026-10-01", block_start="09:00", technician_id=2, score_cents=400
        ),
        oracle.Candidate(
            service_date="2026-10-02", block_start="09:00", technician_id=1, score_cents=0
        ),
    ]

    ordered = oracle.DateMajorDispatcher(None).rank(7, ranked)

    assert [candidate.service_date for candidate in ordered] == [
        "2026-10-01",
        "2026-10-01",
        "2026-10-03",
    ]
    assert [candidate.score_cents for candidate in ordered] == [500, 400, 900]


def test_a_branch_under_the_date_major_baseline_overrides_the_date_major_order():
    ranked = [
        oracle.Candidate(
            service_date="2026-10-03", block_start="09:00", technician_id=1, score_cents=900
        ),
        oracle.Candidate(
            service_date="2026-10-01", block_start="11:00", technician_id=1, score_cents=500
        ),
        oracle.Candidate(
            service_date="2026-10-02", block_start="09:00", technician_id=1, score_cents=400
        ),
    ]
    dispatcher = fork_search.BranchDispatcher(
        None, override=(7, 1), baseline=oracle.BASELINE_DATE_MAJOR
    )

    promoted = dispatcher.rank(7, ranked)

    assert dispatcher.override_reachable is True
    assert [candidate.service_date for candidate in promoted] == [
        "2026-10-02",
        "2026-10-01",
        "2026-10-03",
    ]


def test_an_oracle_run_carries_the_oracle_track_where_a_scored_policy_run_carries_controlled(
    two_week_runs,
):
    clairvoyant, blind = two_week_runs

    assert run_row(clairvoyant.paths.db_path)[0] == oracle.TRACK
    assert run_row(blind.paths.db_path)[0] == CONTROLLED_TRACK


def test_a_fork_search_run_is_tracked_as_an_oracle_run_like_the_dispatcher_it_wraps(
    short_search_twins, short_oracle_twins
):
    searched = run_row(short_search_twins[0].run.paths.db_path)
    greedy = run_row(short_oracle_twins[0].paths.db_path)

    assert searched == greedy


def test_the_oracle_report_keeps_the_frozen_report_shape_of_a_blind_policy_run(
    two_week_runs,
):
    clairvoyant, blind = two_week_runs

    assert tuple(clairvoyant.report) == REPORT_KEYS
    assert tuple(blind.report) == REPORT_KEYS
    assert clairvoyant.report["report_version"] == blind.report["report_version"]
    assert clairvoyant.report["policy"] == oracle.POLICY_NAME
    assert clairvoyant.report["master_seed"] == SEED
    assert clairvoyant.report["first_service_date"] == TWO_WEEKS[0]
    assert clairvoyant.report["last_service_date"] == TWO_WEEKS[1]
    assert clairvoyant.report["config_hash"] == blind.report["config_hash"]
    assert clairvoyant.report_path.read_bytes() == report_bytes(clairvoyant.report)


def test_the_fork_search_report_keeps_the_frozen_report_shape_too(
    short_search_twins,
):
    searched = short_search_twins[0].run

    assert tuple(searched.report) == REPORT_KEYS
    assert searched.report["policy"] == fork_search.POLICY_NAME
    assert searched.report_path.read_bytes() == report_bytes(searched.report)


def test_pol07_the_dev_fixture_reports_each_bankruptcy_without_zeroing_cash(
    two_week_runs,
):
    clairvoyant, blind = two_week_runs

    assert clairvoyant.run_status == "bankrupt"
    assert blind.run_status == "bankrupt"
    assert clairvoyant.report["bankruptcy_date"] is not None
    assert blind.report["bankruptcy_date"] is not None
    assert clairvoyant.report["final_cash_cents"] < 0
    assert blind.report["final_cash_cents"] < 0
    assert clairvoyant.report["policy"] == oracle.POLICY_NAME
    assert run_row(clairvoyant.paths.db_path)[0] == oracle.TRACK


def test_pol07_the_dev_fixture_reconciles_raw_final_cash_for_both_policies(
    two_week_runs,
):
    clairvoyant, blind = two_week_runs

    for result in (clairvoyant, blind):
        assert result.report["final_cash_cents"] == (
            result.report["starting_cash_cents"] + result.report["score_cents"]
        )
