from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    register_conn,
)
from trades_bench import appointments, clock, constants, demand, geo_reader
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import RunStatus
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.txn import privileged, privileged_request_id

SEED = 5
DAY_ONE = "2026-10-01"
DAY_TWO = "2026-10-02"
DAY_THREE = "2026-10-03"
STARTING_CASH = 20_000_000
FINAL_CASH = 19_604_800
MIN_TRAVEL_MINUTES = 20
SINGLE_BLOCK_HOURS = 2
OVERRUN_DURATION_MINUTES = 600
RESUME_SAMPLE_COUNT = 16

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
CATALOG_BY_ADDRESS_ID = {row.address_id: row for row in ARTIFACT.catalog}

COMPLETED_CHAIN = ["dispatched", "arrived", "work_started", "work_completed"]

_JOB_TYPE_TO_CATEGORY_ID = {1: 2, 2: 1, 3: 3, 4: 4}


@dataclass(frozen=True)
class BookingOrder:

    key: str
    crm_id: int
    address_id: int
    technician_id: int
    job_type_id: int
    service: str
    service_date: str
    first_block_code: str


BOOKED_RUN_ORDERS: dict[str, tuple[BookingOrder, ...]] = {
    DAY_ONE: (
        BookingOrder("chain-first", 9101, 40, 1, 1, "HVAC Service", DAY_ONE, "B13_15"),
        BookingOrder("chain-second", 9102, 610, 1, 1, "HVAC Service", DAY_ONE, "B15_17"),
        BookingOrder("install", 9103, 150, 2, 2, "HVAC Installation", DAY_ONE, "B13_15"),
        BookingOrder("after-hours", 9104, 380, 4, 4, "Electrical", DAY_ONE, "B17_19"),
        BookingOrder("next-morning-hvac", 9105, 90, 1, 1, "HVAC Service", DAY_TWO, "B07_09"),
        BookingOrder("next-morning-plumbing", 9106, 520, 3, 3, "Plumbing", DAY_TWO, "B07_09"),
    ),
    DAY_TWO: (
        BookingOrder("midday-idle", 9107, 260, 2, 1, "HVAC Service", DAY_TWO, "B13_15"),
    ),
    DAY_THREE: (
        BookingOrder("weekend", 9108, 2, 3, 3, "Plumbing", DAY_THREE, "B13_15"),
    ),
}

ORDERS_BY_KEY = {
    order.key: order for orders in BOOKED_RUN_ORDERS.values() for order in orders
}

BOOKING_SEQUENCE = [
    "chain-first",
    "chain-second",
    "install",
    "after-hours",
    "next-morning-hvac",
    "next-morning-plumbing",
    "midday-idle",
    "weekend",
]

NO_SHOW_ORDERS: dict[str, tuple[BookingOrder, ...]] = {
    DAY_ONE: (
        BookingOrder("overrun", 9201, 40, 1, 1, "HVAC Service", DAY_ONE, "B11_13"),
        BookingOrder("starved", 9202, 610, 1, 1, "HVAC Service", DAY_ONE, "B15_17"),
    ),
}

