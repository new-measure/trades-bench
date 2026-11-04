from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
)
from policies import accept_all, oracle
from policies.base import AgentApi, ApiResult, ParsedCall, PolicyRequest
from policies.oracle import (
    PUBLISHED_DURATIONS,
    OraclePolicy,
    Stop,
    expected_minutes,
    late_stops,
    planned_load_minutes,
    quantile_minutes,
    reassignment_fits,
    route_plan,
    travel_minutes,
)
from policies.runner import run_policy
from trades_bench import clock
from trades_bench.config import RunConfig, load_run_config
from trades_bench.registry import OPERATIONS_BY_NAME

SEED = 5
THREE_DAYS = ("2026-10-01", "2026-10-03")
SHORT_HORIZON_DAYS = 3

MONDAY = "2026-10-05"
TUESDAY = "2026-10-06"
SATURDAY = "2026-10-03"
DEFAULT_QUANTILE = 0.65
INSTALL_MINUTES = quantile_minutes(PUBLISHED_DURATIONS, "hvac_installation", DEFAULT_QUANTILE)
MINUTE_MS = 60_000

HOME = (33.40, -111.90)
NEAR = (33.41, -111.91)
FAR = (33.70, -112.30)

JOB_TYPE_ROWS = [
    {"id": 1, "code": "hvac_service", "scheduled_blocks": 1, "required_skill_code": None},
    {"id": 2, "code": "plumbing", "scheduled_blocks": 1, "required_skill_code": "plumbing"},
]

CUSTOMER_ID = 3
LOCATION_ID = 7
TECHNICIAN_ID = 1
OTHER_TECHNICIAN_ID = 2

BOOKING_OPERATIONS = frozenset(
    {
        "appointments.acknowledge_cancellation",
        "calls.reject",
        "crm.create_customer",
        "crm.create_location",
        "jobs.book",
    }
)
TABLE_REFERENCE_PATTERN = re.compile(r"(?:FROM|JOIN)\s+(\w+)", re.IGNORECASE)
FORBIDDEN_IMPORTS = (
    "sqlite3",
    "trades_bench.db",
    "trades_bench.txn",
    "trades_bench.ledger",
    "trades_bench.controller",
    "trades_bench.control",
    "trades_bench.run_init",
    "trades_bench._dump",
    "trades_bench.config",
)


def at(service_date: str, hour: int, minute: int = 0) -> int:
    year, month, day = (int(part) for part in service_date.split("-"))
    return clock.phoenix_instant(year, month, day, hour, minute)


def stop(
    service_date: str,
    start_hour: int,
    point: tuple[float, float],
    duration_minutes: int,
    *,
    started_at: int | None = None,
) -> Stop:
    return Stop(
        window_start=at(service_date, start_hour),
        window_end=at(service_date, start_hour + 2),
        point=point,
        duration_minutes=duration_minutes,
        started_at=started_at,
    )


def route_row(
    service_date: str,
    start_hour: int,
    point: tuple[float, float],
    job_type_code: str,
    *,
    actual_start: int | None = None,
    technician_id: int = TECHNICIAN_ID,
    required_skill_code: str | None = None,
) -> dict[str, Any]:
    return {
        "technician_id": technician_id,
        "appointment_id": 90 + start_hour,
        "window_start": at(service_date, start_hour),
        "window_end": at(service_date, start_hour + 2),
        "latitude": point[0],
        "longitude": point[1],
        "job_type_code": job_type_code,
        "required_skill_code": required_skill_code,
        "status": "scheduled" if actual_start is None else "in_progress",
        "actual_start": actual_start,
    }


def shift_row(
    service_date: str,
    start_hour: int = 7,
    end_hour: int = 17,
    technician_id: int = TECHNICIAN_ID,
) -> dict[str, Any]:
    return {
        "technician_id": technician_id,
        "service_date": service_date,
        "start_at": at(service_date, start_hour),
        "end_at": at(service_date, end_hour),
    }


