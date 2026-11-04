from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal
from itertools import count

import pytest

import test_execution
from _helpers import register_conn
from test_execution import (
    ARTIFACT,
    DESTINATION_ADDRESS,
    SECOND_LOCATION_ADDRESS,
    SECOND_WEEKDAY,
    SEED,
    UNSUPPORTED_CATEGORY_ID,
    WEEKDAY,
    _appointment_event_rows,
    _book,
    _book_execution,
    _build_run,
    _customer_location_call,
    _shadow_rng,
    _step_until,
)
from trades_bench import (
    SPEC_VERSION,
    appointments,
    clock,
    demand,
    execution,
    handbook,
    migrations,
    origin_keys,
)
from trades_bench.config import load_run_config
from trades_bench.db import in_memory
from trades_bench.enums import EventKind
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.harness import workspace
from trades_bench.money import BASIS_POINTS_SCALE
from trades_bench.rng import Rng
from trades_bench.txn import privileged, privileged_request_id

FAR_ADDRESS_ID = 27
NEAR_ADDRESS_ID = 2
HVAC_SERVICE_DURATION = {"min": 45, "mode": 75, "max": 180}
MID_RUN_ID_BASE = 50_000_000


def _events_by_type(conn, appointment_id: int) -> dict[str, list[int]]:
    events: dict[str, list[int]] = {}
    for row in _appointment_event_rows(conn, appointment_id):
        events.setdefault(row["event_type"], []).append(int(row["occurred_at"]))
    return events


def _return_home_movements(conn, technician_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT departed_at FROM _technician_movements WHERE technician_id = ? "
        "AND movement_kind = 'return_home' ORDER BY departed_at",
        (technician_id,),
    ).fetchall()


def test_work_duration_draw_is_deterministic_and_keyed_on_the_job_origin():
    first = _shadow_rng()
    second = _shadow_rng()
    draws = {
        origin: execution.work_duration_minutes(first, origin, HVAC_SERVICE_DURATION)
        for origin in (f"t:duration:{index}" for index in range(200))
    }
    replayed = {
        origin: execution.work_duration_minutes(second, origin, HVAC_SERVICE_DURATION)
        for origin in reversed(list(draws))
    }
    assert replayed == draws
    assert all(45 <= minutes <= 180 for minutes in draws.values())
    assert len(set(draws.values())) > 20


def test_work_duration_draw_stays_inside_every_configured_triangle():
    rng = Rng(SEED, SPEC_VERSION)
    rng.enter_txn()
    durations = load_run_config().resolved["execution.duration_minutes_by_job_type"]
    for code, triangle in durations.items():
        drawn = [
            execution.work_duration_minutes(rng, f"t:{code}:{index}", triangle)
            for index in range(300)
        ]
        assert min(drawn) >= triangle["min"]
        assert max(drawn) <= triangle["max"]
        below_mode = sum(1 for minutes in drawn if minutes <= triangle["mode"])
        assert 0 < below_mode < len(drawn)


