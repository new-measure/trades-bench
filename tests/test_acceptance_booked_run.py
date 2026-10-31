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
from trades_bench import appointments, booking, calls, clock, constants, crm, geo_reader
from trades_bench._dump import dump_hash
from trades_bench.adapter import AdapterResponse
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import RunStatus
from trades_bench.run_init import initialize_run, open_run_db

SEED = 5
DAY_ONE = "2026-10-01"
DAY_TWO = "2026-10-02"
DAY_THREE = "2026-10-03"
STARTING_CASH = 4_200_000
FINAL_CASH = 3_969_792
OVERTIME_ROWS = (
    (-2_083, DAY_ONE, 1),
    (-550, DAY_ONE, 2),
    (-1_280, DAY_THREE, 3),
)
MIN_TRAVEL_MINUTES = 20
SINGLE_BLOCK_HOURS = 2
OVERRUN_DURATION_MINUTES = 600
RESUME_SAMPLE_COUNT = 16

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
COMPLETED_CHAIN = ["dispatched", "arrived", "work_started", "work_completed"]

_JOB_TYPE_BY_SERVICE = {
    "hvac service": 1,
    "hvac installation": 2,
    "plumbing": 3,
    "electrical": 4,
}

BOOKED_RUN_DEMAND = {
    "demand.base_new_caller_rate_per_day": 10,
    "demand.segment_weights": {"residential": 0.62, "commercial": 0.23, "apartment": 0.15},
    "demand.category_weights": {
        "residential": {
            "HVAC service": 0.44,
            "Plumbing": 0.33,
            "Electrical": 0.16,
            "HVAC installation": 0.07,
            "Appliance repair": 0.35,
            "Garage-door repair": 0.15,
            "Roofing": 0.10,
            "Pool service": 0.15,
            "Pest control": 0.15,
            "Landscaping": 0.10,
        },
        "commercial": {
            "HVAC service": 0.52,
            "Plumbing": 0.27,
            "Electrical": 0.17,
            "HVAC installation": 0.04,
            "Appliance repair": 0.35,
            "Garage-door repair": 0.15,
            "Roofing": 0.10,
            "Pool service": 0.15,
            "Pest control": 0.15,
            "Landscaping": 0.10,
        },
        "apartment": {
            "HVAC service": 0.40,
            "Plumbing": 0.38,
            "Electrical": 0.15,
            "HVAC installation": 0.07,
            "Appliance repair": 0.35,
            "Garage-door repair": 0.15,
            "Roofing": 0.10,
            "Pool service": 0.15,
            "Pest control": 0.15,
            "Landscaping": 0.10,
        },
    },
    "demand.unsupported_category_share": 0.12,
    "demand.day_of_week_multipliers": [1.45, 1.34, 1.28, 1.33, 1.20, 0.29, 0.11],
}

BOOKED_RUN_ECONOMICS = {
    "finance.daily_operating_overhead": 220_000,
    "finance.starting_cash": STARTING_CASH,
    "payroll.off_hours_bonus_per_job": 125_000,
}


@dataclass(frozen=True)
class BookingOrder:

    key: str
    technician_id: int
    job_type_id: int
    service: str
    service_date: str
    first_block_code: str