class FakeApi:

    def __init__(
        self,
        *,
        routes: list[dict[str, Any]] = (),
        shifts: list[dict[str, Any]] = (),
        technician_ids: tuple[int, ...] = (TECHNICIAN_ID,),
        skills: list[dict[str, Any]] = (),
    ) -> None:
        self.answers: dict[str, list[dict[str, Any]]] = {
            accept_all.JOB_TYPES_SQL: JOB_TYPE_ROWS,
            accept_all.TECHNICIANS_SQL: [{"id": tid} for tid in technician_ids],
            accept_all.SKILLS_SQL: list(skills),
            accept_all.SHIFTS_SQL: list(shifts),
            oracle.HOMES_SQL: [
                {"id": tid, "home_latitude": HOME[0], "home_longitude": HOME[1]}
                for tid in technician_ids
            ],
            oracle.ROUTE_SQL: list(routes),
            oracle.LOCATION_SQL.format(location_id=LOCATION_ID): [
                {"latitude": NEAR[0], "longitude": NEAR[1]}
            ],
        }
        self.statements: list[str] = []
        self.operations: list[tuple[str, Mapping[str, Any]]] = []

    def sql(self, statement: str) -> list[dict[str, Any]]:
        self.statements.append(statement)
        return [dict(row) for row in self.answers[statement]]

    def op(self, operation: str, body: Mapping[str, Any]) -> ApiResult:
        self.operations.append((operation, dict(body)))
        return ApiResult(
            operation=operation, status=200, body={"data": {"outcome": "booked"}}
        )

    def calls_to(self, name: str) -> list[Mapping[str, Any]]:
        return [body for operation, body in self.operations if operation == name]

    def booked(self) -> list[Mapping[str, Any]]:
        return [body for operation, body in self.operations if operation == "jobs.book"]


def request_for(
    api: FakeApi,
    *,
    local_time: str,
    urgent: bool = False,
    requested_service: str = "HVAC service",
    is_cancellation: bool = False,
    horizon_days: int = 0,
) -> PolicyRequest:
    call = ParsedCall(
        call_id=11,
        local_time=local_time,
        caller="Fixture Caller",
        is_cancellation=is_cancellation,
        urgent=urgent,
        customer_id=CUSTOMER_ID,
        location_id=LOCATION_ID,
        appointment_id=5 if is_cancellation else None,
        requested_service=requested_service,
    )
    return PolicyRequest(
        policy_name=oracle.POLICY_NAME,
        call=call,
        api=api,
        service_date=local_time[:10],
        horizon_days=horizon_days,
    )


def oracle_config(dates: tuple[str, str]) -> RunConfig:
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.start_date": dates[0],
        "run.end_date": dates[1],
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def test_the_oracle_is_named_and_versioned_as_the_good_practice_reference():
    policy = OraclePolicy()

    assert oracle.POLICY_NAME == "oracle"
    assert oracle.POLICY_VERSION == "2.1.0"
    assert policy.name == oracle.POLICY_NAME
    assert policy.version == oracle.POLICY_VERSION
    assert policy.margin_minutes == 40
    assert policy.urgent_margin_minutes == 40
    assert policy.defer_off_hours is False
    assert policy.reassign is False
    assert policy.planning_quantile == DEFAULT_QUANTILE
    assert policy.install_latest_start is None
    assert policy.max_planned_minutes is None
    assert policy.rescue is False


def test_the_published_handbook_triangles_give_the_mean_and_default_planning_durations():
    durations = load_run_config().resolved["execution.duration_minutes_by_job_type"]
    assert PUBLISHED_DURATIONS == durations
    assert {code: expected_minutes(durations, code) for code in (
        "hvac_service", "plumbing", "electrical", "hvac_installation"
    )} == {"hvac_service": 100, "plumbing": 100, "electrical": 115, "hvac_installation": 300}
    assert {code: quantile_minutes(durations, code, DEFAULT_QUANTILE) for code in (
        "hvac_service", "plumbing", "electrical", "hvac_installation"
    )} == {"hvac_service": 110, "plumbing": 110, "electrical": 127, "hvac_installation": 330}
    assert OraclePolicy().durations == PUBLISHED_DURATIONS