DAY_TWO_TRACE = [
    ("day_rollover", "2026-10-02T06:45:00-07:00", 100),
    ("employment_transition", "2026-10-02T06:45:00-07:00", 110),
    ("weather_realized", "2026-10-02T06:45:00-07:00", 120),
    ("labor_economy_update", "2026-10-02T06:45:00-07:00", 130),
    ("sickness_draw", "2026-10-02T06:45:00-07:00", 140),
    ("attrition_draw", "2026-10-02T06:45:00-07:00", 150),
    ("training_rates_drawn", "2026-10-02T06:45:00-07:00", 160),
    ("shift_materialization", "2026-10-02T06:45:00-07:00", 170),
    ("demand_generation", "2026-10-02T06:45:00-07:00", 180),
    ("opening_invocation", "2026-10-02T06:45:00-07:00", 900),
    ("first_block_lock", "2026-10-02T06:45:00-07:00", 905),
    ("first_trip_dispatch", "2026-10-02T06:45:00-07:00", 906),
    ("appointment_arrival", "2026-10-02T07:05:00-07:00", 310),
    ("appointment_arrival", "2026-10-02T07:05:00-07:00", 310),
    ("work_started", "2026-10-02T07:05:00-07:00", 320),
    ("work_started", "2026-10-02T07:10:00-07:00", 320),
    ("call_invocation", "2026-10-02T08:03:00-07:00", 910),
    ("call_invocation", "2026-10-02T08:08:00-07:00", 910),
    ("block_lock", "2026-10-02T09:00:00-07:00", 200),
    ("work_completed", "2026-10-02T09:00:00-07:00", 330),
    ("company_no_show", "2026-10-02T09:00:00-07:00", 335),
    ("company_no_show", "2026-10-02T09:00:00-07:00", 335),
    ("technician_return_home", "2026-10-02T09:00:00-07:00", 350),
    ("call_invocation", "2026-10-02T09:04:00-07:00", 910),
    ("work_completed", "2026-10-02T09:08:00-07:00", 330),
    ("technician_return_home", "2026-10-02T09:08:00-07:00", 350),
    ("technician_home_arrival", "2026-10-02T09:20:00-07:00", 360),
    ("call_invocation", "2026-10-02T09:23:00-07:00", 910),
    ("technician_home_arrival", "2026-10-02T09:28:00-07:00", 360),
    ("call_invocation", "2026-10-02T09:50:00-07:00", 910),
    ("call_invocation", "2026-10-02T10:05:00-07:00", 910),
    ("call_invocation", "2026-10-02T10:07:00-07:00", 910),
    ("block_lock", "2026-10-02T11:00:00-07:00", 200),
    ("call_invocation", "2026-10-02T11:15:00-07:00", 910),
    ("call_invocation", "2026-10-02T11:52:00-07:00", 910),
    ("call_invocation", "2026-10-02T11:58:00-07:00", 910),
    ("appointment_dispatch", "2026-10-02T12:40:00-07:00", 300),
    ("call_invocation", "2026-10-02T12:56:00-07:00", 910),
    ("block_lock", "2026-10-02T13:00:00-07:00", 200),
    ("appointment_arrival", "2026-10-02T13:00:00-07:00", 310),
    ("work_started", "2026-10-02T13:00:00-07:00", 320),
    ("call_invocation", "2026-10-02T14:56:00-07:00", 910),
    ("block_lock", "2026-10-02T15:00:00-07:00", 200),
    ("company_no_show", "2026-10-02T15:00:00-07:00", 335),
    ("work_completed", "2026-10-02T15:04:00-07:00", 330),
    ("technician_return_home", "2026-10-02T15:04:00-07:00", 350),
    ("call_invocation", "2026-10-02T15:09:00-07:00", 910),
    ("call_invocation", "2026-10-02T15:11:00-07:00", 910),
    ("call_invocation", "2026-10-02T15:12:00-07:00", 910),
    ("technician_home_arrival", "2026-10-02T15:24:00-07:00", 360),
    ("call_invocation", "2026-10-02T15:44:00-07:00", 910),
    ("call_invocation", "2026-10-02T16:01:00-07:00", 910),
    ("block_lock", "2026-10-02T17:00:00-07:00", 200),
    ("call_invocation", "2026-10-02T17:49:00-07:00", 910),
    ("call_invocation", "2026-10-02T17:54:00-07:00", 910),
    ("closing_invocation", "2026-10-02T19:00:00-07:00", 920),
    ("daily_settlement", "2026-10-02T19:00:00-07:00", 950),
    ("checkpoint", "2026-10-02T19:00:00-07:00", 960),
]


