from __future__ import annotations

import json

import pytest

from policies import calibration, experiment
from policies.oracle import OraclePolicy


def row(
    *,
    policy: str,
    final_cash_cents: int,
    run_status: str,
    bankruptcy_date: str | None,
) -> dict:
    return {
        "bankruptcy_date": bankruptcy_date,
        "final_cash_cents": final_cash_cents,
        "master_seed": 5,
        "policy": policy,
        "run_status": run_status,
        "scenario": "oct_default",
        "trial": "candidate",
    }


def passing_rows() -> list[dict]:
    return [
        row(
            policy=calibration.DO_NOTHING_POLICY_NAME,
            final_cash_cents=-1,
            run_status=calibration.BANKRUPT_RUN_STATUS,
            bankruptcy_date="2026-10-12",
        ),
        row(
            policy=calibration.ACCEPT_ALL_POLICY_NAME,
            final_cash_cents=1_000_000,
            run_status=calibration.COMPLETED_RUN_STATUS,
            bankruptcy_date=None,
        ),
        row(
            policy=calibration.REJECT_HEAVY_POLICY_NAME,
            final_cash_cents=-1,
            run_status=calibration.BANKRUPT_RUN_STATUS,
            bankruptcy_date="2026-10-12",
        ),
        row(
            policy=calibration.ORACLE_POLICY_NAME,
            final_cash_cents=1_000_001,
            run_status=calibration.COMPLETED_RUN_STATUS,
            bankruptcy_date=None,
        ),
    ]


def test_scenario_config_applies_trial_overrides_without_changing_the_scenario_file():
    path = calibration.SCENARIO_DIR / "oct_default.json"
    before = path.read_bytes()

    name, config = experiment._scenario_config(
        path,
        {"finance.starting_cash": 3_900_000},
    )

    assert name == "oct_default"
    assert config.get_int("finance.starting_cash") == 3_900_000
    assert path.read_bytes() == before


