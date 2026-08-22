from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from policies import calibration
from policies.runner import REPORT_FILENAME
from trades_bench import SPEC_VERSION

REPO_ROOT = Path(__file__).parent.parent
REAL_ARTIFACT_DIR = REPO_ROOT / "data" / "geography-arizona-v1"
REQUIRED_ARTIFACT_FILES = (
    REAL_ARTIFACT_DIR / "addresses" / "catalog.csv",
    REAL_ARTIFACT_DIR / "routing" / "anchor_distance.bin",
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "reg02"
REGEN_ENV = "REGEN_GOLDEN_REG02"

LEDGER_DIGEST_FIELD = "ledger_digest"

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.skipif(
        not all(path.is_file() for path in REQUIRED_ARTIFACT_FILES),
        reason="the Arizona geography artifact is absent; build it with tools/fetch_geography.sh",
    ),
]


@pytest.fixture(scope="module")
def real_matrix(tmp_path_factory) -> tuple[dict, Path]:
    out_dir = tmp_path_factory.mktemp("calibration-nightly")
    report = calibration.run_matrix(
        scenarios=calibration.shipped_scenarios(),
        out_dir=out_dir,
        geo_dir=REAL_ARTIFACT_DIR,
        workers=int(os.environ.get("CALIBRATION_WORKERS", "1")),
    )
    return report, out_dir


def rows_by_seed_and_policy(scenario: dict) -> dict[tuple[int, str], dict]:
    return {(row["master_seed"], row["policy"]): row for row in scenario["rows"]}


def final_cash_by_policy(scenario: dict, seed: int) -> dict[str, int]:
    indexed = rows_by_seed_and_policy(scenario)
    return {
        policy: indexed[(seed, policy)]["final_cash_cents"]
        for policy in calibration.POLICY_NAMES
    }


def golden_key(config_version: str, primary_seed: int) -> str:
    return "__".join((SPEC_VERSION, config_version, str(primary_seed)))


def golden_path(config_version: str, primary_seed: int) -> Path:
    return GOLDEN_DIR / f"{golden_key(config_version, primary_seed)}.json"


