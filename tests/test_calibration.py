from __future__ import annotations

import json
from pathlib import Path

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
)
from policies import calibration, oracle
from policies.runner import REPORT_FILENAME, report_bytes
from trades_bench.config import RunConfig, load_run_config
from trades_bench.run_init import initialize_run, open_run_db

SEED = 5
ENVELOPE_SEEDS = (5, 101, 202)

OCT_WINDOW = ("2026-10-01", "2026-10-14")
JULY_WINDOW = ("2027-07-01", "2027-07-14")
JULY_PRECOMPUTE_END = "2027-07-21"

SHORT_OCT_WINDOW = ("2026-10-01", "2026-10-03")
SHORT_JULY_WINDOW = ("2027-07-01", "2027-07-03")
SHORT_JULY_PRECOMPUTE_END = "2027-07-10"
SHORT_HORIZON_DAYS = 3

MIN_JULY_STORM_ZONE_DAYS = 10
MAX_JULY_STORM_ZONE_DAYS = 60

CALIBRATION_REPORT_KEYS: tuple[str, ...] = (
    "primary_seed",
    "report_version",
    "scenarios",
    "seeds",
    "spec_version",
)
SCENARIO_REPORT_KEYS: tuple[str, ...] = (
    "checks",
    "config_hash",
    "config_version",
    "rows",
    "scenario",
)

STORM_ZONE_DAYS_SQL = (
    "SELECT COUNT(*) AS total FROM _zone_weather_daily "
    "WHERE storm_flag = 1 AND local_date >= ? AND local_date <= ?"
)

SCENARIO_KEY_SETS = {
    "oct_default": {},
    "july_monsoon": {
        "run.start_date": JULY_WINDOW[0],
        "run.end_date": JULY_WINDOW[1],
        "weather.precompute_end_date": JULY_PRECOMPUTE_END,
    },
}


def tempe_config(**overrides: object) -> RunConfig:
    keys: dict[str, object] = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
    }
    keys.update(overrides)
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def storm_zone_days(db_path: Path, window: tuple[str, str]) -> int:
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    try:
        return int(conn.execute(STORM_ZONE_DAYS_SQL, window).fetchone()["total"])
    finally:
        conn.close()


@pytest.fixture(scope="module")
def monsoon_storm_counts(tmp_path_factory) -> dict[int, tuple[int, int]]:
    base = tmp_path_factory.mktemp("calibration-monsoon")
    config = tempe_config(
        **{
            "run.start_date": JULY_WINDOW[0],
            "run.end_date": JULY_WINDOW[1],
            "weather.precompute_end_date": JULY_PRECOMPUTE_END,
        }
    )
    counts: dict[int, tuple[int, int]] = {}
    for seed in ENVELOPE_SEEDS:
        db_path = base / f"{seed}.db"
        initialize_run(db_path, master_seed=seed, config=config, artifact_dir=GEO_ARTIFACT_DIR)
        counts[seed] = (
            storm_zone_days(db_path, JULY_WINDOW),
            storm_zone_days(db_path, OCT_WINDOW),
        )
    return counts


@pytest.fixture(scope="module")
def short_matrix(tmp_path_factory) -> tuple[dict, Path]:
    out_dir = tmp_path_factory.mktemp("calibration-matrix")
    scenarios = (
        (
            "oct_default",
            tempe_config(
                **{"run.start_date": SHORT_OCT_WINDOW[0], "run.end_date": SHORT_OCT_WINDOW[1]}
            ),
        ),
        (
            "july_monsoon",
            tempe_config(
                **{
                    "run.start_date": SHORT_JULY_WINDOW[0],
                    "run.end_date": SHORT_JULY_WINDOW[1],
                    "weather.precompute_end_date": SHORT_JULY_PRECOMPUTE_END,
                }
            ),
        ),
    )
    report = calibration.run_matrix(
        scenarios=scenarios,
        seeds=[SEED],
        out_dir=out_dir,
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=SHORT_HORIZON_DAYS,
        sleep=lambda seconds: None,
        retain_run_databases=True,
    )
    return report, out_dir


def test_parallel_matrix_runs_worlds_in_processes(tmp_path):
    scenarios = (
        (
            "oct_default",
            tempe_config(
                **{"run.start_date": SHORT_OCT_WINDOW[0], "run.end_date": SHORT_OCT_WINDOW[1]}
            ),
        ),
        (
            "july_monsoon",
            tempe_config(
                **{
                    "run.start_date": SHORT_JULY_WINDOW[0],
                    "run.end_date": SHORT_JULY_WINDOW[1],
                    "weather.precompute_end_date": SHORT_JULY_PRECOMPUTE_END,
                }
            ),
        ),
    )

    report = calibration.run_matrix(
        scenarios=scenarios,
        seeds=[SEED],
        out_dir=tmp_path,
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=1,
        workers=2,
    )

    assert [item["scenario"] for item in report["scenarios"]] == [
        "oct_default",
        "july_monsoon",
    ]
    assert all(len(item["rows"]) == len(calibration.POLICY_NAMES) for item in report["scenarios"])