BOOKED_RUN_ORDERS: dict[str, tuple[BookingOrder, ...]] = {
    DAY_ONE: (
        BookingOrder("chain-first", 1, 1, "HVAC Service", DAY_ONE, "B13_15"),
        BookingOrder("chain-second", 1, 1, "HVAC Service", DAY_ONE, "B15_17"),
        BookingOrder("install", 2, 2, "HVAC Installation", DAY_ONE, "B13_15"),
        BookingOrder("after-hours", 4, 4, "Electrical", DAY_ONE, "B17_19"),
        BookingOrder("next-morning-hvac", 1, 1, "HVAC Service", DAY_TWO, "B07_09"),
        BookingOrder("next-morning-plumbing", 3, 3, "Plumbing", DAY_TWO, "B07_09"),
    ),
    DAY_TWO: (
        BookingOrder("midday-idle", 2, 1, "HVAC Service", DAY_TWO, "B13_15"),
    ),
    DAY_THREE: (
        BookingOrder("weekend", 3, 3, "Plumbing", DAY_THREE, "B13_15"),
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
        BookingOrder("overrun", 1, 1, "HVAC Service", DAY_ONE, "B11_13"),
        BookingOrder("starved", 1, 1, "HVAC Service", DAY_ONE, "B15_17"),
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
    ("work_started", "2026-10-02T07:05:00-07:00", 320),
    ("call_invocation", "2026-10-02T07:15:00-07:00", 910),
    ("call_invocation", "2026-10-02T08:16:00-07:00", 910),
    ("block_lock", "2026-10-02T09:00:00-07:00", 200),
    ("company_no_show", "2026-10-02T09:00:00-07:00", 335),
    ("call_invocation", "2026-10-02T09:08:00-07:00", 910),
    ("work_completed", "2026-10-02T09:11:00-07:00", 330),
    ("technician_return_home", "2026-10-02T09:11:00-07:00", 350),
    ("call_invocation", "2026-10-02T09:13:00-07:00", 910),
    ("technician_home_arrival", "2026-10-02T09:31:00-07:00", 360),
    ("call_invocation", "2026-10-02T09:34:00-07:00", 910),
    ("call_invocation", "2026-10-02T10:38:00-07:00", 910),
    ("call_invocation", "2026-10-02T10:49:00-07:00", 910),
    ("block_lock", "2026-10-02T11:00:00-07:00", 200),
    ("call_invocation", "2026-10-02T11:48:00-07:00", 910),
    ("appointment_dispatch", "2026-10-02T12:40:00-07:00", 300),
    ("call_invocation", "2026-10-02T12:40:00-07:00", 910),
    ("block_lock", "2026-10-02T13:00:00-07:00", 200),
    ("appointment_arrival", "2026-10-02T13:00:00-07:00", 310),
    ("work_started", "2026-10-02T13:00:00-07:00", 320),
    ("call_invocation", "2026-10-02T14:14:00-07:00", 910),
    ("block_lock", "2026-10-02T15:00:00-07:00", 200),
    ("company_no_show", "2026-10-02T15:00:00-07:00", 335),
    ("work_completed", "2026-10-02T15:04:00-07:00", 330),
    ("technician_return_home", "2026-10-02T15:04:00-07:00", 350),
    ("call_invocation", "2026-10-02T15:09:00-07:00", 910),
    ("technician_home_arrival", "2026-10-02T15:24:00-07:00", 360),
    ("call_invocation", "2026-10-02T16:51:00-07:00", 910),
    ("block_lock", "2026-10-02T17:00:00-07:00", 200),
    ("call_invocation", "2026-10-02T17:10:00-07:00", 910),
    ("call_invocation", "2026-10-02T17:50:00-07:00", 910),
    ("closing_invocation", "2026-10-02T19:00:00-07:00", 920),
    ("daily_settlement", "2026-10-02T19:00:00-07:00", 950),
    ("checkpoint", "2026-10-02T19:00:00-07:00", 960),
]


class ScriptedBookingAdapter(ScriptedNoOpAdapter):

    def __init__(self, orders_by_prompt_date: dict[str, tuple[BookingOrder, ...]]) -> None:
        super().__init__()
        self.controller = None
        self.orders = [order for day in orders_by_prompt_date.values() for order in day]

    def send(self, prompt):
        if prompt.kind.value == "call":
            if "appointment_id" in prompt.payload:
                self._acknowledge_cancellation(int(prompt.payload["call_id"]))
            else:
                order = self._matching_order(prompt)
                if order is None:
                    self._reject(int(prompt.payload["call_id"]))
                else:
                    self._book(prompt, order)
        self.prompts.append(prompt)
        return AdapterResponse(text="", terminal_action_taken=True)

    def _matching_order(self, prompt):
        prompt_date = clock.service_date_for_ms(prompt.ts_ms)
        service = str(prompt.payload["requested_service"]).casefold()
        for order in self.orders:
            if self._already_booked(order):
                continue
            if order.service_date >= prompt_date and order.service.casefold() == service:
                self.orders.remove(order)
                return order
        return None

    def _already_booked(self, order):
        hour = int(order.first_block_code[1:3])
        row = self.controller.conn.execute(
            "SELECT 1 FROM _appointments a JOIN _appointment_assignments aa "
            "ON aa.appointment_id = a.id WHERE a.scheduled_start = ? "
            "AND aa.technician_id = ? LIMIT 1",
            (_ms(order.service_date, hour, 0), order.technician_id),
        ).fetchone()
        return row is not None

    def _result(self, response):
        body = json.loads(response)
        assert body["ok"], body
        return body["result"]

    def _book(self, prompt, order: BookingOrder) -> None:
        controller = self.controller
        call_id = int(prompt.payload["call_id"])
        customer_id = prompt.payload.get("customer_id")
        location_id = prompt.payload.get("location_id")
        if customer_id is None:
            customer_id = self._result(
                crm.create_customer_command(
                    controller.services, f"script-{call_id}-customer", controller.config,
                    call_id=call_id, name=str(prompt.payload["caller"]),
                    segment=str(prompt.payload["customer_type"]),
                    contacts=[{"contact_type": "phone", "value": prompt.payload["phone"]}],
                )
            )["customer_id"]
        if location_id is None:
            location_id = self._result(
                crm.create_location_command(
                    controller.services, f"script-{call_id}-location", controller.config,
                    call_id=call_id, customer_id=int(customer_id),
                    address=str(prompt.payload["address"]), artifact=controller.artifact,
                )
            )["location_id"]
        result = self._result(
            booking.book_command(
                controller.services, f"script-{call_id}-book", controller.config,
                call_id=call_id, customer_id=int(customer_id), location_id=int(location_id),
                job_type_id=_JOB_TYPE_BY_SERVICE[order.service.casefold()],
                service_date=order.service_date,
                block_start=order.first_block_code[1:3] + ":00",
                technician_id=order.technician_id, artifact=controller.artifact,
            )
        )
        assert result["outcome"] == "accepted", result

    def _reject(self, call_id):
        controller = self.controller
        self._result(calls.reject_command(
            controller.services, f"script-{call_id}-reject", controller.config, call_id=call_id
        ))

    def _acknowledge_cancellation(self, call_id):
        controller = self.controller
        self._result(appointments.acknowledge_cancellation_command(
            controller.services, f"script-{call_id}-cancel", controller.config,
            call_id=call_id, artifact=controller.artifact,
        ))


def _config(**overrides):
    keys = {
        **BOOKED_RUN_ECONOMICS,
        "acceptance.base_probability_by_urgency": {"standard": 1, "urgent": 1},
        "acceptance.floor_probability_by_urgency": {"standard": 1, "urgent": 1},
        "acceptance.lead_time_decay_by_urgency": {"standard": 0, "urgent": 0},
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": DAY_THREE,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


BOOKED_RUN_CANCELLATIONS = {
    "cancellations.base_probability_by_segment": {
        "residential": 0.0, "commercial": 0.0, "apartment": 1.0,
    },
}


def _booked_config():
    return _config(
        **BOOKED_RUN_DEMAND, **BOOKED_RUN_CANCELLATIONS,
        **{"travel.min_travel_minutes": MIN_TRAVEL_MINUTES},
    )


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
    order = ORDERS_BY_KEY.get(key)
    if order is None:
        order = next(order for orders in NO_SHOW_ORDERS.values() for order in orders if order.key == key)
    hour = int(order.first_block_code[1:3])
    return int(
        conn.execute(
            "SELECT a.id AS appointment_id FROM _appointments a "
            "JOIN _appointment_assignments aa ON aa.appointment_id = a.id "
            "WHERE a.scheduled_start = ? AND aa.technician_id = ? ORDER BY aa.id LIMIT 1",
            (_ms(order.service_date, hour, 0), order.technician_id),
        ).fetchone()["appointment_id"]
    )


TERMINAL_EXECUTION_EVENT_TYPES = ("work_completed", "work_failed", "no_show")


def _latest_terminal_execution_at(conn, service_date):
    placeholders = ", ".join("?" for _ in TERMINAL_EXECUTION_EVENT_TYPES)
    row = conn.execute(
        f"SELECT MAX(occurred_at) AS latest FROM _appointment_events "
        f"WHERE service_date = ? AND event_type IN ({placeholders})",
        (service_date, *TERMINAL_EXECUTION_EVENT_TYPES),
    ).fetchone()
    return row["latest"]


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
        "ORDER BY scheduled_at_ms, priority, order_key, event_id",
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
    assert _event_chain(conn, _appointment_id(conn, "next-morning-hvac")) == ["cancelled"]
    for key in ("next-morning-plumbing",):
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
    after_hours_job_pay = SINGLE_BLOCK_HOURS * wages[after_hours_technician] + bonus
    weekend_technician = ORDERS_BY_KEY["weekend"].technician_id
    weekend_job_pay = SINGLE_BLOCK_HOURS * wages[weekend_technician] + bonus
    invoice_totals = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) revenue, "
        "COALESCE(SUM(material_cost_cents), 0) materials FROM _invoices"
    ).fetchone()
    expected = (
        starting
        + int(invoice_totals["revenue"])
        - int(invoice_totals["materials"])
        - weekday_count * daily_payroll
        - day_count * overhead
        - after_hours_job_pay
        - weekend_job_pay
        + sum(amount for amount, _date, _technician in OVERTIME_ROWS)
    )
    assert expected == FINAL_CASH
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM _ledger_entries"
    ).fetchone()["s"]
    assert balance == expected
    off_hours_rows = conn.execute(
        "SELECT entry_type, amount_cents, service_date, technician_id FROM _ledger_entries "
        "WHERE entry_type IN ('payroll_off_hours', 'payroll_off_hours_bonus') "
        "AND description NOT LIKE 'overtime wages%' ORDER BY id"
    ).fetchall()
    overtime_rows = conn.execute(
        "SELECT amount_cents, service_date, technician_id FROM _ledger_entries "
        "WHERE description LIKE 'overtime wages%' ORDER BY id"
    ).fetchall()
    assert tuple(tuple(row) for row in overtime_rows) == OVERTIME_ROWS
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