def _insert_customer_location_call(conn, config, order: BookingOrder, now_ms: int) -> int:
    crm_id = order.crm_id
    address = CATALOG_BY_ADDRESS_ID[order.address_id]
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (?, ?, 'residential', 'active', ?, ?)",
        (crm_id, f"Customer {crm_id}", now_ms, now_ms),
    )
    demand.register_customer_repeat_state(
        conn, config, customer_id=crm_id, segment="residential", now_ms=now_ms
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, active, created_at, modified_at, "
        "effective_zone_id, address_catalog_id) "
        "VALUES (?, ?, 'Home', ?, '', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            crm_id, crm_id, address.street, address.city, address.state,
            address.postal_code, address.latitude, address.longitude,
            now_ms, now_ms, address.effective_zone_id, address.address_id,
        ),
    )
    cur = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (?, ?, 'new_job', 'booked', ?, '555-0100', ?, 0, ?)",
        (now_ms, order.service_date, f"Caller {crm_id}", order.service, now_ms),
    )
    call_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, "
        "prospect_id, acceptance_result, created_at) "
        "VALUES (?, 'residential', ?, NULL, NULL, ?)",
        (call_id, _JOB_TYPE_TO_CATEGORY_ID[order.job_type_id], now_ms),
    )
    return call_id


class ScriptedBookingAdapter(ScriptedNoOpAdapter):

    def __init__(self, orders_by_prompt_date: dict[str, tuple[BookingOrder, ...]]) -> None:
        super().__init__()
        self.controller = None
        self.orders_by_prompt_date = orders_by_prompt_date

    def send(self, prompt):
        if prompt.kind.value == "call":
            prompt_date = clock.service_date_for_ms(prompt.ts_ms)
            for order in self.orders_by_prompt_date.get(prompt_date, ()):
                self._book(order)
        return super().send(prompt)

    def _book(self, order: BookingOrder) -> None:
        controller = self.controller
        config = controller.config
        artifact = controller.artifact

        def fn(ctx):
            call_id = _insert_customer_location_call(ctx.conn, config, order, ctx.now())
            return appointments.book(
                ctx, config, artifact=artifact,
                call_id=call_id, customer_id=order.crm_id, location_id=order.crm_id,
                job_type_id=order.job_type_id, requested_service_name=order.service,
                priority="normal", summary=order.key, technician_id=order.technician_id,
                service_date=order.service_date, first_block_code=order.first_block_code,
            )

        body = json.loads(
            privileged(
                controller.services, privileged_request_id("scripted_booking", order.key), fn
            )
        )
        assert body["ok"], body


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": DAY_THREE,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _booked_config():
    return _config(**{"travel.min_travel_minutes": MIN_TRAVEL_MINUTES})


def _init(path, config):
    initialize_run(path, master_seed=SEED, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    return open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)


def _ms(service_date, hour, minute):
    d = date.fromisoformat(service_date)
    return clock.phoenix_instant(d.year, d.month, d.day, hour, minute)


def _run_id(conn):
    return conn.execute("SELECT run_id FROM _run").fetchone()["run_id"]


def _build_controller(conn, config, orders):
    adapter = ScriptedBookingAdapter(orders)
    controller = Controller(conn, config, adapter, artifact_dir=GEO_ARTIFACT_DIR)
    adapter.controller = controller
    return controller


def _run_counting_steps(conn, config, orders):
    controller = _build_controller(conn, config, orders)
    steps = 0
    while controller.step():
        steps += 1
    return controller.status(), steps


def _appointment_id(conn, key):
    return int(
        conn.execute(
            "SELECT a.id AS appointment_id FROM _appointments a "
            "JOIN _jobs j ON j.id = a.job_id WHERE j.summary = ?",
            (key,),
        ).fetchone()["appointment_id"]
    )


def _event_chain(conn, appointment_id):
    return [
        row["event_type"]
        for row in conn.execute(
            "SELECT event_type FROM _appointment_events WHERE appointment_id = ? "
            "ORDER BY occurred_at, id",
            (appointment_id,),
        )
    ]


@pytest.fixture(scope="module")
def booked_run(tmp_path_factory):
    path = tmp_path_factory.mktemp("acceptance_booked") / "run.db"
    config = _booked_config()
    conn = _init(path, config)
    status, steps = _run_counting_steps(conn, config, BOOKED_RUN_ORDERS)
    assert status is RunStatus.COMPLETED
    yield {
        "conn": conn,
        "config": config,
        "steps": steps,
        "dump": dump_hash(conn, _run_id(conn)),
    }
    conn.close()


