from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import policies
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    register_conn,
)
from policies import accept_all
from policies.accept_all import AcceptAllPolicy
from policies.base import (
    AgentApi,
    ApiResult,
    PolicyApiError,
    PolicyOutcome,
    PolicyRequest,
    parse_call,
)
from policies.do_nothing import DoNothingPolicy
from policies.reject_heavy import RejectHeavyPolicy, reject_draw
from policies.runner import PolicyRunResult, run_policy
from trades_bench import clock
from trades_bench.config import RunConfig, load_run_config
from trades_bench.harness import prompts
from trades_bench.registry import OPERATIONS_BY_NAME
from trades_bench.run_init import open_run_db

SEED = 5
TWO_WEEKS = ("2026-10-01", "2026-10-14")
TWO_WEEK_HORIZON_DAYS = 14

THREE_DAYS = ("2026-10-01", "2026-10-03")
SHORT_HORIZON_DAYS = 3

BEYOND_END_SERVICE_DATE = "2026-10-08"
BEYOND_END_BLOCK_START = "09:00"

ACTIVE_TECHNICIANS_SQL = (
    "SELECT id FROM technicians WHERE employment_status = 'active' ORDER BY id"
)

POLICY_NAMES = ("do_nothing", "accept_all", "reject_heavy")
BOOKING_POLICY_NAMES = ("accept_all", "reject_heavy")

POLICY_MODULE_NAMES = ("__init__", "base", "accept_all", "do_nothing", "reject_heavy")
OPERATOR_MODULE_NAMES = ("runner", "oracle", "fork_search")
FORBIDDEN_POLICY_IMPORTS = (
    "sqlite3",
    "trades_bench.db",
    "trades_bench.txn",
    "trades_bench.ledger",
    "trades_bench.controller",
    "trades_bench.control",
    "trades_bench.run_init",
    "trades_bench._dump",
)

UNQUALIFIED_ASSIGNMENTS_SQL = (
    "SELECT COUNT(*) AS total FROM _appointments a "
    "JOIN _appointment_assignments aa ON aa.appointment_id = a.id "
    "JOIN _jobs j ON j.id = a.job_id "
    "JOIN _job_types jt ON jt.id = j.job_type_id "
    "WHERE jt.required_skill_code IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM _technician_skills ts WHERE ts.technician_id = aa.technician_id "
    "AND ts.skill_code = jt.required_skill_code AND ts.active = 1)"
)
SKILLED_ASSIGNMENTS_SQL = (
    "SELECT COUNT(*) AS total FROM _appointments a "
    "JOIN _appointment_assignments aa ON aa.appointment_id = a.id "
    "JOIN _jobs j ON j.id = a.job_id "
    "JOIN _job_types jt ON jt.id = j.job_type_id "
    "WHERE jt.required_skill_code IS NOT NULL"
)


def policy_config(dates: tuple[str, str]) -> RunConfig:
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.start_date": dates[0],
        "run.end_date": dates[1],
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def build_policy(name: str, config: RunConfig):
    if name == "do_nothing":
        return DoNothingPolicy()
    if name == "accept_all":
        return AcceptAllPolicy()
    return RejectHeavyPolicy.from_config(config, master_seed=SEED)


def drive(policy, config: RunConfig, run_dir: Path, horizon_days: int) -> PolicyRunResult:
    return run_policy(
        policy,
        config=config,
        master_seed=SEED,
        run_dir=run_dir,
        geo_dir=GEO_ARTIFACT_DIR,
        horizon_days=horizon_days,
        sleep=lambda seconds: None,
    )


def open_world(db_path: Path):
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    return conn


def call_rows(db_path: Path) -> list[tuple[int, str, str]]:
    conn = open_world(db_path)
    rows = [
        (int(row["id"]), str(row["call_kind"]), str(row["status"]))
        for row in conn.execute("SELECT id, call_kind, status FROM _calls ORDER BY id")
    ]
    conn.close()
    return rows


def appointment_rows(db_path: Path) -> list[tuple[int, str, str]]:
    conn = open_world(db_path)
    rows = [
        (
            int(row["id"]),
            str(row["status"]),
            clock.service_date_for_ms(int(row["first_block_start"])),
        )
        for row in conn.execute(
            "SELECT a.id AS id, a.status AS status, "
            "MIN(ab.block_start) AS first_block_start "
            "FROM _appointments a JOIN _appointment_blocks ab "
            "ON ab.appointment_id = a.id GROUP BY a.id ORDER BY a.id"
        )
    ]
    conn.close()
    return rows