def test_booked_appointments_complete_or_are_acknowledged_as_cancelled(booked_run):
    conn = booked_run["conn"]
    rows = conn.execute(
        "SELECT a.id, a.status, a.actual_start, a.actual_end, a.lateness_minutes, "
        "j.summary FROM _appointments a JOIN _jobs j ON j.id = a.job_id ORDER BY a.id"
    ).fetchall()
    assert [row["summary"] for row in rows] == [
        "HVAC service", "HVAC service", "HVAC installation", "HVAC service",
        "Plumbing", "Plumbing", "HVAC service", "Electrical",
    ]
    for row in rows:
        if row["status"] == "cancelled":
            assert row["actual_start"] is None
            assert row["actual_end"] is None
            assert _event_chain(conn, int(row["id"])) == ["cancelled"]
        else:
            assert row["status"] == "completed"
            assert row["actual_start"] is not None
            assert row["actual_end"] is not None
            assert int(row["actual_end"]) > int(row["actual_start"])
            assert int(row["lateness_minutes"]) >= 0
            assert _event_chain(conn, int(row["id"])) == COMPLETED_CHAIN
    assert [row["status"] for row in rows] == [
        "completed", "completed", "completed", "cancelled",
        "completed", "completed", "completed", "completed",
    ]
    assert [row["lateness_minutes"] for row in rows] == [0, 13, 0, None, 0, 0, 0, 0]
    guard_rows = conn.execute(
        "SELECT entity_id, status FROM _events WHERE kind = 'company_no_show' ORDER BY event_id"
    ).fetchall()
    appointment_statuses = {int(row["id"]): row["status"] for row in rows}
    assert [row["status"] for row in guard_rows] == [
        "cancelled" if appointment_statuses[int(row["entity_id"])] == "cancelled" else "applied"
        for row in guard_rows
    ]
    assert sorted(int(row["entity_id"]) for row in guard_rows) == sorted(appointment_statuses)
    assert conn.execute("SELECT COUNT(*) c FROM _notifications").fetchone()["c"] == 1
    days = conn.execute("SELECT * FROM _day_state ORDER BY service_date").fetchall()
    assert [row["service_date"] for row in days] == [DAY_ONE, DAY_TWO, DAY_THREE]
    for day in days:
        assert day["closing_at"] is not None
        assert day["settlement_status"] == "settled"
        assert day["unresolved_execution_count"] == 0
        latest_terminal = _latest_terminal_execution_at(conn, day["service_date"])
        assert day["last_execution_completed_at"] == latest_terminal
        expected_closing = _ms(day["service_date"], 19, 0)
        if latest_terminal is not None:
            expected_closing = max(expected_closing, int(latest_terminal))
        assert int(day["closing_at"]) == expected_closing
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
        **BOOKED_RUN_DEMAND,
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
    assert int(day["last_execution_completed_at"]) == int(overrun["actual_end"])
    assert int(day["last_execution_completed_at"]) == int(
        _latest_terminal_execution_at(conn, DAY_ONE)
    )
    pending = conn.execute(
        "SELECT kind FROM _events WHERE status = 'pending'"
    ).fetchall()
    assert [row["kind"] for row in pending] == ["technician_home_arrival"]


def test_the_overrun_day_is_twin_stable(tmp_path):
    first = _run_overrun_day(tmp_path / "first.db")
    second = _run_overrun_day(tmp_path / "second.db")
    assert dump_hash(first, _run_id(first)) == dump_hash(second, _run_id(second))