def test_completed_job_works_the_drawn_duration_of_its_true_job_type(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]
    expected = execution.work_duration_minutes(
        _shadow_rng(), origin_keys.of_job(conn, booked["job_id"]),
        cfg.resolved["execution.duration_minutes_by_job_type"]["hvac_service"],
    )
    assert str(controller.run()) == "completed"
    appt = conn.execute(
        "SELECT actual_start, actual_end FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["actual_end"] - appt["actual_start"] == expected * 60000


def _worked_minutes_of_pinned_job(directory, *, origin_key, service_date, technician_id, block):
    test_execution._id_seq, test_execution._request_seq = count(9500), count(1)
    conn, cfg, controller, _ = _build_run(directory, end_date=SECOND_WEEKDAY)
    booked = _book_execution(
        controller, cfg, technician_id=technician_id, service_date=service_date,
        first_block_code=block, origin_key=origin_key,
    )
    assert str(controller.run()) == "completed"
    appt = conn.execute(
        "SELECT actual_start, actual_end FROM _appointments WHERE id = ?",
        (booked["appointment_id"],),
    ).fetchone()
    return (int(appt["actual_end"]) - int(appt["actual_start"])) // 60000


def test_the_same_job_works_the_same_duration_whatever_day_window_or_technician(tmp_path):
    saved = test_execution._id_seq, test_execution._request_seq
    try:
        bookings = {
            "weekday_morning_first_technician": (WEEKDAY, 1, "B09_11"),
            "next_day_afternoon_second_technician": (SECOND_WEEKDAY, 2, "B13_15"),
        }
        worked = {}
        for label, (service_date, technician_id, block) in bookings.items():
            directory = tmp_path / label
            directory.mkdir()
            worked[label] = _worked_minutes_of_pinned_job(
                directory, origin_key="t:duration:pinned", service_date=service_date,
                technician_id=technician_id, block=block,
            )
        expected = execution.work_duration_minutes(
            _shadow_rng(), "t:duration:pinned",
            load_run_config().resolved["execution.duration_minutes_by_job_type"]["hvac_service"],
        )
        assert set(worked.values()) == {expected}
    finally:
        test_execution._id_seq, test_execution._request_seq = saved


def test_different_jobs_draw_their_durations_independently(tmp_path):
    saved = test_execution._id_seq, test_execution._request_seq
    try:
        origins = [f"t:duration:job:{index}" for index in range(40)]
        triangle = load_run_config().resolved["execution.duration_minutes_by_job_type"][
            "hvac_service"
        ]
        draws = [execution.work_duration_minutes(_shadow_rng(), origin, triangle) for origin in origins]
        assert len(set(draws)) > 10
        first, second = next(
            (left, right)
            for left in origins
            for right in origins
            if execution.work_duration_minutes(_shadow_rng(), left, triangle)
            != execution.work_duration_minutes(_shadow_rng(), right, triangle)
        )
        worked = []
        for origin in (first, second):
            directory = tmp_path / origin.replace(":", "_")
            directory.mkdir()
            worked.append(
                _worked_minutes_of_pinned_job(
                    directory, origin_key=origin, service_date=WEEKDAY,
                    technician_id=1, block="B09_11",
                )
            )
        assert worked == [
            execution.work_duration_minutes(_shadow_rng(), origin, triangle)
            for origin in (first, second)
        ]
        assert worked[0] != worked[1]
    finally:
        test_execution._id_seq, test_execution._request_seq = saved


def test_installation_books_a_single_arrival_window_late_in_the_day(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=2, service_date=WEEKDAY, first_block_code="B15_17",
        job_type_id=2,
    )
    assert booked["block_codes"] == ["B15_17"]
    holds = conn.execute(
        "SELECT COUNT(*) c FROM _technician_block_holds WHERE appointment_id = ?",
        (booked["appointment_id"],),
    ).fetchone()
    assert holds["c"] == 1


def test_one_technician_may_hold_several_jobs_in_the_same_window(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = [
        _book_execution(
            controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
            address_catalog_id=address,
        )
        for address in (
            DESTINATION_ADDRESS.address_id, SECOND_LOCATION_ADDRESS.address_id, FAR_ADDRESS_ID,
        )
    ]
    block_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    holds = conn.execute(
        "SELECT appointment_id FROM _technician_block_holds "
        "WHERE technician_id = 1 AND block_start = ? AND hold_kind = 'job' "
        "ORDER BY appointment_id",
        (block_start,),
    ).fetchall()
    assert [row["appointment_id"] for row in holds] == [b["appointment_id"] for b in booked]


def _insert_training_hold(conn, *, technician_id: int, block_code: str) -> None:
    start, end = appointments._block_window(WEEKDAY, block_code)
    cur = conn.execute(
        "INSERT INTO _non_job_appointments (technician_id, kind, name, start_at, end_at, "
        "block_code, status, created_at) VALUES (?, 'training', 'course', ?, ?, ?, "
        "'scheduled', 0)",
        (technician_id, start, end, block_code),
    )
    conn.execute(
        "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
        "appointment_id, non_job_appointment_id, created_at) VALUES (?, ?, 'training', "
        "NULL, ?, 0)",
        (technician_id, start, int(cur.lastrowid)),
    )


def test_a_training_hold_still_makes_the_window_unavailable(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _insert_training_hold(conn, technician_id=1, block_code="B11_13")
    now = clock.now(conn)
    location_id = _customer_location_call(
        conn, now, WEEKDAY, address_catalog_id=DESTINATION_ADDRESS.address_id, config=cfg,
    )
    request_id = privileged_request_id("book", 9_000_001)
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.book(
            ctx, cfg,
            call_id=location_id, customer_id=location_id, location_id=location_id,
            job_type_id=1, requested_service_name="HVAC service", priority="normal",
            summary="x", technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        ),
    )
    body = json.loads(response)
    assert body["ok"] is False
    assert body["error"]["details"]["reason"] == "overlap"
    _book(
        controller.services, cfg,
        call_id=location_id, customer_id=location_id, location_id=location_id,
        job_type_id=1, requested_service_name="HVAC service", priority="normal",
        summary="x", technician_id=2, service_date=WEEKDAY, first_block_code="B11_13",
    )


def test_reassigning_into_a_training_window_is_rejected(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
    )
    _insert_training_hold(conn, technician_id=2, block_code="B11_13")
    request_id = privileged_request_id("assign", 9_000_002)
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=booked["appointment_id"], technician_id=2,
            artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is False
    assert body["error"]["details"]["reason"] == "overlap"


def test_same_window_jobs_are_visited_nearest_first_without_returning_home(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    far = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=FAR_ADDRESS_ID,
    )
    near = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=NEAR_ADDRESS_ID,
    )
    home = execution.technician_home_point(conn, 1)

    def planned_from_home(appointment_id: int) -> int:
        location_id = execution._appointment_job_location_id(conn, appointment_id)[1]
        return execution.planned_travel_minutes(
            conn, ARTIFACT, cfg, origin=home,
            destination=execution.location_point(conn, location_id),
            service_date=WEEKDAY, minute_of_day=9 * 60,
        )

    assert planned_from_home(near["appointment_id"]) < planned_from_home(far["appointment_id"])
    assert str(controller.run()) == "completed"

    near_events = _events_by_type(conn, near["appointment_id"])
    far_events = _events_by_type(conn, far["appointment_id"])
    assert near_events["dispatched"][0] < far_events["dispatched"][0]
    assert far_events["dispatched"] == near_events["work_completed"]
    movements = _return_home_movements(conn, 1)
    assert [row["departed_at"] for row in movements] == far_events["work_completed"]


def test_a_gap_between_windows_drives_straight_to_the_next_job(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    morning = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )
    afternoon = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    assert str(controller.run()) == "completed"
    morning_events = _events_by_type(conn, morning["appointment_id"])
    afternoon_events = _events_by_type(conn, afternoon["appointment_id"])
    assert afternoon_events["dispatched"] == morning_events["work_completed"]
    assert afternoon_events["arrived"][0] < clock.phoenix_instant(2026, 10, 1, 15, 0)
    assert afternoon_events["work_started"][0] >= clock.phoenix_instant(2026, 10, 1, 14, 45)
    assert [row["departed_at"] for row in _return_home_movements(conn, 1)] == afternoon_events[
        "work_completed"
    ]


def _book_mid_run(controller, cfg, *, first_block_code: str) -> dict:
    location_id = _customer_location_call(
        controller.conn, clock.now(controller.conn), WEEKDAY,
        address_catalog_id=NEAR_ADDRESS_ID, config=cfg,
    )
    return _book(
        controller.services, cfg,
        call_id=location_id, customer_id=location_id, location_id=location_id,
        job_type_id=1, requested_service_name="HVAC service", priority="normal",
        summary="x", technician_id=1, service_date=WEEKDAY,
        first_block_code=first_block_code, artifact=ARTIFACT,
    )


def test_an_earlier_window_booked_while_waiting_pulls_the_technician_away(tmp_path, monkeypatch):
    conn, cfg, controller, _ = _build_run(tmp_path)
    morning = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )
    afternoon = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    afternoon_id = afternoon["appointment_id"]
    _step_until(
        controller,
        lambda: "arrived" in _events_by_type(conn, afternoon_id),
    )
    assert clock.now(conn) < clock.phoenix_instant(2026, 10, 1, 11, 0)
    monkeypatch.setattr(test_execution, "_id_seq", count(MID_RUN_ID_BASE))
    midday = _book_mid_run(controller, cfg, first_block_code="B11_13")
    midday_events = _events_by_type(conn, midday["appointment_id"])
    assert midday_events["dispatched"] == [clock.now(conn)]

    assert str(controller.run()) == "completed"
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute(
            "SELECT id, status FROM _appointments WHERE id IN (?, ?, ?)",
            (morning["appointment_id"], midday["appointment_id"], afternoon_id),
        )
    }
    assert set(statuses.values()) == {"completed"}
    afternoon_events = _events_by_type(conn, afternoon_id)
    midday_events = _events_by_type(conn, midday["appointment_id"])
    assert len(afternoon_events["dispatched"]) == 2
    assert afternoon_events["dispatched"][1] == midday_events["work_completed"][0]