def test_an_empty_route_fits_without_driving():
    assert route_plan(HOME, [], floor_ms=at(MONDAY, 6, 45), margin_minutes=20) == (True, 0)


def test_the_first_stop_waits_for_its_window_and_counts_the_drive_from_home():
    fits, driven = route_plan(
        HOME, [stop(MONDAY, 9, FAR, 60)], floor_ms=at(MONDAY, 6, 45), margin_minutes=20
    )

    assert fits is True
    assert driven == travel_minutes(HOME, FAR)


def test_a_route_fits_only_while_each_start_leaves_the_margin_before_its_window_ends():
    first = stop(MONDAY, 9, NEAR, 100)
    second = stop(MONDAY, 9, NEAR, 60)
    gap = travel_minutes(NEAR, NEAR)
    slack = 120 - 100 - gap

    assert route_plan(HOME, [first, second], floor_ms=at(MONDAY, 6), margin_minutes=slack)[0]
    assert not route_plan(
        HOME, [first, second], floor_ms=at(MONDAY, 6), margin_minutes=slack + 1
    )[0]


def test_an_in_progress_stop_holds_the_technician_until_it_is_expected_to_finish():
    started = stop(MONDAY, 9, NEAR, 120, started_at=at(MONDAY, 9))
    gap = travel_minutes(NEAR, NEAR)
    floor = at(MONDAY, 9, 30)
    fits_after, driven = route_plan(
        HOME, [started, stop(MONDAY, 10, NEAR, 60)], floor_ms=floor, margin_minutes=20
    )
    late = stop(MONDAY, 9, NEAR, 161 - gap, started_at=at(MONDAY, 9))

    assert fits_after is True
    assert driven == gap
    assert route_plan(
        HOME, [late, stop(MONDAY, 10, NEAR, 60)], floor_ms=floor, margin_minutes=20
    ) == (False, gap)


def test_a_new_call_books_the_earliest_daytime_window_that_fits_the_route():
    api = FakeApi(shifts=[shift_row(MONDAY)])
    outcome = OraclePolicy().decide(
        request_for(api, local_time=f"{MONDAY} 06:00")
    )

    assert outcome.terminal is True
    assert [
        (body["service_date"], body["block_start"], body["technician_id"])
        for body in api.booked()
    ] == [(MONDAY, "07:00", TECHNICIAN_ID)]
    assert api.booked()[0]["customer_id"] == CUSTOMER_ID
    assert api.booked()[0]["location_id"] == LOCATION_ID


def test_a_stop_whose_window_already_ended_unstarted_is_dropped_from_the_route():
    stale = route_row(MONDAY, 9, FAR, "hvac_installation")
    api = FakeApi(routes=[stale], shifts=[shift_row(MONDAY)])
    OraclePolicy().decide(request_for(api, local_time=f"{MONDAY} 11:30"))

    assert [body["block_start"] for body in api.booked()] == ["13:00"]


def test_a_started_stop_stays_on_the_route_after_its_window_ends():
    running = route_row(
        MONDAY, 7, NEAR, "hvac_installation", actual_start=at(MONDAY, 7)
    )
    reachable = 11 * 60 + 2 * 60 - (7 * 60 + INSTALL_MINUTES) - travel_minutes(NEAR, NEAR)
    api = FakeApi(routes=[running], shifts=[shift_row(MONDAY)])
    OraclePolicy(margin_minutes=reachable + 1).decide(
        request_for(api, local_time=f"{MONDAY} 09:30")
    )

    assert [body["block_start"] for body in api.booked()] == ["13:00"]