def final_score_cents(db_path: Path) -> int:
    conn = open_world(db_path)
    row = conn.execute("SELECT final_score_cents FROM _run").fetchone()
    conn.close()
    return int(row["final_score_cents"])


def skill_match_counts(db_path: Path) -> tuple[int, int]:
    conn = open_world(db_path)
    checked = int(conn.execute(SKILLED_ASSIGNMENTS_SQL).fetchone()["total"])
    unqualified = int(conn.execute(UNQUALIFIED_ASSIGNMENTS_SQL).fetchone()["total"])
    conn.close()
    return checked, unqualified


def slugify_service(requested_service: str) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in requested_service.lower()
    )


def unsupported_new_job_ids(db_path: Path) -> set[int]:
    conn = open_world(db_path)
    codes = {
        str(row["code"])
        for row in conn.execute("SELECT code FROM _job_types WHERE active = 1")
    }
    unsupported = {
        int(row["id"])
        for row in conn.execute(
            "SELECT id, requested_service_name FROM _calls WHERE call_kind = 'new_job'"
        )
        if slugify_service(str(row["requested_service_name"])) not in codes
    }
    conn.close()
    return unsupported


def new_job_ids(rows: list[tuple[int, str, str]]) -> set[int]:
    return {call_id for call_id, kind, _ in rows if kind == "new_job"}


def rejected_ids(rows: list[tuple[int, str, str]]) -> set[int]:
    return {
        call_id
        for call_id, kind, status in rows
        if kind == "new_job" and status == "rejected"
    }


def payload_probe(result: ApiResult) -> str:
    return json.dumps(
        {"data": dict(result.data), "error": dict(result.error)}, sort_keys=True
    )


class RecordingApi:

    def __init__(self, api: AgentApi) -> None:
        self.api = api
        self.calls: list[tuple[str, Mapping[str, Any], ApiResult]] = []

    def op(self, operation: str, body: Mapping[str, Any]) -> ApiResult:
        result = self.api.op(operation, body)
        self.calls.append((operation, dict(body), result))
        return result

    def sql(self, statement: str) -> list[dict[str, Any]]:
        return self.api.sql(statement)


def job_type_for(api, requested_service: str | None) -> Mapping[str, Any] | None:
    if not requested_service:
        return None
    wanted = slugify_service(requested_service)
    for row in api.sql(accept_all.JOB_TYPES_SQL):
        if str(row["code"]) == wanted:
            return row
    return None


def register_caller(request: PolicyRequest) -> tuple[int | None, int | None]:
    call = request.call
    customer_id = call.customer_id
    if customer_id is None:
        if call.segment is None or call.phone is None:
            return None, None
        result = request.api.op(
            "crm.create_customer",
            {
                "call_id": call.call_id,
                "name": call.caller,
                "segment": call.segment,
                "contacts": [{"contact_type": "phone", "value": call.phone}],
                "request_id": request.request_id("customer"),
            },
        )
        if not result.ok:
            return None, None
        customer_id = int(result.data["customer_id"])
    location_id = call.location_id
    if location_id is None:
        if call.address is None:
            return customer_id, None
        result = request.api.op(
            "crm.create_location",
            {
                "call_id": call.call_id,
                "customer_id": customer_id,
                "address": call.address,
                "request_id": request.request_id("location"),
            },
        )
        if not result.ok:
            return customer_id, None
        location_id = int(result.data["location_id"])
    return customer_id, location_id


def assign_technician(
    request: PolicyRequest, appointment_id: int, technician_id: int
) -> ApiResult:
    return request.api.op(
        "dispatch.assign",
        {
            "appointment_id": appointment_id,
            "technician_id": technician_id,
            "request_id": request.request_id("assign", technician_id),
        },
    )


def assign_every_technician(
    request: PolicyRequest, appointment_id: int
) -> list[ApiResult]:
    return [
        assign_technician(request, appointment_id, int(row["id"]))
        for row in request.api.sql(ACTIVE_TECHNICIANS_SQL)
    ]