def test_day_two_of_the_booked_run_replays_the_pinned_trace(booked_run):
    conn = booked_run["conn"]
    rows = conn.execute(
        "SELECT kind, scheduled_at_ms, priority FROM _events WHERE status = 'applied' "
        "AND scheduled_at_ms >= ? AND scheduled_at_ms < ? "
        "ORDER BY scheduled_at_ms, priority, event_id",
        (_ms(DAY_TWO, 0, 0), _ms(DAY_THREE, 0, 0)),
    ).fetchall()
    for row in rows:
        assert int(row["scheduled_at_ms"]) % 60000 == 0
    keys = [(int(row["scheduled_at_ms"]), int(row["priority"])) for row in rows]
    assert keys == sorted(keys)
    trace = [
        (row["kind"], clock.to_iso8601(row["scheduled_at_ms"]), row["priority"]) for row in rows
    ]
    assert trace == DAY_TWO_TRACE


def test_morning_first_dispatches_are_floor_clamped_to_the_planning_instant(booked_run):
    conn = booked_run["conn"]
    planning_instant = _ms(DAY_TWO, 6, 45)
    for key in ("next-morning-hvac", "next-morning-plumbing"):
        appointment_id = _appointment_id(conn, key)
        dispatched = conn.execute(
            "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
            "AND event_type = 'dispatched'",
            (appointment_id,),
        ).fetchone()
        assert int(dispatched["occurred_at"]) == planning_instant
        arrived = conn.execute(
            "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
            "AND event_type = 'arrived'",
            (appointment_id,),
        ).fetchone()
        assert int(arrived["occurred_at"]) == planning_instant + MIN_TRAVEL_MINUTES * 60000
    day_two_dispatch_entries = [
        entry for entry in DAY_TWO_TRACE if entry[0] == "appointment_dispatch"
    ]
    assert [entry[1] for entry in day_two_dispatch_entries] == ["2026-10-02T12:40:00-07:00"]


def test_final_cash_matches_the_hand_derived_booked_arithmetic(booked_run):
    conn = booked_run["conn"]
    config = booked_run["config"]
    starting = config.get_int("finance.starting_cash")
    assert starting == STARTING_CASH
    wages = {
        int(row["id"]): int(row["hourly_wage_cents"])
        for row in conn.execute("SELECT id, hourly_wage_cents FROM _technicians ORDER BY id")
    }
    weekday_count = 2
    day_count = 3
    daily_payroll = constants.WEEKDAY_PAY_MULTIPLIER * sum(wages.values())
    overhead = config.get_int("finance.daily_operating_overhead")
    bonus = config.get_int("payroll.off_hours_bonus_per_job")
    after_hours_technician = ORDERS_BY_KEY["after-hours"].technician_id
    weekend_technician = ORDERS_BY_KEY["weekend"].technician_id
    after_hours_job_pay = SINGLE_BLOCK_HOURS * wages[after_hours_technician] + bonus
    weekend_job_pay = SINGLE_BLOCK_HOURS * wages[weekend_technician] + bonus
    expected = (
        starting
        - weekday_count * daily_payroll
        - day_count * overhead
        - after_hours_job_pay
        - weekend_job_pay
    )
    assert expected == FINAL_CASH
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM _ledger_entries"
    ).fetchone()["s"]
    assert balance == expected
    off_hours_rows = conn.execute(
        "SELECT entry_type, amount_cents, service_date, technician_id FROM _ledger_entries "
        "WHERE entry_type IN ('payroll_off_hours', 'payroll_off_hours_bonus') ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in off_hours_rows] == [
        (
            "payroll_off_hours",
            -SINGLE_BLOCK_HOURS * wages[after_hours_technician],
            DAY_ONE,
            after_hours_technician,
        ),
        ("payroll_off_hours_bonus", -bonus, DAY_ONE, after_hours_technician),
        (
            "payroll_off_hours",
            -SINGLE_BLOCK_HOURS * wages[weekend_technician],
            DAY_THREE,
            weekend_technician,
        ),
        ("payroll_off_hours_bonus", -bonus, DAY_THREE, weekend_technician),
    ]