def test_an_earlier_window_booked_mid_trip_is_served_before_the_later_stop(tmp_path, monkeypatch):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )
    afternoon = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=FAR_ADDRESS_ID,
    )
    afternoon_id = afternoon["appointment_id"]
    _step_until(controller, lambda: "dispatched" in _events_by_type(conn, afternoon_id))
    assert "arrived" not in _events_by_type(conn, afternoon_id)
    monkeypatch.setattr(test_execution, "_id_seq", count(MID_RUN_ID_BASE))
    midday = _book_mid_run(controller, cfg, first_block_code="B11_13")
    assert str(controller.run()) == "completed"
    midday_events = _events_by_type(conn, midday["appointment_id"])
    afternoon_events = _events_by_type(conn, afternoon_id)
    assert midday_events["work_completed"][0] < afternoon_events["work_started"][0]
    assert len(afternoon_events["arrived"]) == 1
    assert afternoon_events["dispatched"][1] == midday_events["work_completed"][0]


def _repeat_state(conn, job_id: int) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT r.propensity_micros, r.last_outcome FROM _customer_repeat_state r "
        "JOIN _jobs j ON j.customer_id = r.customer_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    return int(row["propensity_micros"]), row["last_outcome"]


def _job_ledger_rows(conn, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT entry_type, amount_cents FROM _ledger_entries WHERE job_id = ?", (job_id,)
    ).fetchall()


