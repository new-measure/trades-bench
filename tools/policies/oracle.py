from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from policies.base import DEFAULT_HORIZON_DAYS
from policies.runner import (
    DEFAULT_SCENARIO,
    REPORT_FILENAME,
    PolicyRunPaths,
    PolicyRunResult,
    build_report,
    report_bytes,
)
from trades_bench import (
    appointments,
    booking,
    calls,
    clock,
    crm,
    execution,
    overtime,
    run_init,
)
from trades_bench.adapter import AdapterPrompt, AdapterResponse
from trades_bench.appointments import BLOCK_CODES, BookingPlan
from trades_bench.config import RunConfig
from trades_bench.constants import (
    BLOCK_GRID_MINUTES,
    TIMING_VARIATION_HIGH,
    TIMING_VARIATION_LOW,
)
from trades_bench.controller import Controller
from trades_bench.enums import DayType, PromptKind
from trades_bench.errors import EngineError

POLICY_NAME = "oracle"
POLICY_VERSION = "1.1.0"
TRACK = "oracle"

BASELINE_GREEDY = "greedy"
BASELINE_DATE_MAJOR = "date_major"

RUN_END_DATE_KEY = "run.end_date"

BASIS_POINTS = 10_000
MINUTES_PER_HOUR = 60
MILLISECONDS_PER_DAY = 86_400_000

BLOCK_HOURS = (BLOCK_GRID_MINUTES[0][1] - BLOCK_GRID_MINUTES[0][0]) // MINUTES_PER_HOUR
OFF_HOURS_BLOCK_CODE = "B17_19"

BLOCK_STARTS: tuple[str, ...] = tuple(
    f"{start // 60:02d}:{start % 60:02d}" for start, _end in BLOCK_GRID_MINUTES
)

CALL_SQL = (
    "SELECT c.id AS call_id, c.arrived_at AS arrived_at, c.call_kind AS call_kind, "
    "c.urgent AS urgent, c.segment AS segment, c.customer_id AS customer_id, "
    "c.location_id AS location_id, c.caller_name AS caller_name, "
    "c.caller_phone AS caller_phone, h.requested_category_id AS requested_category_id "
    "FROM _calls c JOIN _call_hidden h ON h.call_id = c.id WHERE c.id = ?"
)
TRUE_JOB_TYPE_SQL = (
    "SELECT jt.id AS id, jt.code AS code, jt.scheduled_blocks AS scheduled_blocks, "
    "jt.required_skill_code AS required_skill_code "
    "FROM _call_hidden h JOIN _service_catalog sc ON sc.id = h.requested_category_id "
    "JOIN _job_types jt ON jt.id = sc.supported_job_type_id WHERE h.call_id = ?"
)
ACTIVE_TECHNICIANS_SQL = (
    "SELECT id, hourly_wage_cents FROM _technicians "
    "WHERE employment_status = 'active' ORDER BY id"
)
SKILL_HOLDER_SQL = (
    "SELECT 1 FROM _technician_skills "
    "WHERE technician_id = ? AND skill_code = ? AND active = 1 LIMIT 1"
)
ACCEPTANCE_PARAMETERS_SQL = (
    "SELECT base_micros, decay_micros, floor_micros FROM _acceptance_parameters "
    "WHERE urgency_class = ?"
)
CUSTOMER_SEGMENT_SQL = "SELECT segment FROM _customers WHERE id = ?"