class BeyondEndBooker:

    name = "beyond_end_booker"
    version = "1.0.0"

    def __init__(self, service_date: str) -> None:
        self.service_date = service_date
        self.bookings: list[ApiResult] = []
        self.assignments: list[ApiResult] = []
        self.booking: ApiResult | None = None
        self.appointment_id: int | None = None
        self.control_booking: ApiResult | None = None
        self.control_assignment: ApiResult | None = None
        self.control_appointment_id: int | None = None

    def decide(self, request: PolicyRequest) -> PolicyOutcome:
        if request.call.is_cancellation:
            return accept_all.accept_all_decision(request)
        if self.appointment_id is None:
            return self._book_beyond_end(request)
        if self.control_appointment_id is None:
            return self._book_in_window(request)
        return PolicyOutcome()

    def _book_beyond_end(self, request: PolicyRequest) -> PolicyOutcome:
        call = request.call
        job_type = job_type_for(request.api, call.requested_service)
        if job_type is None:
            return PolicyOutcome(notes=("no supported job type for this call",))
        customer_id, location_id = register_caller(request)
        if customer_id is None or location_id is None:
            return PolicyOutcome(notes=("the caller could not be registered",))
        technician_ids = [int(row["id"]) for row in request.api.sql(ACTIVE_TECHNICIANS_SQL)]
        if not technician_ids:
            return PolicyOutcome(notes=("no active technician to book against",))
        result = request.api.op(
            "jobs.book",
            {
                "call_id": call.call_id,
                "customer_id": customer_id,
                "location_id": location_id,
                "job_type_id": int(job_type["id"]),
                "service_date": self.service_date,
                "block_start": BEYOND_END_BLOCK_START,
                "technician_id": technician_ids[0],
                "request_id": request.request_id("beyond"),
            },
        )
        self.bookings.append(result)
        if not result.ok or result.outcome != "accepted":
            return PolicyOutcome(notes=(result.describe(),))
        self.booking = result
        self.appointment_id = int(result.data["appointment_id"])
        self.assignments.extend(assign_every_technician(request, self.appointment_id))
        return PolicyOutcome(terminal=True, notes=(f"booked {result.outcome}",))

    def _book_in_window(self, request: PolicyRequest) -> PolicyOutcome:
        recorder = RecordingApi(request.api)
        outcome = accept_all.book_new_job(replace(request, api=recorder))
        booked = next(
            (
                (body, result)
                for operation, body, result in recorder.calls
                if operation == "jobs.book" and result.data.get("appointment_id")
            ),
            None,
        )
        if booked is None:
            return outcome
        body, result = booked
        self.control_booking = result
        self.control_appointment_id = int(result.data["appointment_id"])
        self.control_assignment = assign_technician(
            request, self.control_appointment_id, int(body["technician_id"])
        )
        return outcome


class RetryOnceAcceptAll:

    name = accept_all.POLICY_NAME
    version = accept_all.POLICY_VERSION

    def __init__(self) -> None:
        self.invoked_call_ids: list[int] = []
        self.raised_for: int | None = None

    def decide(self, request: PolicyRequest) -> PolicyOutcome:
        call_id = request.call.call_id
        self.invoked_call_ids.append(call_id)
        if self.raised_for is None:
            self.raised_for = call_id
            raise PolicyApiError("simulated transient adapter failure")
        return accept_all.accept_all_decision(request)


def _policy_module_paths() -> list[Path]:
    root = Path(policies.__file__).parent
    return [root / f"{name}.py" for name in POLICY_MODULE_NAMES]


def _imported_targets(tree: ast.AST) -> list[str]:
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or "" if node.level == 0 else "policies"
            targets.extend(
                f"{base}.{alias.name}" if base else alias.name for alias in node.names
            )
            targets.append(base)
    return targets


def _forbidden(target: str) -> bool:
    return any(
        target == forbidden or target.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_POLICY_IMPORTS
    )


@pytest.fixture(scope="module")
def config() -> RunConfig:
    return policy_config(TWO_WEEKS)


@pytest.fixture(scope="module")
def short_config() -> RunConfig:
    return policy_config(THREE_DAYS)