def test_a_missed_appointment_loses_the_customer_and_posts_no_payment(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 260}
    )
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    assert str(controller.run()) == "completed"

    statuses = [
        conn.execute("SELECT status FROM _appointments WHERE id = ?", (b["appointment_id"],))
        .fetchone()["status"]
        for b in (first, second)
    ]
    assert statuses == ["completed", "no_show"]
    assert _job_ledger_rows(conn, second["job_id"]) == []
    assert _repeat_state(conn, second["job_id"]) == (0, "no_show")


def test_a_partial_no_show_multiplier_scales_the_repeat_propensity(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        **{
            "appointments.min_actual_duration_minutes": 260,
            "demand.no_show_repeat_propensity_multiplier": 0.5,
        },
    )
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    before, _ = _repeat_state(conn, second["job_id"])
    assert str(controller.run()) == "completed"
    assert _repeat_state(conn, second["job_id"]) == (before // 2, "no_show")


def test_a_failed_visit_loses_the_customer_like_a_missed_appointment(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    failed = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    before, _ = _repeat_state(conn, failed["job_id"])
    assert before > 0
    assert str(controller.run()) == "completed"

    assert conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (failed["appointment_id"],)
    ).fetchone()["status"] == "failed"
    assert _repeat_state(conn, failed["job_id"]) == (0, "no_show")


def test_work_never_starts_before_the_arrival_window_opens(tmp_path):
    early_origin = "t:early:28"
    assert execution.start_variation_minutes(_shadow_rng(), early_origin, WEEKDAY) < 0
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
        origin_key=test_execution.EARLY_FINISH_ORIGIN,
    )
    later = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        origin_key=early_origin,
    )
    assert str(controller.run()) == "completed"

    events = _events_by_type(conn, later["appointment_id"])
    window_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    assert events["arrived"][0] < window_start
    assert events["work_started"] == [window_start]