def test_every_booked_appointment_completes_with_a_legal_event_chain(booked_run):
    conn = booked_run["conn"]
    rows = conn.execute(
        "SELECT a.id, a.status, a.actual_start, a.actual_end, a.lateness_minutes, "
        "j.summary FROM _appointments a JOIN _jobs j ON j.id = a.job_id ORDER BY a.id"
    ).fetchall()
    assert [row["summary"] for row in rows] == BOOKING_SEQUENCE
    for row in rows:
        assert row["status"] == "completed"
        assert row["actual_start"] is not None
        assert row["actual_end"] is not None
        assert int(row["actual_end"]) > int(row["actual_start"])
        assert int(row["lateness_minutes"]) >= 0
        assert _event_chain(conn, int(row["id"])) == COMPLETED_CHAIN
    assert [int(row["lateness_minutes"]) for row in rows] == [0, 6, 0, 0, 0, 0, 0, 0]
    guard_rows = conn.execute(
        "SELECT entity_id, status FROM _events WHERE kind = 'company_no_show' ORDER BY event_id"
    ).fetchall()
    assert [row["status"] for row in guard_rows] == ["applied"] * len(rows)
    assert sorted(int(row["entity_id"]) for row in guard_rows) == [int(row["id"]) for row in rows]
    assert conn.execute("SELECT COUNT(*) c FROM _notifications").fetchone()["c"] == 0
    days = conn.execute("SELECT * FROM _day_state ORDER BY service_date").fetchall()
    assert [row["service_date"] for row in days] == [DAY_ONE, DAY_TWO, DAY_THREE]
    for day in days:
        assert day["closing_at"] is not None
        assert day["settlement_status"] == "settled"
        assert day["unresolved_execution_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE status = 'pending'"
    ).fetchone()["c"] == 0


def test_a_fresh_booked_run_from_the_same_seed_matches_the_baseline_dump(booked_run, tmp_path):
    config = _booked_config()
    conn = _init(tmp_path / "twin.db", config)
    register_conn(conn)
    status, _steps = _run_counting_steps(conn, config, BOOKED_RUN_ORDERS)
    assert status is RunStatus.COMPLETED
    assert dump_hash(conn, _run_id(conn)) == booked_run["dump"]