@pytest.fixture(scope="module")
def twin_runs(tmp_path_factory, config) -> dict[str, tuple[PolicyRunResult, ...]]:
    base = tmp_path_factory.mktemp("reference-policies")
    return {
        name: tuple(
            drive(
                build_policy(name, config),
                config,
                base / f"{name}-{label}",
                TWO_WEEK_HORIZON_DAYS,
            )
            for label in ("a", "b")
        )
        for name in POLICY_NAMES
    }


@pytest.fixture(scope="module")
def beyond_end_twin_runs(
    tmp_path_factory, short_config
) -> tuple[tuple[PolicyRunResult, BeyondEndBooker], ...]:
    base = tmp_path_factory.mktemp("beyond-end")
    runs = []
    for label in ("a", "b"):
        policy = BeyondEndBooker(BEYOND_END_SERVICE_DATE)
        runs.append(
            (drive(policy, short_config, base / label, SHORT_HORIZON_DAYS), policy)
        )
    return tuple(runs)


@pytest.fixture(scope="module")
def retry_runs(
    tmp_path_factory, short_config
) -> tuple[PolicyRunResult, RetryOnceAcceptAll, PolicyRunResult]:
    base = tmp_path_factory.mktemp("retry-once")
    flaky = RetryOnceAcceptAll()
    flaky_result = drive(flaky, short_config, base / "flaky", SHORT_HORIZON_DAYS)
    clean_result = drive(
        AcceptAllPolicy(), short_config, base / "clean", SHORT_HORIZON_DAYS
    )
    return flaky_result, flaky, clean_result


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_pol_common_every_reference_policy_runs_headless_to_completed(twin_runs, name):
    for result in twin_runs[name]:
        assert result.run_status == "completed"
        assert result.report["run_status"] == "completed"
        assert result.report["policy"] == name
        assert result.report["master_seed"] == SEED
        assert result.report["first_service_date"] == TWO_WEEKS[0]
        assert result.report["last_service_date"] == TWO_WEEKS[1]
        assert result.report_path.read_bytes() == result.report_bytes


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_pol_common_twin_runs_of_one_policy_reach_the_same_logical_dump(twin_runs, name):
    first, second = twin_runs[name]
    assert first.paths.db_path != second.paths.db_path
    assert first.report["dump_hash"] == second.report["dump_hash"]
    assert first.report_bytes == second.report_bytes
    assert call_rows(first.paths.db_path) == call_rows(second.paths.db_path)


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_pol_common_every_report_reconciles_its_cash_with_the_finalized_run_file(
    twin_runs, config, name
):
    report = twin_runs[name][0].report
    starting_cash_cents = config.get_int("finance.starting_cash")

    assert report["starting_cash_cents"] == starting_cash_cents
    assert report["score_cents"] == (
        report["final_cash_cents"] - report["starting_cash_cents"]
    )
    assert report["daily_cash_cents"][-1][1] == report["final_cash_cents"]
    assert report["final_cash_cents"] == final_score_cents(
        twin_runs[name][0].paths.db_path
    )


@pytest.mark.parametrize("name", POLICY_NAMES)
def test_pol_common_no_reference_policy_reaches_bankruptcy_on_the_two_week_window(
    twin_runs, name
):
    for result in twin_runs[name]:
        assert result.report["bankruptcy_date"] is None


def test_pol05_do_nothing_leaves_no_active_calls_and_only_unanswered_ones(twin_runs):
    result = twin_runs["do_nothing"][0]
    rows = call_rows(result.paths.db_path)
    statuses = [status for _, _, status in rows]

    assert rows
    assert statuses.count("active") == 0
    assert statuses.count("unanswered") == len(rows)
    assert result.report["calls"]["new_job"]["unanswered"] > 0
    assert result.report["calls"]["new_job"]["booked"] == 0
    assert appointment_rows(result.paths.db_path) == []


def test_pol05_do_nothing_never_reaches_a_cancellation_call_because_it_books_nothing(
    twin_runs,
):
    counts = twin_runs["do_nothing"][0].report["calls"]["cancellation"]

    assert counts
    assert set(counts.values()) == {0}