def _migrated() -> sqlite3.Connection:
    conn = in_memory()
    register_conn(conn)
    migrations.migrate(conn)
    return conn


def test_migration_lets_job_holds_share_a_window_but_not_non_job_holds():
    conn = _migrated()
    pk_columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(_technician_block_holds)")
        if row["pk"]
    ]
    assert pk_columns == []
    indexes = {
        row["name"]: row["unique"]
        for row in conn.execute("PRAGMA index_list(_technician_block_holds)")
    }
    assert indexes["_technician_block_holds_job"] == 1
    assert indexes["_technician_block_holds_non_job"] == 1
    scheduled_blocks = {
        row["code"]: row["scheduled_blocks"]
        for row in conn.execute("SELECT code, scheduled_blocks FROM _job_types")
    }
    assert set(scheduled_blocks.values()) == {1}
    ledger_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = '_ledger_entries'"
    ).fetchone()["sql"]
    assert "'missed_appointment_payment'" not in ledger_sql
    triggers = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = '_ledger_entries'"
        )
    }
    assert triggers == {"trg_ledger_entries_no_update", "trg_ledger_entries_no_delete"}


def test_handbook_is_rendered_from_the_run_config_and_is_deterministic():
    overrides = {
        "finance.daily_operating_overhead": 123_456,
        "execution.duration_minutes_by_job_type": {
            "hvac_service": {"min": 40, "mode": 80, "max": 240},
            "hvac_installation": {"min": 150, "mode": 330, "max": 450},
            "plumbing": {"min": 42, "mode": 76, "max": 181},
            "electrical": {"min": 43, "mode": 91, "max": 211},
        },
        "payroll.overtime_cap_basis_points": 1500,
    }
    cfg = load_run_config(scenario_json=json.dumps({"keys": overrides}))
    text = handbook.render_handbook(cfg)
    assert text == handbook.render_handbook(cfg)
    for fragment in (
        "$1,234.56",
        "- HVAC service: 40 min to 4 h, typically 80 min.",
        "- HVAC installation: 2½ h to 7½ h, typically 5½ h.",
        "- Plumbing: 42 min to 181 min, typically 76 min.",
        "capped at 15% of the job's invoice",
        "17:00-19:00",
    ):
        assert fragment in text
    assert text != handbook.render_handbook(load_run_config())


def test_handbook_does_not_disclose_hidden_rates():
    cfg = load_run_config()
    text = handbook.render_handbook(cfg).lower()
    for word in ("probability", "acceptance", "cancellation", "weather", "propensity", "demand"):
        assert word not in text
    for key in (
        "acceptance.base_probability_by_urgency",
        "cancellations.base_probability_by_segment",
    ):
        for value in cfg.resolved[key].values():
            shown = handbook._percent(int(Decimal(value) * BASIS_POINTS_SCALE))
            assert re.search(rf"(?<![\d.]){re.escape(shown)}", text) is None
    for key in ("invoices.off_hours_premium_basis_points", "payroll.off_hours_share_basis_points"):
        shown = handbook._percent(cfg.get_int(key))
        assert re.search(rf"(?<![\d.]){re.escape(shown)}", text) is None