class OracleError(RuntimeError):
    pass


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _cents(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ReadContext:

    conn: sqlite3.Connection
    now_ms: int

    def now(self) -> int:
        return self.now_ms


@dataclass(frozen=True)
class Technician:

    technician_id: int
    hourly_wage_cents: int


@dataclass(frozen=True)
class JobType:

    job_type_id: int
    code: str
    scheduled_blocks: int
    required_skill_code: str | None


@dataclass(frozen=True)
class Candidate:

    service_date: str
    block_start: str
    technician_id: int
    score_cents: int


@dataclass(frozen=True)
class Pricing:

    expected_margin_cents: Decimal
    acceptance_base: Decimal
    acceptance_decay: Decimal
    acceptance_floor: Decimal
    cancellation_base: Decimal
    cancellation_urgent_multiplier: Decimal
    cancellation_lead_days_coefficient: Decimal
    off_hours_bonus_cents: int


def _row(conn: sqlite3.Connection, statement: str, *params: object) -> sqlite3.Row | None:
    return conn.execute(statement, params).fetchone()


def read_call(conn: sqlite3.Connection, call_id: int) -> sqlite3.Row:
    row = _row(conn, CALL_SQL, call_id)
    if row is None:
        raise OracleError(f"call {call_id} carries no arrived-call record")
    return row


def true_job_type(conn: sqlite3.Connection, call_id: int) -> JobType | None:
    row = _row(conn, TRUE_JOB_TYPE_SQL, call_id)
    if row is None:
        return None
    required_skill_code = row["required_skill_code"]
    return JobType(
        job_type_id=int(row["id"]),
        code=str(row["code"]),
        scheduled_blocks=int(row["scheduled_blocks"]),
        required_skill_code=None if required_skill_code is None else str(required_skill_code),
    )


def active_technicians(conn: sqlite3.Connection) -> tuple[Technician, ...]:
    return tuple(
        Technician(
            technician_id=int(row["id"]), hourly_wage_cents=int(row["hourly_wage_cents"])
        )
        for row in conn.execute(ACTIVE_TECHNICIANS_SQL).fetchall()
    )


def arrival_failure_reason(
    conn: sqlite3.Connection, technician_id: int, job_type: JobType
) -> str | None:
    if job_type.required_skill_code is None:
        return None
    holder = _row(conn, SKILL_HOLDER_SQL, technician_id, job_type.required_skill_code)
    return None if holder is not None else "unqualified"


def acceptance_parameters(
    conn: sqlite3.Connection, urgency_class: str
) -> tuple[Decimal, Decimal, Decimal]:
    row = _row(conn, ACCEPTANCE_PARAMETERS_SQL, urgency_class)
    if row is None:
        raise OracleError(f"no acceptance parameters for urgency class {urgency_class!r}")
    scale = Decimal(10**6)
    return (
        Decimal(int(row["base_micros"])) / scale,
        Decimal(int(row["decay_micros"])) / scale,
        Decimal(int(row["floor_micros"])) / scale,
    )


def customer_segment(conn: sqlite3.Connection, customer_id: int) -> str:
    row = _row(conn, CUSTOMER_SEGMENT_SQL, customer_id)
    if row is None:
        raise OracleError(f"customer {customer_id} carries no segment")
    return str(row["segment"])


def expected_margin_cents(config: RunConfig, job_type_code: str, segment: str) -> Decimal:
    amounts = config.resolved["invoices.amount"][job_type_code][segment]
    mean_invoice = (
        _decimal(amounts["min_cents"])
        + _decimal(amounts["mode_cents"])
        + _decimal(amounts["max_cents"])
    ) / Decimal(3)
    percent_range = config.resolved["materials.percent_range"][job_type_code]
    mean_basis_points = (
        _decimal(percent_range["min_basis_points"]) + _decimal(percent_range["max_basis_points"])
    ) / Decimal(2)
    return mean_invoice * (Decimal(1) - mean_basis_points / Decimal(BASIS_POINTS))


def pricing_for(
    conn: sqlite3.Connection,
    config: RunConfig,
    *,
    job_type: JobType,
    segment: str,
    urgency_class: str,
) -> Pricing:
    base, decay, floor = acceptance_parameters(conn, urgency_class)
    return Pricing(
        expected_margin_cents=expected_margin_cents(config, job_type.code, segment),
        acceptance_base=base,
        acceptance_decay=decay,
        acceptance_floor=floor,
        cancellation_base=_decimal(
            config.resolved["cancellations.base_probability_by_segment"][segment]
        ),
        cancellation_urgent_multiplier=config.get_decimal("cancellations.urgent_multiplier"),
        cancellation_lead_days_coefficient=config.get_decimal(
            "cancellations.lead_days_coefficient"
        ),
        off_hours_bonus_cents=config.get_int("payroll.off_hours_bonus_per_job"),
    )


def cancellation_probability(pricing: Pricing, *, urgent: bool, lead_days: Decimal) -> Decimal:
    probability = (
        pricing.cancellation_base
        * (pricing.cancellation_urgent_multiplier if urgent else Decimal(1))
        * (Decimal(1) + pricing.cancellation_lead_days_coefficient * lead_days)
    )
    return max(Decimal(0), min(Decimal(1), probability))


def off_hours_cost_cents(
    plan: BookingPlan, *, hourly_wage_cents: int, bonus_cents: int
) -> int:
    day_type = execution.day_type_for(plan.service_date)
    off_hours = any(
        day_type is DayType.WEEKEND or block_code == OFF_HOURS_BLOCK_CODE
        for block_code in plan.block_codes
    )
    if not off_hours:
        return 0
    return BLOCK_HOURS * plan.scheduled_blocks * hourly_wage_cents + bonus_cents


def _duration_variation_weights() -> list[tuple[int, int]]:
    peak = TIMING_VARIATION_HIGH + 1
    return [
        (minutes, peak - abs(minutes))
        for minutes in range(TIMING_VARIATION_LOW, TIMING_VARIATION_HIGH + 1)
    ]


def expected_overtime_cents(
    plan: BookingPlan, *, hourly_wage_cents: int, paid_off_hours: bool
) -> Decimal:
    scheduled_start = plan.windows[0][0]
    scheduled_end = plan.windows[-1][1]
    planned_end = scheduled_start + BLOCK_HOURS * MINUTES_PER_HOUR * plan.scheduled_blocks * 60_000
    paid = [(scheduled_start, scheduled_end)] if paid_off_hours else []
    weights = _duration_variation_weights()
    total = Decimal(0)
    for variation, weight in weights:
        minutes = overtime.visit_overtime_minutes(
            service_date=plan.service_date,
            scheduled_start=scheduled_start,
            departed_at=scheduled_start,
            ended_at=planned_end + variation * 60_000,
            paid_off_hours=paid,
        )
        total += Decimal(weight * overtime.overtime_cents(minutes, hourly_wage_cents))
    return total / Decimal(sum(weight for _, weight in weights))


def settles_inside_run(service_date: str, end_service_date: str | None) -> bool:
    return end_service_date is None or service_date <= end_service_date


def score_cents(
    plan: BookingPlan,
    pricing: Pricing,
    *,
    urgent: bool,
    arrived_at_ms: int,
    hourly_wage_cents: int,
    failure_reason: str | None,
    end_service_date: str | None = None,
) -> int:
    scheduled_start = plan.windows[0][0]
    lead_days = Decimal(scheduled_start - arrived_at_ms) / Decimal(MILLISECONDS_PER_DAY)
    p_accept = booking._acceptance_probability(
        pricing.acceptance_base,
        pricing.acceptance_decay,
        lead_days,
        pricing.acceptance_floor,
    )
    p_keep = Decimal(1) - cancellation_probability(pricing, urgent=urgent, lead_days=lead_days)
    earns_margin = failure_reason is None and settles_inside_run(
        plan.service_date, end_service_date
    )
    margin = pricing.expected_margin_cents if earns_margin else Decimal(0)
    off_hours = off_hours_cost_cents(
        plan, hourly_wage_cents=hourly_wage_cents, bonus_cents=pricing.off_hours_bonus_cents
    )
    overtime_cost = expected_overtime_cents(
        plan, hourly_wage_cents=hourly_wage_cents, paid_off_hours=off_hours > 0
    )
    return _cents(p_accept * p_keep * (margin - Decimal(off_hours) - overtime_cost))


def walk_candidates(
    ctx: ReadContext,
    config: RunConfig,
    *,
    job_type: JobType,
    technicians: Sequence[Technician],
    from_service_date: str,
    horizon_days: int,
) -> Iterator[tuple[Technician, BookingPlan]]:
    first_day = date.fromisoformat(from_service_date)
    last_block_index = len(BLOCK_CODES) - job_type.scheduled_blocks
    for offset in range(horizon_days + 1):
        service_date = (first_day + timedelta(days=offset)).isoformat()
        for block_index in range(last_block_index + 1):
            for technician in technicians:
                try:
                    plan = appointments.validate_booking(
                        ctx,
                        config,
                        job_type_id=job_type.job_type_id,
                        technician_id=technician.technician_id,
                        service_date=service_date,
                        first_block_code=BLOCK_CODES[block_index],
                    )
                except EngineError:
                    continue
                yield technician, plan


def ranked_candidates(
    ctx: ReadContext,
    config: RunConfig,
    *,
    job_type: JobType,
    technicians: Sequence[Technician],
    pricing: Pricing,
    from_service_date: str,
    horizon_days: int,
    urgent: bool,
    arrived_at_ms: int,
    end_service_date: str | None = None,
) -> list[Candidate]:
    scored: list[Candidate] = []
    for technician, plan in walk_candidates(
        ctx,
        config,
        job_type=job_type,
        technicians=technicians,
        from_service_date=from_service_date,
        horizon_days=horizon_days,
    ):
        block_index = BLOCK_CODES.index(plan.block_codes[0])
        scored.append(
            Candidate(
                service_date=plan.service_date,
                block_start=BLOCK_STARTS[block_index],
                technician_id=technician.technician_id,
                score_cents=score_cents(
                    plan,
                    pricing,
                    urgent=urgent,
                    arrived_at_ms=arrived_at_ms,
                    hourly_wage_cents=technician.hourly_wage_cents,
                    failure_reason=arrival_failure_reason(
                        ctx.conn, technician.technician_id, job_type
                    ),
                    end_service_date=end_service_date,
                ),
            )
        )
    return sorted(scored, key=lambda candidate: -candidate.score_cents)


def greedy_order(ranked: list[Candidate]) -> list[Candidate]:
    return ranked


def date_major_order(ranked: list[Candidate]) -> list[Candidate]:
    return sorted(
        [candidate for candidate in ranked if candidate.score_cents > 0],
        key=lambda candidate: candidate.service_date,
    )


BASELINE_ORDERS: Mapping[str, Callable[[list[Candidate]], list[Candidate]]] = {
    BASELINE_GREEDY: greedy_order,
    BASELINE_DATE_MAJOR: date_major_order,
}

BASELINE_END_AWARE: Mapping[str, bool] = {
    BASELINE_GREEDY: True,
    BASELINE_DATE_MAJOR: False,
}

BASELINE_NAMES: tuple[str, ...] = (BASELINE_GREEDY, BASELINE_DATE_MAJOR)


def _result(response: bytes) -> Mapping[str, Any] | None:
    body = json.loads(response)
    if not body.get("ok"):
        return None
    return body["result"]


class OracleDispatcher:

    def __init__(
        self,
        read_conn: sqlite3.Connection,
        *,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        branch: str = "",
        baseline: str = BASELINE_GREEDY,
    ) -> None:
        if baseline not in BASELINE_ORDERS:
            raise OracleError(f"unknown dispatcher baseline {baseline!r}")
        self.read_conn = read_conn
        self.horizon_days = int(horizon_days)
        self.branch = branch
        self.baseline = baseline
        self.controller: Controller | None = None
        self.decisions: list[str] = []

    @property
    def _controller(self) -> Controller:
        if self.controller is None:
            raise OracleError("the dispatcher was driven before its controller was bound")
        return self.controller

    def end_service_date(self) -> str | None:
        if not BASELINE_END_AWARE[self.baseline]:
            return None
        return self._controller.config.get_string(RUN_END_DATE_KEY)

    def rank(self, call_id: int, ranked: list[Candidate]) -> list[Candidate]:
        return BASELINE_ORDERS[self.baseline](ranked)

    def request_id(self, call_id: int, *parts: object) -> str:
        segments = [POLICY_NAME, str(call_id)]
        if self.branch:
            segments.append(self.branch)
        segments.extend(str(part) for part in parts)
        return "-".join(segments)

    def send(self, prompt: AdapterPrompt) -> AdapterResponse:
        if prompt.kind is PromptKind.CALL:
            return self._decide(prompt)
        if prompt.kind is PromptKind.REMINDER:
            return self._reject(int(prompt.payload["call_id"]), "reminder fallback")
        return AdapterResponse(text="", terminal_action_taken=False)

    def _decide(self, prompt: AdapterPrompt) -> AdapterResponse:
        call_id = int(prompt.payload["call_id"])
        call = read_call(self.read_conn, call_id)
        if str(call["call_kind"]) == "cancellation":
            return self._acknowledge(call_id)
        return self._book(prompt, call)

    def _acknowledge(self, call_id: int) -> AdapterResponse:
        controller = self._controller
        result = _result(
            appointments.acknowledge_cancellation_command(
                controller.services,
                self.request_id(call_id, "acknowledge"),
                controller.config,
                call_id=call_id,
                artifact=controller.artifact,
            )
        )
        return self._record(call_id, "acknowledged", result is not None)

    def _reject(self, call_id: int, reason: str) -> AdapterResponse:
        controller = self._controller
        result = _result(
            calls.reject_command(
                controller.services,
                self.request_id(call_id, "reject"),
                controller.config,
                call_id=call_id,
            )
        )
        return self._record(call_id, f"rejected ({reason})", result is not None)

    def _record(self, call_id: int, outcome: str, terminal: bool) -> AdapterResponse:
        self.decisions.append(f"call {call_id}: {outcome}")
        return AdapterResponse(text="", terminal_action_taken=terminal)

    def _registered(self, prompt: AdapterPrompt, call: sqlite3.Row) -> tuple[int, int] | None:
        controller = self._controller
        call_id = int(call["call_id"])
        customer_id = call["customer_id"]
        if customer_id is None:
            created = _result(
                crm.create_customer_command(
                    controller.services,
                    self.request_id(call_id, "customer"),
                    controller.config,
                    call_id=call_id,
                    name=str(call["caller_name"]),
                    segment=str(call["segment"]),
                    contacts=[
                        {"contact_type": "phone", "value": str(call["caller_phone"])}
                    ],
                )
            )
            if created is None:
                return None
            customer_id = int(created["customer_id"])
        location_id = call["location_id"]
        if location_id is None:
            created = _result(
                crm.create_location_command(
                    controller.services,
                    self.request_id(call_id, "location"),
                    controller.config,
                    call_id=call_id,
                    customer_id=int(customer_id),
                    address=str(prompt.payload["address"]),
                    artifact=controller.artifact,
                )
            )
            if created is None:
                return None
            location_id = int(created["location_id"])
        return int(customer_id), int(location_id)

    def _book(self, prompt: AdapterPrompt, call: sqlite3.Row) -> AdapterResponse:
        controller = self._controller
        call_id = int(call["call_id"])
        job_type = true_job_type(self.read_conn, call_id)
        if job_type is None:
            return self._reject(call_id, "unsupported category")
        technicians = active_technicians(self.read_conn)
        qualified = [
            technician
            for technician in technicians
            if arrival_failure_reason(self.read_conn, technician.technician_id, job_type)
            is None
        ]
        if not qualified:
            return self._reject(call_id, "no qualified technician")

        registration = self._registered(prompt, call)
        if registration is None:
            return self._record(call_id, "registration refused", False)
        customer_id, location_id = registration

        service_date = clock.service_date_for_ms(prompt.ts_ms)
        pricing = pricing_for(
            self.read_conn,
            controller.config,
            job_type=job_type,
            segment=customer_segment(self.read_conn, customer_id),
            urgency_class="urgent" if int(call["urgent"]) else "standard",
        )
        ranked = self.rank(
            call_id,
            ranked_candidates(
                ReadContext(conn=self.read_conn, now_ms=prompt.ts_ms),
                controller.config,
                job_type=job_type,
                technicians=technicians,
                pricing=pricing,
                from_service_date=service_date,
                horizon_days=self.horizon_days,
                urgent=bool(int(call["urgent"])),
                arrived_at_ms=int(call["arrived_at"]),
                end_service_date=self.end_service_date(),
            ),
        )

        attempt = 0
        for candidate in ranked:
            if candidate.score_cents <= 0:
                break
            attempt += 1
            result = _result(
                booking.book_command(
                    controller.services,
                    self.request_id(call_id, "book", attempt),
                    controller.config,
                    call_id=call_id,
                    customer_id=customer_id,
                    location_id=location_id,
                    job_type_id=job_type.job_type_id,
                    service_date=candidate.service_date,
                    block_start=candidate.block_start,
                    technician_id=candidate.technician_id,
                    artifact=controller.artifact,
                )
            )
            if result is not None:
                return self._record(
                    call_id,
                    f"{result['outcome']} on {candidate.service_date} "
                    f"{candidate.block_start} with technician {candidate.technician_id} "
                    f"scored {candidate.score_cents}",
                    True,
                )
        return self._reject(call_id, f"no profitable block in {len(ranked)} candidates")


class DateMajorDispatcher(OracleDispatcher):

    def __init__(
        self,
        read_conn: sqlite3.Connection,
        *,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        branch: str = "",
    ) -> None:
        super().__init__(
            read_conn,
            horizon_days=horizon_days,
            branch=branch,
            baseline=BASELINE_DATE_MAJOR,
        )


def open_read_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    return conn


def run_oracle(
    *,
    config: RunConfig,
    master_seed: int,
    run_dir: str | Path,
    geo_dir: str | Path | None = None,
    scenario: str = DEFAULT_SCENARIO,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    dataset_version: str = "",
    baseline: str = BASELINE_GREEDY,
) -> PolicyRunResult:
    paths = PolicyRunPaths.under(run_dir)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_init.initialize_run(
        paths.db_path,
        master_seed=master_seed,
        config=config,
        dataset_version=dataset_version,
        artifact_dir=geo_dir,
        harness_name=POLICY_NAME,
        harness_version=POLICY_VERSION,
        track=TRACK,
    )
    run_status = drive_oracle(
        paths.db_path,
        config=config,
        geo_dir=geo_dir,
        horizon_days=horizon_days,
        baseline=baseline,
    )
    report = build_report(
        paths.db_path,
        policy_name=POLICY_NAME,
        config=config,
        scenario=scenario,
        geo_dir=geo_dir,
    )
    report_path = paths.run_dir / REPORT_FILENAME
    report_path.write_bytes(report_bytes(report))
    return PolicyRunResult(
        run_id=run_id,
        run_status=run_status,
        paths=paths,
        report=report,
        report_path=report_path,
    )


def drive_oracle(
    db_path: str | Path,
    *,
    config: RunConfig,
    geo_dir: str | Path | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    branch: str = "",
    baseline: str = BASELINE_GREEDY,
) -> str:
    conn = run_init.open_run_db(db_path, artifact_dir=geo_dir)
    read_conn = open_read_connection(db_path)
    try:
        dispatcher = OracleDispatcher(
            read_conn, horizon_days=horizon_days, branch=branch, baseline=baseline
        )
        controller = Controller(conn, config, dispatcher, artifact_dir=geo_dir)
        dispatcher.controller = controller
        return controller.run().value
    finally:
        read_conn.close()
        conn.close()


__all__ = [
    "ACCEPTANCE_PARAMETERS_SQL",
    "ACTIVE_TECHNICIANS_SQL",
    "BASELINE_DATE_MAJOR",
    "BASELINE_END_AWARE",
    "BASELINE_GREEDY",
    "BASELINE_NAMES",
    "BASELINE_ORDERS",
    "BLOCK_STARTS",
    "CALL_SQL",
    "CUSTOMER_SEGMENT_SQL",
    "POLICY_NAME",
    "POLICY_VERSION",
    "SKILL_HOLDER_SQL",
    "TRACK",
    "TRUE_JOB_TYPE_SQL",
    "RUN_END_DATE_KEY",
    "Candidate",
    "DateMajorDispatcher",
    "JobType",
    "OracleDispatcher",
    "OracleError",
    "Pricing",
    "ReadContext",
    "Technician",
    "acceptance_parameters",
    "active_technicians",
    "arrival_failure_reason",
    "cancellation_probability",
    "customer_segment",
    "date_major_order",
    "drive_oracle",
    "expected_margin_cents",
    "greedy_order",
    "off_hours_cost_cents",
    "open_read_connection",
    "pricing_for",
    "ranked_candidates",
    "read_call",
    "run_oracle",
    "score_cents",
    "settles_inside_run",
    "true_job_type",
    "walk_candidates",
]