def recorded_document(report: dict, out_dir: Path) -> bytes:
    primary_seed = int(report["primary_seed"])
    scenarios: dict[str, dict] = {}
    for scenario in report["scenarios"]:
        name = scenario["scenario"]
        indexed = rows_by_seed_and_policy(scenario)
        policies: dict[str, dict] = {}
        for policy in calibration.POLICY_NAMES:
            stored = json.loads(
                (out_dir / name / str(primary_seed) / policy / REPORT_FILENAME).read_bytes()
            )
            stored[LEDGER_DIGEST_FIELD] = indexed[(primary_seed, policy)][LEDGER_DIGEST_FIELD]
            policies[policy] = stored
        scenarios[name] = policies
    document = {
        "spec_version": SPEC_VERSION,
        "config_version": report["scenarios"][0]["config_version"],
        "master_seed": primary_seed,
        "scenarios": scenarios,
    }
    return (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode()


def test_the_oracle_completes_above_accept_all_on_every_world(real_matrix):
    report, _ = real_matrix
    failures: list[tuple[str, int, int, int, str]] = []
    for scenario in report["scenarios"]:
        for seed in report["seeds"]:
            indexed = rows_by_seed_and_policy(scenario)
            accept_all = indexed[(seed, calibration.ACCEPT_ALL_POLICY_NAME)]
            oracle = indexed[(seed, calibration.ORACLE_POLICY_NAME)]
            if (
                oracle["run_status"] != calibration.COMPLETED_RUN_STATUS
                or oracle["final_cash_cents"] <= accept_all["final_cash_cents"]
            ):
                failures.append(
                    (
                        scenario["scenario"],
                        seed,
                        accept_all["final_cash_cents"],
                        oracle["final_cash_cents"],
                        oracle["run_status"],
                    )
                )

    assert failures == []


def test_do_nothing_and_reject_heavy_bankrupt_on_every_world(real_matrix):
    report, _ = real_matrix
    failures: list[tuple[str, int, str, str, str | None, int]] = []
    for scenario in report["scenarios"]:
        for seed in report["seeds"]:
            indexed = rows_by_seed_and_policy(scenario)
            for policy in (
                calibration.DO_NOTHING_POLICY_NAME,
                calibration.REJECT_HEAVY_POLICY_NAME,
            ):
                row = indexed[(seed, policy)]
                if (
                    row["run_status"] != calibration.BANKRUPT_RUN_STATUS
                    or row["bankruptcy_date"] is None
                    or row["final_cash_cents"] >= 0
                ):
                    failures.append(
                        (
                            scenario["scenario"],
                            seed,
                            policy,
                            row["run_status"],
                            row["bankruptcy_date"],
                            row["final_cash_cents"],
                        )
                    )

    assert failures == []


def test_accept_all_is_bankrupt_or_finishes_below_the_cash_ceiling(real_matrix):
    report, _ = real_matrix
    failures: list[tuple[str, int, str, int]] = []
    for scenario in report["scenarios"]:
        cash_ceiling = next(
            config.get_int(calibration.ACCEPT_ALL_MAX_FINAL_CASH_KEY)
            for name, config in calibration.shipped_scenarios()
            if name == scenario["scenario"]
        )
        for seed in report["seeds"]:
            row = rows_by_seed_and_policy(scenario)[
                (seed, calibration.ACCEPT_ALL_POLICY_NAME)
            ]
            valid = row["run_status"] == calibration.BANKRUPT_RUN_STATUS or (
                row["run_status"] == calibration.COMPLETED_RUN_STATUS
                and 0 < row["final_cash_cents"] <= cash_ceiling
            )
            if not valid:
                failures.append(
                    (
                        scenario["scenario"],
                        seed,
                        row["run_status"],
                        row["final_cash_cents"],
                    )
                )

    assert failures == []


def test_every_calibration_check_passes_on_every_scenario(real_matrix):
    report, _ = real_matrix
    failed = [
        (scenario["scenario"], check["name"], check["inputs"])
        for scenario in report["scenarios"]
        for check in scenario["checks"]
        if not check["passed"]
    ]

    assert failed == []
    for scenario in report["scenarios"]:
        assert tuple(check["name"] for check in scenario["checks"]) == calibration.CHECK_NAMES


def test_every_row_carries_raw_cash_and_native_diagnostics(real_matrix):
    report, _ = real_matrix

    for scenario in report["scenarios"]:
        for row in scenario["rows"]:
            assert "score_cents" not in row
            assert isinstance(row["final_cash_cents"], int)
            assert row["diagnostics"]["ledger_by_category"]
            assert row["diagnostics"]["daily_cash_cents"]


def test_golden_primary_seed_reports(real_matrix, tmp_path):
    report, out_dir = real_matrix
    recorded = recorded_document(report, out_dir)
    config_version = report["scenarios"][0]["config_version"]
    primary_seed = int(report["primary_seed"])
    path = golden_path(config_version, primary_seed)
    if os.environ.get(REGEN_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recorded)
        pytest.skip(f"{REGEN_ENV}=1 rewrote {path.name}")
    assert path.is_file(), (
        f"missing calibration golden {path}; rerun with {REGEN_ENV}=1 to record it deliberately"
    )
    stored = path.read_bytes()
    if stored != recorded:
        actual = tmp_path / "actual.json"
        actual.write_bytes(recorded)
        raise AssertionError(
            f"calibration goldens changed for key {golden_key(config_version, primary_seed)}; "
            f"compare {path} with {actual} and rerun with {REGEN_ENV}=1 only for a deliberate "
            "world change"
        )


def test_golden_document_covers_every_scenario_and_policy(real_matrix):
    report, _ = real_matrix
    config_version = report["scenarios"][0]["config_version"]
    primary_seed = int(report["primary_seed"])
    path = golden_path(config_version, primary_seed)
    assert path.is_file(), (
        f"missing calibration golden {path}; rerun with {REGEN_ENV}=1 to record it deliberately"
    )
    stored = json.loads(path.read_bytes())

    assert stored["spec_version"] == SPEC_VERSION
    assert stored["config_version"] == config_version
    assert stored["master_seed"] == primary_seed
    assert tuple(stored["scenarios"]) == tuple(
        scenario["scenario"] for scenario in report["scenarios"]
    )
    for policies in stored["scenarios"].values():
        assert tuple(policies) == calibration.POLICY_NAMES
        for stored_report in policies.values():
            assert len(stored_report[LEDGER_DIGEST_FIELD]) == 64