def test_the_margin_skips_a_window_the_route_would_reach_too_late():
    running = route_row(
        MONDAY, 7, NEAR, "hvac_installation", actual_start=at(MONDAY, 7)
    )
    reachable = 11 * 60 + 2 * 60 - (7 * 60 + INSTALL_MINUTES) - travel_minutes(NEAR, NEAR)
    picks = {}
    for margin in (reachable, reachable + 1):
        api = FakeApi(routes=[running], shifts=[shift_row(MONDAY)])
        OraclePolicy(margin_minutes=margin).decide(
            request_for(api, local_time=f"{MONDAY} 09:30")
        )
        picks[margin] = api.booked()[0]["block_start"]

    assert picks == {reachable: "11:00", reachable + 1: "13:00"}


def test_an_urgent_call_plans_with_its_own_margin():
    running = route_row(
        MONDAY, 7, NEAR, "hvac_installation", actual_start=at(MONDAY, 7)
    )
    reachable = 11 * 60 + 2 * 60 - (7 * 60 + INSTALL_MINUTES) - travel_minutes(NEAR, NEAR)
    api = FakeApi(routes=[running], shifts=[shift_row(MONDAY)])
    OraclePolicy(margin_minutes=reachable + 1, urgent_margin_minutes=reachable).decide(request_for(api, local_time=f"{MONDAY} 09:30", urgent=True))

    assert [body["block_start"] for body in api.booked()] == ["11:00"]


def test_when_no_window_keeps_the_margin_the_oracle_falls_back_to_a_zero_margin_plan():
    policy = OraclePolicy(margin_minutes=121)
    request = request_for(FakeApi(shifts=[shift_row(MONDAY)]), local_time=f"{MONDAY} 06:00")
    strict = policy._slots(
        request,
        policy._board(request),
        point=NEAR,
        duration_minutes=60,
        required_skill_code=None,
        margin_minutes=121,
    )
    api = FakeApi(shifts=[shift_row(MONDAY)])
    outcome = policy.decide(request_for(api, local_time=f"{MONDAY} 06:00"))

    assert strict == []
    assert outcome.terminal is True
    assert [body["block_start"] for body in api.booked()] == ["07:00"]


def test_a_call_whose_route_fits_nowhere_is_rejected():
    api = FakeApi(shifts=[])
    outcome = OraclePolicy().decide(
        request_for(api, local_time=f"{MONDAY} 06:00")
    )

    assert outcome.terminal is True
    assert [operation for operation, _ in api.operations] == ["calls.reject"]


def test_with_deferral_a_routine_call_defers_off_hours_windows_while_an_urgent_call_does_not():
    shifts = [shift_row(SATURDAY), shift_row(MONDAY)]
    picks = {}
    for urgent in (False, True):
        api = FakeApi(shifts=shifts)
        OraclePolicy(defer_off_hours=True).decide(
            request_for(api, local_time=f"{SATURDAY} 06:00", urgent=urgent, horizon_days=2)
        )
        picks[urgent] = (api.booked()[0]["service_date"], api.booked()[0]["block_start"])

    assert picks == {False: (MONDAY, "07:00"), True: (SATURDAY, "07:00")}


def test_by_default_a_routine_call_takes_the_earliest_off_hours_window():
    api = FakeApi(shifts=[shift_row(SATURDAY), shift_row(MONDAY)])
    OraclePolicy().decide(
        request_for(api, local_time=f"{SATURDAY} 06:00", horizon_days=2)
    )

    assert (api.booked()[0]["service_date"], api.booked()[0]["block_start"]) == (
        SATURDAY, "07:00"
    )


def test_a_reassignment_fits_only_when_both_routes_keep_the_margin():
    floor = at(MONDAY, 6, 45)
    full_day = [stop(MONDAY, hour, NEAR, 90) for hour in (7, 9, 11, 13, 15)]
    new_stop = stop(MONDAY, 7, NEAR, 90)
    moved = full_day[0]

    assert not route_plan(HOME, [*full_day, new_stop], floor_ms=floor, margin_minutes=40)[0]
    assert reassignment_fits(
        (HOME, full_day), (HOME, []),
        moved=moved, new_stop=new_stop, floor_ms=floor, margin_minutes=40,
    )
    assert not reassignment_fits(
        (HOME, full_day), (HOME, [stop(MONDAY, 7, FAR, 90)]),
        moved=moved, new_stop=new_stop, floor_ms=floor, margin_minutes=40,
    )
    assert not reassignment_fits(
        (HOME, full_day), (HOME, []),
        moved=full_day[1], new_stop=new_stop, floor_ms=floor, margin_minutes=40,
    )