def policy_row(
    policy: str,
    final_cash_cents: int,
    *,
    run_status: str,
    bankruptcy_date: str | None,
) -> calibration.PolicyRow:
    return calibration.PolicyRow(
        scenario="test",
        master_seed=SEED,
        policy=policy,
        final_cash_cents=final_cash_cents,
        run_status=run_status,
        bankruptcy_date=bankruptcy_date,
        dump_hash="0" * 64,
        ledger_digest="1" * 64,
        ledger_by_category={"revenue": 0},
        bookings_by_urgency={"standard": {"booked": 0, "calls": 0}},
        appointments={"completed": 0},
        utilization={"assigned_blocks": 0, "shift_blocks": 1},
        daily_cash_cents=(("2026-10-01", final_cash_cents),),
    )


def passing_rows(accept_all_final_cash_cents: int = 500_000) -> dict[int, tuple]:
    return {
        SEED: (
            policy_row(
                calibration.DO_NOTHING_POLICY_NAME,
                -100,
                run_status=calibration.BANKRUPT_RUN_STATUS,
                bankruptcy_date="2026-10-08",
            ),
            policy_row(
                calibration.ACCEPT_ALL_POLICY_NAME,
                accept_all_final_cash_cents,
                run_status=calibration.COMPLETED_RUN_STATUS,
                bankruptcy_date=None,
            ),
            policy_row(
                calibration.REJECT_HEAVY_POLICY_NAME,
                -200,
                run_status=calibration.BANKRUPT_RUN_STATUS,
                bankruptcy_date="2026-10-10",
            ),
            policy_row(
                calibration.ORACLE_POLICY_NAME,
                600_000,
                run_status=calibration.COMPLETED_RUN_STATUS,
                bankruptcy_date=None,
            ),
        )
    }


def test_the_raw_cash_checks_accept_bankruptcy_or_thin_survival():
    checks = calibration.evaluate_checks(config=tempe_config(), rows_by_seed=passing_rows())

    assert tuple(check.name for check in checks) == calibration.CHECK_NAMES
    assert all(check.passed for check in checks)


def test_accept_all_may_end_bankrupt_with_its_negative_ledger_balance():
    rows = passing_rows(accept_all_final_cash_cents=-300)
    items = list(rows[SEED])
    items[1] = policy_row(
        calibration.ACCEPT_ALL_POLICY_NAME,
        -300,
        run_status=calibration.BANKRUPT_RUN_STATUS,
        bankruptcy_date="2026-10-13",
    )
    items[3] = policy_row(
        calibration.ORACLE_POLICY_NAME,
        100,
        run_status=calibration.COMPLETED_RUN_STATUS,
        bankruptcy_date=None,
    )

    checks = calibration.evaluate_checks(config=tempe_config(), rows_by_seed={SEED: tuple(items)})

    assert all(check.passed for check in checks)


def test_a_completed_accept_all_run_above_the_cash_ceiling_fails():
    config = tempe_config()
    ceiling = config.get_int(calibration.ACCEPT_ALL_MAX_FINAL_CASH_KEY)
    checks = calibration.evaluate_checks(
        config=config, rows_by_seed=passing_rows(accept_all_final_cash_cents=ceiling + 1)
    )

    by_name = {check.name: check for check in checks}
    assert by_name[calibration.ACCEPT_ALL_OUTCOME_CHECK].passed is False
    assert by_name[calibration.ACCEPT_ALL_OUTCOME_CHECK].inputs["failing_seeds"] == [SEED]


def test_the_oracle_must_complete_with_strictly_more_final_cash():
    rows = passing_rows(accept_all_final_cash_cents=600_000)

    checks = calibration.evaluate_checks(config=tempe_config(), rows_by_seed=rows)

    by_name = {check.name: check for check in checks}
    assert by_name[calibration.ORACLE_OUTCOME_CHECK].passed is False
    assert by_name[calibration.ORACLE_OUTCOME_CHECK].inputs["failing_seeds"] == [SEED]


def test_the_scenario_directory_holds_exactly_the_two_shipped_scenario_files():
    present = sorted(path.name for path in calibration.SCENARIO_DIR.iterdir())

    assert present == sorted(calibration.SCENARIO_FILENAMES)


def test_the_default_scenario_carries_no_overrides():
    name, config = calibration.scenario_config(
        calibration.SCENARIO_DIR / "oct_default.json"
    )
    defaults = load_run_config()

    assert name == "oct_default"
    assert dict(config.resolved) == dict(defaults.resolved)
    assert config.config_hash == defaults.config_hash


def test_the_monsoon_scenario_carries_exactly_the_three_window_keys():
    stored = json.loads(
        (calibration.SCENARIO_DIR / "july_monsoon.json").read_text(encoding="utf-8")
    )
    name, config = calibration.scenario_config(
        calibration.SCENARIO_DIR / "july_monsoon.json"
    )

    assert name == "july_monsoon"
    assert set(stored["keys"]) == set(SCENARIO_KEY_SETS["july_monsoon"])
    assert config.get_string("run.start_date") == JULY_WINDOW[0]
    assert config.get_string("run.end_date") == JULY_WINDOW[1]
    assert config.get_string("weather.precompute_end_date") == JULY_PRECOMPUTE_END