def test_trial_loader_rejects_duplicate_names(tmp_path):
    path = tmp_path / "trials.json"
    path.write_text(
        json.dumps(
            {
                "trials": [
                    {"name": "same", "overrides": {}},
                    {"name": "same", "overrides": {}},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        experiment._load_trials(path)


def test_trial_summary_accepts_the_strict_cash_ceiling_and_oracle_gap():
    trial = experiment.Trial(name="candidate", overrides={})
    summary = experiment._trial_summary(
        trial,
        (calibration.SCENARIO_DIR / "oct_default.json",),
        (5,),
        passing_rows(),
        oracle_requested=True,
    )

    assert summary["feasible"] is True
    assert summary["cheap_failures"] == []
    assert summary["oracle_failures"] == []
    assert summary["objective"] == {
        "median_oracle_gap_cents": 1,
        "min_oracle_gap_cents": 1,
    }


def test_trial_summary_rejects_an_oracle_that_does_not_beat_accept_all():
    rows = passing_rows()
    oracle = next(item for item in rows if item["policy"] == calibration.ORACLE_POLICY_NAME)
    oracle["final_cash_cents"] = 1_000_000
    trial = experiment.Trial(name="candidate", overrides={})

    summary = experiment._trial_summary(
        trial,
        (calibration.SCENARIO_DIR / "oct_default.json",),
        (5,),
        rows,
        oracle_requested=True,
    )

    assert summary["feasible"] is False
    assert summary["oracle_failures"] == [
        {
            "master_seed": 5,
            "policy": calibration.ORACLE_POLICY_NAME,
            "scenario": "oct_default",
        }
    ]
    assert summary["objective"] is None


def test_the_oracle_is_built_like_every_other_reference_policy(tmp_path):
    scenario_path = calibration.SCENARIO_DIR / "oct_default.json"
    _, config = experiment._scenario_config(scenario_path, {})
    task = experiment.ExperimentTask(
        trial=experiment.Trial(name="candidate", overrides={}),
        scenario_path=str(scenario_path),
        master_seed=5,
        policy=calibration.ORACLE_POLICY_NAME,
        geo_dir=str(tmp_path),
        work_root=str(tmp_path),
        horizon_days=14,
    )

    policy = experiment._policy(task, config)

    assert isinstance(policy, OraclePolicy)
    assert policy.name == calibration.ORACLE_POLICY_NAME


def test_the_experiment_cli_carries_no_search_budget_flags():
    options = {
        option
        for action in experiment.parser()._actions
        for option in action.option_strings
    }

    assert not {option for option in options if "fork" in option or "lookahead" in option}
    assert "--include-reject" not in options
    assert "--run-oracle-on-infeasible" not in options


def test_trial_summary_rejects_accept_all_above_the_strict_cash_ceiling():
    rows = passing_rows()
    accept_all = next(
        item
        for item in rows
        if item["policy"] == calibration.ACCEPT_ALL_POLICY_NAME
    )
    scenario_path = calibration.SCENARIO_DIR / "oct_default.json"
    _, config = experiment._scenario_config(scenario_path, {})
    accept_all["final_cash_cents"] = (
        config.get_int(calibration.ACCEPT_ALL_MAX_FINAL_CASH_KEY) + 1
    )
    trial = experiment.Trial(name="candidate", overrides={})

    summary = experiment._trial_summary(
        trial,
        (scenario_path,),
        (5,),
        rows,
        oracle_requested=False,
    )

    assert summary["feasible"] is False
    assert summary["cheap_failures"] == [
        {
            "master_seed": 5,
            "policy": calibration.ACCEPT_ALL_POLICY_NAME,
            "scenario": "oct_default",
        }
    ]


def test_trial_summary_can_require_accept_all_to_survive():
    rows = passing_rows()
    accept_all = next(
        item
        for item in rows
        if item["policy"] == calibration.ACCEPT_ALL_POLICY_NAME
    )
    accept_all.update(
        final_cash_cents=-1,
        run_status=calibration.BANKRUPT_RUN_STATUS,
        bankruptcy_date="2026-10-12",
    )
    trial = experiment.Trial(name="candidate", overrides={})

    summary = experiment._trial_summary(
        trial,
        (calibration.SCENARIO_DIR / "oct_default.json",),
        (5,),
        rows,
        oracle_requested=False,
        require_accept_all_survival=True,
    )

    assert summary["feasible"] is False
    assert summary["cheap_failures"] == [
        {
            "master_seed": 5,
            "policy": calibration.ACCEPT_ALL_POLICY_NAME,
            "scenario": "oct_default",
        }
    ]


def seed_file(tmp_path, seeds) -> object:
    path = tmp_path / "trials.json"
    path.write_text(
        json.dumps(
            {
                "seeds": seeds,
                "trials": [{"name": "held_out", "overrides": {}}],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_seeds_resolve_from_the_trial_file_when_the_flag_is_omitted(tmp_path):
    path = seed_file(tmp_path, [11, 23, 47])

    assert experiment._resolve_seeds(None, path) == (11, 23, 47)


def test_an_explicit_seeds_flag_overrides_the_trial_file_seed_list(tmp_path):
    path = seed_file(tmp_path, [11, 23, 47])

    assert experiment._resolve_seeds("5,101", path) == (5, 101)


def test_seeds_fall_back_to_the_screening_default_without_a_flag_or_file():
    assert experiment._resolve_seeds(None, None) == experiment.DEFAULT_SEEDS


def test_the_seed_resolver_rejects_an_empty_or_duplicated_trial_file_seed_list(tmp_path):
    with pytest.raises(ValueError, match="at least one seed"):
        experiment._resolve_seeds(None, seed_file(tmp_path, []))

    with pytest.raises(ValueError, match="unique"):
        experiment._resolve_seeds(None, seed_file(tmp_path, [11, 11]))


def test_the_held_out_trial_file_declares_its_ten_validation_seeds():
    path = calibration.SCENARIO_DIR.parent / "trials" / "held_out_validation.json"

    seeds = experiment._stored_seeds(path)

    assert seeds == (11, 23, 47, 89, 157, 271, 443, 701, 1103, 1601)
    assert not set(seeds) & {5, 101, 202, 303, 404}