@pytest.mark.parametrize("slot", range(1, RESUME_SAMPLE_COUNT + 1))
def test_sampled_boundary_kills_resume_the_booked_run_to_the_identical_dump(
    booked_run, tmp_path, slot
):
    total = booked_run["steps"]
    boundary = max(1, slot * (total - 1) // RESUME_SAMPLE_COUNT)
    config = _booked_config()
    killed_path = tmp_path / f"killed_{boundary}.db"
    conn = _init(killed_path, config)
    try:
        status = _build_controller(conn, config, BOOKED_RUN_ORDERS).run(max_steps=boundary)
        assert status is RunStatus.ACTIVE, boundary
    finally:
        conn.close()
    resumed_path = tmp_path / f"resumed_{boundary}.db"
    shutil.copyfile(killed_path, resumed_path)
    resumed = open_run_db(resumed_path, artifact_dir=GEO_ARTIFACT_DIR)
    try:
        status = _build_controller(resumed, config, BOOKED_RUN_ORDERS).run()
        assert status is RunStatus.COMPLETED, boundary
        assert dump_hash(resumed, _run_id(resumed)) == booked_run["dump"], boundary
    finally:
        resumed.close()
    killed_path.unlink()
    resumed_path.unlink()


def _run_overrun_day(path):
    config = _config(**{
        "run.end_date": DAY_ONE,
        "appointments.min_actual_duration_minutes": OVERRUN_DURATION_MINUTES,
    })
    conn = _init(path, config)
    register_conn(conn)
    status, _steps = _run_counting_steps(conn, config, NO_SHOW_ORDERS)
    assert status is RunStatus.COMPLETED
    return conn


def test_an_overrunning_first_job_cascades_the_second_into_a_company_no_show(tmp_path):
    conn = _run_overrun_day(tmp_path / "overrun.db")
    overrun_id = _appointment_id(conn, "overrun")
    starved_id = _appointment_id(conn, "starved")
    overrun = conn.execute(
        "SELECT * FROM _appointments WHERE id = ?", (overrun_id,)
    ).fetchone()
    assert overrun["status"] == "completed"
    assert int(overrun["actual_start"]) >= _ms(DAY_ONE, 11, 0)
    assert (
        int(overrun["actual_end"])
        == int(overrun["actual_start"]) + OVERRUN_DURATION_MINUTES * 60000
    )
    assert int(overrun["actual_end"]) > _ms(DAY_ONE, 17, 0)
    assert _event_chain(conn, overrun_id) == COMPLETED_CHAIN

    starved = conn.execute(
        "SELECT * FROM _appointments WHERE id = ?", (starved_id,)
    ).fetchone()
    assert starved["status"] == "no_show"
    assert starved["actual_start"] is None
    assert starved["actual_end"] is None
    assert starved["lateness_minutes"] is None
    starved_events = conn.execute(
        "SELECT event_type, occurred_at, technician_id FROM _appointment_events "
        "WHERE appointment_id = ? ORDER BY occurred_at, id",
        (starved_id,),
    ).fetchall()
    assert [row["event_type"] for row in starved_events] == ["no_show"]
    assert int(starved_events[0]["occurred_at"]) == _ms(DAY_ONE, 17, 0)
    assert int(starved_events[0]["technician_id"]) == NO_SHOW_ORDERS[DAY_ONE][1].technician_id

    guard_rows = conn.execute(
        "SELECT entity_id, status, scheduled_at_ms FROM _events "
        "WHERE kind = 'company_no_show' ORDER BY event_id"
    ).fetchall()
    assert [(int(row["entity_id"]), row["status"]) for row in guard_rows] == [
        (overrun_id, "applied"),
        (starved_id, "applied"),
    ]
    assert int(guard_rows[1]["scheduled_at_ms"]) == _ms(DAY_ONE, 17, 0)

    holds = {
        int(row["appointment_id"]): int(row["c"])
        for row in conn.execute(
            "SELECT appointment_id, COUNT(*) c FROM _technician_block_holds "
            "WHERE appointment_id IS NOT NULL GROUP BY appointment_id"
        )
    }
    assert holds.get(starved_id, 0) == 0
    assert holds[overrun_id] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM _appointment_blocks WHERE appointment_id = ?", (starved_id,)
    ).fetchone()["c"] == 1

    notifications = conn.execute("SELECT * FROM _notifications").fetchall()
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification["notification_type"] == "company_no_show"
    assert notification["entity_type"] == "appointment"
    assert int(notification["entity_id"]) == starved_id
    assert int(notification["created_at"]) == _ms(DAY_ONE, 17, 0)
    assert notification["service_date"] == DAY_ONE
    assert str(starved["appointment_number"]) in notification["message"]
    assert DAY_ONE in notification["message"]

    day = conn.execute(
        "SELECT * FROM _day_state WHERE service_date = ?", (DAY_ONE,)
    ).fetchone()
    assert int(day["closing_at"]) == int(overrun["actual_end"])
    assert day["settlement_status"] == "settled"
    assert day["unresolved_execution_count"] == 0
    pending = conn.execute(
        "SELECT kind FROM _events WHERE status = 'pending'"
    ).fetchall()
    assert [row["kind"] for row in pending] == ["technician_home_arrival"]


def test_the_overrun_day_is_twin_stable(tmp_path):
    first = _run_overrun_day(tmp_path / "first.db")
    second = _run_overrun_day(tmp_path / "second.db")
    assert dump_hash(first, _run_id(first)) == dump_hash(second, _run_id(second))