def test_workspace_creation_writes_the_handbook_next_to_the_api_reference(tmp_path):
    cfg = load_run_config()
    root = workspace.create(tmp_path / "ws", cfg)
    written = (workspace.docs_path(root) / handbook.HANDBOOK_FILENAME).read_text(
        encoding="utf-8"
    )
    assert written == handbook.render_handbook(cfg)


def test_booking_an_unknown_block_code_is_a_validation_error():
    with pytest.raises(EngineError) as raised:
        appointments._reservation_blocks("B19_21")
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_a_job_booked_before_the_drive_home_starts_is_driven_to_directly(tmp_path, monkeypatch):
    conn, cfg, controller, _ = _build_run(tmp_path)
    morning = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )
    later = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=FAR_ADDRESS_ID,
    )
    later_id = later["appointment_id"]
    _step_until(controller, lambda: "dispatched" in _events_by_type(conn, later_id))
    request_id = privileged_request_id("assign", 9_000_003)
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=later_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    assert json.loads(response)["ok"] is True
    return_home = conn.execute(
        "SELECT scheduled_at_ms FROM _events WHERE kind = 'technician_return_home' "
        "AND status = 'pending' AND entity_id = 1"
    ).fetchone()
    assert return_home is not None

    monkeypatch.setattr(test_execution, "_id_seq", count(MID_RUN_ID_BASE))
    afternoon = _book_mid_run(controller, cfg, first_block_code="B13_15")
    assert str(controller.run()) == "completed"

    afternoon_events = _events_by_type(conn, afternoon["appointment_id"])
    assert afternoon_events["dispatched"] == [int(return_home["scheduled_at_ms"])]
    assert [row["departed_at"] for row in _return_home_movements(conn, 1)] == afternoon_events[
        "work_completed"
    ]
    assert conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (morning["appointment_id"],)
    ).fetchone()["status"] == "completed"


def test_handbook_percent_check_can_detect_a_disclosed_rate():
    shown = handbook._percent(9000)
    assert shown == "90%"
    assert re.search(rf"(?<![\d.]){re.escape(shown)}", "accepts 90% of offers") is not None
    assert re.search(rf"(?<![\d.]){re.escape(handbook._percent(500))}", "share 25%") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"appointments.min_actual_duration_minutes": 822},
        {
            "execution.duration_minutes_by_job_type": {
                "hvac_service": {"min": 45, "mode": 75, "max": 180},
                "hvac_installation": {"min": 300, "mode": 420, "max": 700},
                "plumbing": {"min": 45, "mode": 75, "max": 180},
                "electrical": {"min": 45, "mode": 90, "max": 210},
            }
        },
        {"execution.failed_visit_minutes": 700},
    ],
)
def test_config_rejects_work_that_could_run_into_the_next_opening(overrides):
    with pytest.raises(EngineError) as raised:
        load_run_config(scenario_json=json.dumps({"keys": overrides}))
    assert raised.value.code is ErrorCode.CONFIG_VALUE_INVALID
    assert "next day's opening" in raised.value.message


_CRAWLING_EVENING = {
    "weekday": {
        "night": 62, "morning_peak": 38, "midday": 52,
        "evening_peak": 34, "evening": 0.22, "night_late": 60,
    },
    "weekend": {
        "night": 60, "morning_peak": 52, "midday": 55,
        "evening_peak": 50, "evening": 52, "night_late": 60,
    },
}