def _full_plumber_day() -> FakeApi:
    return FakeApi(
        routes=[
            route_row(MONDAY, hour, NEAR, "hvac_service", required_skill_code="hvac_service")
            for hour in (7, 9, 11, 13, 15)
        ],
        shifts=[shift_row(MONDAY), shift_row(MONDAY, technician_id=OTHER_TECHNICIAN_ID)],
        technician_ids=(TECHNICIAN_ID, OTHER_TECHNICIAN_ID),
        skills=[
            {"technician_id": TECHNICIAN_ID, "skill_code": "plumbing"},
            {"technician_id": TECHNICIAN_ID, "skill_code": "hvac_service"},
            {"technician_id": OTHER_TECHNICIAN_ID, "skill_code": "hvac_service"},
        ],
    )


def test_with_reassignment_the_oracle_moves_one_job_to_free_a_window_for_the_new_call():
    api = _full_plumber_day()
    outcome = OraclePolicy(reassign=True).decide(
        request_for(api, local_time=f"{MONDAY} 06:00", requested_service="Plumbing")
    )

    assert outcome.terminal is True
    assert [operation for operation, _ in api.operations] == ["dispatch.assign", "jobs.book"]
    assert api.calls_to("dispatch.assign")[0]["appointment_id"] == 97
    assert api.calls_to("dispatch.assign")[0]["technician_id"] == OTHER_TECHNICIAN_ID
    assert (api.booked()[0]["block_start"], api.booked()[0]["technician_id"]) == (
        "07:00", TECHNICIAN_ID
    )


def test_without_reassignment_a_full_route_falls_back_to_the_zero_margin_plan():
    api = _full_plumber_day()
    OraclePolicy().decide(
        request_for(api, local_time=f"{MONDAY} 06:00", requested_service="Plumbing")
    )

    assert api.calls_to("dispatch.assign") == []
    assert len(api.booked()) == 1


def test_a_planning_quantile_reads_the_published_triangle_inverse_cdf():
    service = PUBLISHED_DURATIONS["hvac_service"]

    assert quantile_minutes(PUBLISHED_DURATIONS, "hvac_service", 0.0) == service["min"]
    assert quantile_minutes(PUBLISHED_DURATIONS, "hvac_service", 1.0) == service["max"]
    assert quantile_minutes(PUBLISHED_DURATIONS, "hvac_service", 0.1) == 66
    assert quantile_minutes(PUBLISHED_DURATIONS, "hvac_service", 0.25) == 77
    assert {code: quantile_minutes(PUBLISHED_DURATIONS, code, 0.75) for code in (
        "hvac_service", "plumbing", "electrical", "hvac_installation"
    )} == {"hvac_service": 121, "plumbing": 121, "electrical": 140, "hvac_installation": 353}


def test_a_higher_planning_quantile_keeps_a_window_free_the_mean_would_fill():
    existing = route_row(MONDAY, 7, NEAR, "hvac_service")
    slack = 120 - expected_minutes(PUBLISHED_DURATIONS, "hvac_service") - travel_minutes(NEAR, NEAR)
    picks = {}
    for quantile in (None, 0.75):
        api = FakeApi(routes=[existing], shifts=[shift_row(MONDAY)])
        OraclePolicy(
            margin_minutes=slack, urgent_margin_minutes=slack, planning_quantile=quantile
        ).decide(request_for(api, local_time=f"{MONDAY} 06:00"))
        picks[quantile] = api.booked()[0]["block_start"]

    assert picks == {None: "07:00", 0.75: "09:00"}