def test_pol02_accept_all_books_where_do_nothing_books_none(twin_runs):
    booking_result = twin_runs["accept_all"][0]
    idle_result = twin_runs["do_nothing"][0]

    assert idle_result.report["calls"]["new_job"]["booked"] == 0
    assert idle_result.report["utilization"]["assigned_blocks"] == 0
    assert booking_result.report["calls"]["new_job"]["booked"] >= 1
    assert booking_result.report["utilization"]["assigned_blocks"] >= 1
    assert appointment_rows(booking_result.paths.db_path) != []


def test_pol01_every_accept_all_booking_went_to_a_technician_holding_the_required_skill(
    twin_runs,
):
    for result in twin_runs["accept_all"]:
        checked, unqualified = skill_match_counts(result.paths.db_path)
        assert checked >= 1
        assert unqualified == 0


def test_pol02_every_accept_all_appointment_lands_in_window_and_reaches_a_terminal_status(
    twin_runs,
):
    result = twin_runs["accept_all"][0]
    rows = appointment_rows(result.paths.db_path)

    assert rows
    for _, status, service_date in rows:
        assert status in ("completed", "failed", "no_show", "cancelled")
        assert TWO_WEEKS[0] <= service_date <= TWO_WEEKS[1]


@pytest.mark.parametrize("name", BOOKING_POLICY_NAMES)
def test_pol_common_a_booking_policy_acknowledges_at_least_one_cancellation(
    twin_runs, name
):
    assert twin_runs[name][0].report["calls"]["cancellation"]["acknowledged"] >= 1


def test_pol03_reject_heavy_rejects_a_seed_stable_proper_subset(twin_runs, config):
    first, second = twin_runs["reject_heavy"]
    first_rows = call_rows(first.paths.db_path)
    rejected = rejected_ids(first_rows)
    offered = new_job_ids(first_rows)
    policy = RejectHeavyPolicy.from_config(config, master_seed=SEED)
    drawn = {call_id for call_id in offered if policy.rejects(call_id)}

    assert rejected == rejected_ids(call_rows(second.paths.db_path))
    assert drawn
    assert drawn < offered
    assert drawn <= rejected
    assert rejected < offered
    assert first.report["calls"]["new_job"]["rejected"] == len(rejected)


def test_pol03_rejects_is_exactly_the_pinned_keyed_draw_under_the_threshold(config):
    policy = RejectHeavyPolicy.from_config(config, master_seed=SEED)

    assert policy.reject_threshold == 14757395258967641292
    assert reject_draw("reject_heavy", SEED, 1) == 11928565927411889805
    assert reject_draw("reject_heavy", SEED, 2) == 3433797076190799741
    assert reject_draw("reject_heavy", SEED, 3) == 3330977923073208539
    for call_id in range(1, 201):
        assert policy.rejects(call_id) == (
            reject_draw("reject_heavy", SEED, call_id) < policy.reject_threshold
        )


def test_pol03_reject_heavy_rejects_exactly_the_drawn_calls_plus_the_unsupported_ones(
    twin_runs, config
):
    result = twin_runs["reject_heavy"][0]
    rows = call_rows(result.paths.db_path)
    offered = new_job_ids(rows)
    policy = RejectHeavyPolicy.from_config(config, master_seed=SEED)
    drawn = {call_id for call_id in offered if policy.rejects(call_id)}
    unsupported = unsupported_new_job_ids(result.paths.db_path) & offered

    assert unsupported
    assert rejected_ids(rows) == drawn | unsupported


def test_a_transient_adapter_failure_is_re_decided_and_leaves_the_world_unchanged(
    retry_runs,
):
    flaky_result, flaky, clean_result = retry_runs
    raised_for = flaky.raised_for
    flaky_rows = {call_id: status for call_id, _, status in call_rows(flaky_result.paths.db_path)}
    clean_rows = {call_id: status for call_id, _, status in call_rows(clean_result.paths.db_path)}

    assert raised_for is not None
    assert flaky.invoked_call_ids.count(raised_for) == 2
    assert flaky_rows[raised_for] != "unanswered"
    assert flaky_rows[raised_for] == clean_rows[raised_for]
    assert flaky_result.run_status == "completed"
    assert flaky_result.report_bytes == clean_result.report_bytes