def test_next_day_first_trip_waits_for_a_technician_still_driving_home(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, end_date=SECOND_WEEKDAY,
        **{"travel.speed_by_time_band": _CRAWLING_EVENING, "travel.noise.max_multiplier": 1},
    )
    evening = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
    )
    morning = _book_execution(
        controller, cfg, technician_id=1, service_date=SECOND_WEEKDAY,
        first_block_code="B07_09",
    )
    assert str(controller.run()) == "completed"

    home_arrival = conn.execute(
        "SELECT arrives_at FROM _technician_movements WHERE technician_id = 1 "
        "AND service_date = ? AND movement_kind = 'return_home'",
        (WEEKDAY,),
    ).fetchone()
    assert home_arrival["arrives_at"] > clock.phoenix_instant(2026, 10, 2, 6, 45)
    assert _events_by_type(conn, evening["appointment_id"])["work_completed"]
    morning_events = _events_by_type(conn, morning["appointment_id"])
    assert morning_events["dispatched"][0] >= home_arrival["arrives_at"]
    assert conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (morning["appointment_id"],)
    ).fetchone()["status"] == "completed"


def test_a_stale_home_arrival_leaves_a_newer_trip_untouched(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    _step_until(controller, lambda: "dispatched" in _events_by_type(conn, booked["appointment_id"]))
    before = tuple(conn.execute(
        "SELECT * FROM _technician_locations WHERE technician_id = 1"
    ).fetchone())
    handler = execution.build_handlers(cfg, ARTIFACT)[EventKind.TECHNICIAN_HOME_ARRIVAL]
    stale = {
        "scheduled_at_ms": clock.now(conn),
        "payload": json.dumps(
            {"service_date": WEEKDAY, "technician_id": 1, "departed_at": 1}
        ),
    }
    request_id = privileged_request_id("stale_home", 9_000_004)
    privileged(controller.services, request_id, lambda ctx: handler(ctx, stale))
    after = tuple(conn.execute(
        "SELECT * FROM _technician_locations WHERE technician_id = 1"
    ).fetchone())
    assert after == before
    assert str(controller.run()) == "completed"


def test_repeat_dispatches_to_one_appointment_draw_fresh_travel_noise(tmp_path, monkeypatch):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )
    afternoon = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=FAR_ADDRESS_ID,
    )
    afternoon_id = afternoon["appointment_id"]
    assert execution.inbound_trip_index(conn, afternoon_id) == 0
    _step_until(controller, lambda: "dispatched" in _events_by_type(conn, afternoon_id))
    assert execution.inbound_trip_index(conn, afternoon_id) == 1
    monkeypatch.setattr(test_execution, "_id_seq", count(MID_RUN_ID_BASE))
    _book_mid_run(controller, cfg, first_block_code="B11_13")
    assert str(controller.run()) == "completed"
    assert len(_events_by_type(conn, afternoon_id)["dispatched"]) == 2
    scope = origin_keys.scoped(origin_keys.of_appointment(conn, afternoon_id), WEEKDAY)
    draw_indexes = [
        row["draw_index"]
        for row in conn.execute(
            "SELECT draw_index FROM _random_audit WHERE event_kind = 'travel_noise' "
            "AND scope = ? ORDER BY seq",
            (scope,),
        )
    ]
    assert draw_indexes == [0, 1]


def test_a_customer_lost_to_a_no_show_stays_at_zero_after_later_consequences(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    now = clock.now(conn)
    location_id = _customer_location_call(
        conn, now, WEEKDAY, address_catalog_id=DESTINATION_ADDRESS.address_id, config=cfg,
    )
    conn.execute(
        "UPDATE _customer_repeat_state SET propensity_micros = 0, last_outcome = 'no_show' "
        "WHERE customer_id = ?",
        (location_id,),
    )
    demand.apply_no_show_consequence(conn, cfg, now_ms=now, customer_id=location_id)
    assert conn.execute(
        "SELECT propensity_micros FROM _customer_repeat_state WHERE customer_id = ?",
        (location_id,),
    ).fetchone()["propensity_micros"] == 0