def _install_api(*shift_dates: str) -> FakeApi:
    api = FakeApi(shifts=[shift_row(service_date) for service_date in shift_dates])
    api.answers[accept_all.JOB_TYPES_SQL] = [
        *JOB_TYPE_ROWS,
        {"id": 3, "code": "hvac_installation", "scheduled_blocks": 1, "required_skill_code": None},
    ]
    return api


def test_morning_installs_wait_for_a_morning_window_when_one_fits():
    picks = {}
    for latest in (None, "11:00"):
        api = _install_api(MONDAY, TUESDAY)
        OraclePolicy(install_latest_start=latest).decide(
            request_for(
                api, local_time=f"{MONDAY} 11:30",
                requested_service="HVAC installation", horizon_days=1,
            )
        )
        picks[latest] = (api.booked()[0]["service_date"], api.booked()[0]["block_start"])

    assert picks == {None: (MONDAY, "13:00"), "11:00": (TUESDAY, "07:00")}


def test_morning_installs_fall_back_to_an_afternoon_window_when_no_morning_fits():
    api = _install_api(MONDAY)
    OraclePolicy(install_latest_start="11:00").decide(
        request_for(api, local_time=f"{MONDAY} 11:30", requested_service="HVAC installation")
    )

    assert [body["block_start"] for body in api.booked()] == ["13:00"]


def test_a_service_call_ignores_the_morning_install_rule():
    api = FakeApi(shifts=[shift_row(MONDAY), shift_row(TUESDAY)])
    OraclePolicy(install_latest_start="11:00").decide(
        request_for(api, local_time=f"{MONDAY} 11:30", horizon_days=1)
    )

    assert [body["block_start"] for body in api.booked()] == ["13:00"]


def test_the_planned_load_counts_on_site_and_driving_minutes():
    assert planned_load_minutes([stop(MONDAY, 7, NEAR, 100), stop(MONDAY, 9, NEAR, 60)], 12) == 172


def test_a_day_cap_moves_the_call_to_a_technician_with_room():
    def two_technicians() -> FakeApi:
        return FakeApi(
            routes=[route_row(MONDAY, 7, NEAR, "hvac_service")],
            shifts=[shift_row(MONDAY), shift_row(MONDAY, technician_id=OTHER_TECHNICIAN_ID)],
            technician_ids=(TECHNICIAN_ID, OTHER_TECHNICIAN_ID),
        )

    picks = {}
    for cap in (None, 150):
        api = two_technicians()
        OraclePolicy(max_planned_minutes=cap).decide(
            request_for(api, local_time=f"{MONDAY} 07:30")
        )
        picks[cap] = (api.booked()[0]["block_start"], api.booked()[0]["technician_id"])

    assert picks == {None: ("09:00", TECHNICIAN_ID), 150: ("09:00", OTHER_TECHNICIAN_ID)}


def test_late_stops_are_those_the_route_reaches_after_their_window_ends():
    stops = [stop(MONDAY, 9, NEAR, 100) for _ in range(3)]

    assert late_stops(HOME, stops, floor_ms=at(MONDAY, 8, 30)) == [stops[2]]
    assert late_stops(HOME, stops[:2], floor_ms=at(MONDAY, 8, 30)) == []


def _overbooked_morning() -> FakeApi:
    rows = []
    for appointment_id in (201, 202, 203):
        row = route_row(MONDAY, 9, NEAR, "hvac_service")
        rows.append({**row, "appointment_id": appointment_id})
    return FakeApi(
        routes=rows,
        shifts=[shift_row(MONDAY), shift_row(MONDAY, technician_id=OTHER_TECHNICIAN_ID)],
        technician_ids=(TECHNICIAN_ID, OTHER_TECHNICIAN_ID),
    )