def test_both_shipped_scenarios_load_and_keep_their_names():
    loaded = calibration.shipped_scenarios()

    assert [name for name, _ in loaded] == [
        Path(name).stem for name in calibration.SCENARIO_FILENAMES
    ]
    assert len({config.config_hash for _, config in loaded}) == 2


@pytest.mark.parametrize("seed", ENVELOPE_SEEDS)
def test_the_monsoon_window_realizes_storms_inside_a_wide_envelope(monsoon_storm_counts, seed):
    july_count, _ = monsoon_storm_counts[seed]

    assert july_count >= 1
    assert MIN_JULY_STORM_ZONE_DAYS <= july_count <= MAX_JULY_STORM_ZONE_DAYS


def test_the_monsoon_window_carries_more_storm_zone_days_than_the_default_window(
    monsoon_storm_counts,
):
    july_total = sum(july for july, _ in monsoon_storm_counts.values())
    october_total = sum(october for _, october in monsoon_storm_counts.values())

    assert july_total > october_total


def test_the_calibration_report_carries_the_pinned_top_level_keys(short_matrix):
    report, _ = short_matrix

    assert tuple(sorted(report)) == CALIBRATION_REPORT_KEYS
    assert report["report_version"] == calibration.CALIBRATION_REPORT_VERSION
    assert report["seeds"] == [SEED]
    assert report["primary_seed"] == SEED
    assert [scenario["scenario"] for scenario in report["scenarios"]] == [
        "oct_default",
        "july_monsoon",
    ]
    for scenario in report["scenarios"]:
        assert tuple(sorted(scenario)) == SCENARIO_REPORT_KEYS


def test_the_written_calibration_report_is_byte_stable(short_matrix):
    report, out_dir = short_matrix
    written = (out_dir / calibration.CALIBRATION_REPORT_FILENAME).read_bytes()

    assert written == report_bytes(report)


def test_every_scenario_carries_one_row_per_policy_per_seed(short_matrix):
    report, _ = short_matrix

    for scenario in report["scenarios"]:
        assert [row["policy"] for row in scenario["rows"]] == list(calibration.POLICY_NAMES)
        for row in scenario["rows"]:
            assert row["master_seed"] == SEED
            assert row["scenario"] == scenario["scenario"]
            assert len(row["dump_hash"]) == 64
            assert len(row["ledger_digest"]) == 64


def test_every_matrix_row_reports_raw_final_cash_and_diagnostics(short_matrix):
    report, _ = short_matrix

    for scenario in report["scenarios"]:
        for row in scenario["rows"]:
            assert "score_cents" not in row
            assert isinstance(row["final_cash_cents"], int)
            assert tuple(sorted(row["diagnostics"])) == (
                "appointments",
                "bookings_by_urgency",
                "daily_cash_cents",
                "ledger_by_category",
                "utilization",
            )
            assert row["diagnostics"]["daily_cash_cents"]


def test_every_named_check_is_present_with_its_inputs(short_matrix):
    report, _ = short_matrix

    for scenario in report["scenarios"]:
        assert tuple(check["name"] for check in scenario["checks"]) == calibration.CHECK_NAMES
        for check in scenario["checks"]:
            assert isinstance(check["passed"], bool)
            assert check["inputs"]["seed_count"] == 1


def test_the_ledger_digest_is_stable_across_a_re_read_of_the_same_run_file(short_matrix):
    _, out_dir = short_matrix
    db_path = out_dir / "oct_default" / str(SEED) / "accept_all" / "run.db"

    first = calibration.ledger_digest(db_path, geo_dir=GEO_ARTIFACT_DIR)
    second = calibration.ledger_digest(db_path, geo_dir=GEO_ARTIFACT_DIR)

    assert len(first) == 64
    assert int(first, 16) >= 0
    assert first == second


def test_the_ledger_digest_separates_two_different_runs(short_matrix):
    _, out_dir = short_matrix
    idle = out_dir / "oct_default" / str(SEED) / "do_nothing" / "run.db"
    booking = out_dir / "oct_default" / str(SEED) / "accept_all" / "run.db"

    assert calibration.ledger_digest(idle, geo_dir=GEO_ARTIFACT_DIR) != calibration.ledger_digest(
        booking, geo_dir=GEO_ARTIFACT_DIR
    )


def test_the_oracle_row_is_played_through_the_public_policy_runner(short_matrix):
    _, out_dir = short_matrix
    for scenario in ("oct_default", "july_monsoon"):
        run_dir = out_dir / scenario / str(SEED) / calibration.ORACLE_POLICY_NAME
        stored = json.loads((run_dir / REPORT_FILENAME).read_bytes())

        assert calibration.ORACLE_POLICY_NAME == oracle.POLICY_NAME == "oracle"
        assert stored["policy"] == oracle.POLICY_NAME
        assert (run_dir / "run.db").is_file()