def test_parse_call_reads_the_cancellation_discriminator_from_the_appointment_id_field():
    cancellation = prompts.render_call(
        {
            "time": "2026-10-01T08:15",
            "call_id": 41,
            "caller": "Dana Reyes",
            "customer_id": 7,
            "location_id": 9,
            "appointment_id": 13,
            "scheduled": "2026-10-02T09:00",
            "scheduled_end": "2026-10-02T11:00",
        }
    )
    new_job = prompts.render_call(
        {
            "time": "2026-10-01T09:30",
            "call_id": 42,
            "caller": "Sam Okafor",
            "phone": "480-555-0143",
            "address": "1 Palm Way, Tempe, AZ 85281",
            "customer_type": "residential",
            "requested_service": "AC repair",
            "urgent": False,
        }
    )

    cancelling = parse_call(cancellation)
    booking = parse_call(new_job)

    assert cancelling.is_cancellation
    assert cancelling.appointment_id == 13
    assert not booking.is_cancellation
    assert booking.appointment_id is None
    assert booking.requested_service == "AC repair"


def test_booking_beyond_the_last_service_date_commits_without_leaking_the_timeline(
    beyond_end_twin_runs,
):
    result, policy = beyond_end_twin_runs[0]
    booking = policy.booking
    last_service_date = result.report["last_service_date"]

    assert last_service_date == THREE_DAYS[1]
    assert BEYOND_END_SERVICE_DATE > last_service_date
    assert booking is not None
    assert booking.ok
    assert booking.error == {}
    assert booking.outcome == "accepted"
    assert last_service_date not in payload_probe(booking)


def test_booking_beyond_the_last_service_date_finalizes_clean_and_stays_scheduled(
    beyond_end_twin_runs,
):
    result, policy = beyond_end_twin_runs[0]
    appointments = appointment_rows(result.paths.db_path)
    dangling = [row for row in appointments if row[0] == policy.appointment_id]

    assert result.run_status == "completed"
    assert dangling == [(policy.appointment_id, "scheduled", BEYOND_END_SERVICE_DATE)]
    assert result.report["appointments"]["failed"] == 0
    assert result.report["appointments"]["no_show"] == 0


def test_booking_beyond_the_last_service_date_keeps_twin_runs_identical(
    beyond_end_twin_runs,
):
    (first, first_policy), (second, second_policy) = beyond_end_twin_runs

    assert first.report["dump_hash"] == second.report["dump_hash"]
    assert first.report_bytes == second.report_bytes
    assert first_policy.appointment_id == second_policy.appointment_id
    assert first_policy.control_appointment_id == second_policy.control_appointment_id


def test_dispatch_assign_on_a_beyond_end_appointment_never_leaks_the_timeline(
    beyond_end_twin_runs,
):
    result, policy = beyond_end_twin_runs[0]
    last_service_date = result.report["last_service_date"]

    assert "dispatch.assign" in OPERATIONS_BY_NAME
    assert not [name for name in OPERATIONS_BY_NAME if name.startswith("workforce.")]
    assert len(policy.assignments) >= 2
    for assignment in policy.assignments:
        assert assignment.ok, assignment.describe()
        assert assignment.error == {}
        assert int(assignment.data["appointment_id"]) == policy.appointment_id
        assert last_service_date not in payload_probe(assignment)


def test_dispatch_assign_answers_a_beyond_end_appointment_exactly_as_an_in_window_one(
    beyond_end_twin_runs,
):
    _, policy = beyond_end_twin_runs[0]
    control = policy.control_assignment
    beyond_end = policy.assignments[0] if policy.assignments else None

    assert control is not None
    assert beyond_end is not None
    assert policy.control_appointment_id != policy.appointment_id
    assert set(beyond_end.data) == set(control.data)
    assert beyond_end.error == {}
    assert control.error == {}


def test_no_policy_module_reaches_around_the_public_http_boundary():
    scanned: list[str] = []
    offenders: list[tuple[str, str]] = []
    for path in _policy_module_paths():
        scanned.append(path.name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            (path.name, target)
            for target in _imported_targets(tree)
            if _forbidden(target)
        )
    package_files = {
        path.name for path in Path(policies.__file__).parent.glob("*.py")
    }

    assert offenders == []
    assert scanned == [f"{name}.py" for name in POLICY_MODULE_NAMES]
    assert package_files == set(scanned) | {
        f"{name}.py" for name in OPERATOR_MODULE_NAMES
    }