def test_with_rescue_an_appointment_the_route_would_miss_moves_to_a_free_technician():
    api = _overbooked_morning()
    outcome = OraclePolicy(rescue=True).decide(request_for(api, local_time=f"{MONDAY} 08:30"))

    assert outcome.terminal is True
    assert [operation for operation, _ in api.operations] == ["dispatch.assign", "jobs.book"]
    assert api.calls_to("dispatch.assign")[0]["appointment_id"] in {201, 202, 203}
    assert api.calls_to("dispatch.assign")[0]["technician_id"] == OTHER_TECHNICIAN_ID


def test_without_rescue_an_at_risk_appointment_stays_where_it_is():
    api = _overbooked_morning()
    OraclePolicy().decide(request_for(api, local_time=f"{MONDAY} 08:30"))

    assert api.calls_to("dispatch.assign") == []


def test_a_cancellation_call_is_acknowledged_without_booking():
    api = FakeApi(shifts=[shift_row(MONDAY)])
    outcome = OraclePolicy().decide(
        request_for(api, local_time=f"{MONDAY} 06:00", is_cancellation=True)
    )

    assert outcome.terminal is True
    assert [operation for operation, _ in api.operations] == [
        "appointments.acknowledge_cancellation"
    ]
    assert api.statements == []


def test_an_unsupported_service_is_rejected_without_booking():
    api = FakeApi(shifts=[shift_row(MONDAY)])
    outcome = OraclePolicy().decide(
        request_for(api, local_time=f"{MONDAY} 06:00", requested_service="Pool cleaning")
    )

    assert outcome.terminal is True
    assert [operation for operation, _ in api.operations] == ["calls.reject"]


def test_the_oracle_module_imports_nothing_that_reaches_around_the_public_api():
    path = Path(oracle.__file__)
    targets: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append(node.module)

    assert [
        target
        for target in targets
        if any(target == bad or target.startswith(f"{bad}.") for bad in FORBIDDEN_IMPORTS)
    ] == []


def test_every_oracle_read_names_only_agent_views():
    api = FakeApi(routes=[route_row(MONDAY, 9, FAR, "hvac_service")], shifts=[shift_row(MONDAY)])
    OraclePolicy().decide(request_for(api, local_time=f"{MONDAY} 06:00"))
    tables = {
        table for statement in api.statements for table in TABLE_REFERENCE_PATTERN.findall(statement)
    }

    assert tables
    assert not {table for table in tables if table.startswith("_")}


@pytest.fixture(scope="module")
def recorded_oracle_run(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    statements: list[str] = []
    operations: list[str] = []
    original_sql, original_op = AgentApi.sql, AgentApi.op

    def recording_sql(self, statement):
        statements.append(statement)
        return original_sql(self, statement)

    def recording_op(self, operation, body):
        operations.append(operation)
        return original_op(self, operation, body)

    patch.setattr(AgentApi, "sql", recording_sql)
    patch.setattr(AgentApi, "op", recording_op)
    try:
        config = oracle_config(THREE_DAYS)
        result = run_policy(
            OraclePolicy(),
            config=config,
            master_seed=SEED,
            run_dir=tmp_path_factory.mktemp("oracle") / "run",
            geo_dir=GEO_ARTIFACT_DIR,
            horizon_days=SHORT_HORIZON_DAYS,
            sleep=lambda _seconds: None,
        )
    finally:
        patch.undo()
    return result, statements, operations


def test_the_oracle_drives_a_scored_run_through_the_public_policy_runner(recorded_oracle_run):
    result, _, operations = recorded_oracle_run

    assert result.run_status == "completed"
    assert result.report["policy"] == oracle.POLICY_NAME
    assert "jobs.book" in operations


def test_a_scored_oracle_run_touches_only_public_operations_and_views(recorded_oracle_run):
    _, statements, operations = recorded_oracle_run
    tables = {
        table for statement in statements for table in TABLE_REFERENCE_PATTERN.findall(statement)
    }

    assert set(operations) <= BOOKING_OPERATIONS
    assert set(operations) <= set(OPERATIONS_BY_NAME)
    assert tables
    assert not {table for table in tables if table.startswith("_")}
