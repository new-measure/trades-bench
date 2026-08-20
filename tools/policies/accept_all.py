from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from policies.base import AgentApi, ApiResult, PolicyOutcome, PolicyRequest
from trades_bench import clock
from trades_bench.appointments import BLOCK_CODES
from trades_bench.constants import BLOCK_GRID_MINUTES

POLICY_NAME = "accept_all"
POLICY_VERSION = "1.0.0"

BLOCK_STARTS: tuple[str, ...] = tuple(
    f"{start // 60:02d}:{start % 60:02d}" for start, _end in BLOCK_GRID_MINUTES
)

ADVANCING_ERROR_CODES = frozenset({"conflict", "validation_error"})

TECHNICIANS_SQL = (
    "SELECT id FROM technicians WHERE employment_status = 'active' ORDER BY id"
)
JOB_TYPES_SQL = (
    "SELECT id, code, scheduled_blocks, required_skill_code FROM job_types "
    "WHERE active = 1 ORDER BY id"
)
SKILLS_SQL = (
    "SELECT technician_id, skill_code FROM technician_skills WHERE active = 1 "
    "ORDER BY technician_id, skill_code"
)
SHIFTS_SQL = (
    "SELECT technician_id, service_date, start_at, end_at FROM technician_shifts "
    "WHERE active = 1 ORDER BY service_date, technician_id, start_at"
)
OCCUPIED_SQL = (
    "SELECT aa.technician_id AS technician_id, ab.block_code AS block_code, "
    "ab.block_start AS block_start "
    "FROM appointment_blocks ab "
    "JOIN appointment_assignments aa ON aa.appointment_id = ab.appointment_id "
    "WHERE aa.status = 'active' "
    "ORDER BY ab.block_start, aa.technician_id, ab.block_code"
)


@dataclass(frozen=True)
class Candidate:

    service_date: str
    block_start: str
    technician_id: int


def _block_window_ms(service_date: str, block_index: int) -> tuple[int, int]:
    day = date.fromisoformat(service_date)
    start_minutes, end_minutes = BLOCK_GRID_MINUTES[block_index]
    return (
        clock.phoenix_instant(
            day.year, day.month, day.day, start_minutes // 60, start_minutes % 60
        ),
        clock.phoenix_instant(
            day.year, day.month, day.day, end_minutes // 60, end_minutes % 60
        ),
    )


def _job_type(api: AgentApi, requested_service: str | None) -> Mapping[str, object] | None:
    if not requested_service:
        return None
    wanted = "".join(
        character if character.isalnum() else "_" for character in requested_service.lower()
    )
    for row in api.sql(JOB_TYPES_SQL):
        if str(row["code"]) == wanted:
            return row
    return None


def _technician_ids(api: AgentApi) -> tuple[int, ...]:
    return tuple(int(row["id"]) for row in api.sql(TECHNICIANS_SQL))


def _skills_by_technician(api: AgentApi) -> dict[int, frozenset[str]]:
    holders: dict[int, set[str]] = {}
    for row in api.sql(SKILLS_SQL):
        holders.setdefault(int(row["technician_id"]), set()).add(str(row["skill_code"]))
    return {technician_id: frozenset(codes) for technician_id, codes in holders.items()}


def _qualified_technician_ids(
    api: AgentApi, required_skill_code: str | None
) -> tuple[int, ...]:
    technician_ids = _technician_ids(api)
    if required_skill_code is None:
        return technician_ids
    skills = _skills_by_technician(api)
    return tuple(
        technician_id
        for technician_id in technician_ids
        if required_skill_code in skills.get(technician_id, frozenset())
    )


def _shifts_by_day(api: AgentApi) -> dict[str, list[tuple[int, int, int]]]:
    shifts: dict[str, list[tuple[int, int, int]]] = {}
    for row in api.sql(SHIFTS_SQL):
        shifts.setdefault(str(row["service_date"]), []).append(
            (int(row["technician_id"]), int(row["start_at"]), int(row["end_at"]))
        )
    return shifts


def _occupied_blocks(api: AgentApi) -> frozenset[tuple[int, str, str]]:
    occupied = set()
    for row in api.sql(OCCUPIED_SQL):
        service_date = clock.service_date_for_ms(int(row["block_start"]))
        occupied.add((int(row["technician_id"]), service_date, str(row["block_code"])))
    return frozenset(occupied)


def _covered_by_shift(
    shifts: Sequence[tuple[int, int, int]], technician_id: int, window: tuple[int, int]
) -> bool:
    return any(
        shift_technician == technician_id and start_at <= window[0] and end_at >= window[1]
        for shift_technician, start_at, end_at in shifts
    )


def candidates(
    api: AgentApi,
    request: PolicyRequest,
    scheduled_blocks: int,
    required_skill_code: str | None = None,
) -> Iterator[Candidate]:
    technician_ids = _qualified_technician_ids(api, required_skill_code)
    if not technician_ids:
        return
    shifts_by_day = _shifts_by_day(api)
    occupied = _occupied_blocks(api)
    moment = request.call.moment
    now_ms = clock.phoenix_instant(
        moment.year, moment.month, moment.day, moment.hour, moment.minute
    )
    first_day = date.fromisoformat(request.service_date)
    for offset in range(int(request.horizon_days) + 1):
        day = first_day + timedelta(days=offset)
        service_date = day.isoformat()
        shifts = shifts_by_day.get(service_date, [])
        if not shifts:
            continue
        for block_index in range(len(BLOCK_CODES) - scheduled_blocks + 1):
            reserved = BLOCK_CODES[block_index : block_index + scheduled_blocks]
            windows = [
                _block_window_ms(service_date, index)
                for index in range(block_index, block_index + scheduled_blocks)
            ]
            if windows[0][0] <= now_ms:
                continue
            for technician_id in technician_ids:
                if any(
                    (technician_id, service_date, block_code) in occupied
                    for block_code in reserved
                ):
                    continue
                if not all(
                    _covered_by_shift(shifts, technician_id, window) for window in windows
                ):
                    continue
                yield Candidate(
                    service_date=service_date,
                    block_start=BLOCK_STARTS[block_index],
                    technician_id=technician_id,
                )


def _advances(result: ApiResult) -> bool:
    return result.error_code in ADVANCING_ERROR_CODES


def _register_customer(request: PolicyRequest) -> tuple[int | None, str]:
    call = request.call
    if call.segment is None or call.phone is None:
        return None, "call carries no segment or phone to register the caller with"
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
        return None, result.describe()
    return int(result.data["customer_id"]), "customer registered"


def _register_location(request: PolicyRequest, customer_id: int) -> tuple[int | None, str]:
    call = request.call
    if call.address is None:
        return None, "call carries no address to register the location with"
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
        return None, result.describe()
    return int(result.data["location_id"]), "location registered"


def reject(request: PolicyRequest, note: str) -> PolicyOutcome:
    result = request.api.op(
        "calls.reject",
        {"call_id": request.call.call_id, "request_id": request.request_id("reject")},
    )
    if not result.ok:
        return PolicyOutcome(notes=(note, result.describe()))
    return PolicyOutcome(terminal=True, notes=(note, f"rejected: {result.outcome}"))


def acknowledge_cancellation(request: PolicyRequest) -> PolicyOutcome:
    result = request.api.op(
        "appointments.acknowledge_cancellation",
        {"call_id": request.call.call_id, "request_id": request.request_id("acknowledge")},
    )
    if not result.ok:
        return PolicyOutcome(notes=(result.describe(),))
    return PolicyOutcome(terminal=True, notes=(f"cancellation acknowledged: {result.outcome}",))


def book_new_job(request: PolicyRequest) -> PolicyOutcome:
    call = request.call
    notes: list[str] = []

    job_type = _job_type(request.api, call.requested_service)
    if job_type is None:
        return reject(request, f"no supported job type for {call.requested_service!r}")

    customer_id = call.customer_id
    if customer_id is None:
        customer_id, note = _register_customer(request)
        notes.append(note)
        if customer_id is None:
            return PolicyOutcome(notes=tuple(notes))

    location_id = call.location_id
    if location_id is None:
        location_id, note = _register_location(request, customer_id)
        notes.append(note)
        if location_id is None:
            return PolicyOutcome(notes=tuple(notes))

    required_skill_code = job_type["required_skill_code"]
    attempt = 0
    for candidate in candidates(
        request.api,
        request,
        int(job_type["scheduled_blocks"]),
        None if required_skill_code is None else str(required_skill_code),
    ):
        attempt += 1
        result = request.api.op(
            "jobs.book",
            {
                "call_id": call.call_id,
                "customer_id": customer_id,
                "location_id": location_id,
                "job_type_id": int(job_type["id"]),
                "service_date": candidate.service_date,
                "block_start": candidate.block_start,
                "technician_id": candidate.technician_id,
                "request_id": request.request_id("book", attempt),
            },
        )
        if result.ok:
            notes.append(
                f"booking {result.outcome} on {candidate.service_date} "
                f"{candidate.block_start} with technician {candidate.technician_id}"
            )
            return PolicyOutcome(terminal=True, notes=tuple(notes))
        if _advances(result):
            continue
        notes.append(result.describe())
        return PolicyOutcome(notes=tuple(notes))

    notes.append(f"no feasible block found in {attempt} attempts")
    return PolicyOutcome(notes=tuple(notes))


def accept_all_decision(request: PolicyRequest) -> PolicyOutcome:
    if request.call.is_cancellation:
        return acknowledge_cancellation(request)
    return book_new_job(request)


class AcceptAllPolicy:

    def __init__(self, *, name: str = POLICY_NAME, version: str = POLICY_VERSION) -> None:
        self.name = name
        self.version = version

    def decide(self, request: PolicyRequest) -> PolicyOutcome:
        return accept_all_decision(request)


__all__ = [
    "ADVANCING_ERROR_CODES",
    "BLOCK_STARTS",
    "POLICY_NAME",
    "POLICY_VERSION",
    "Candidate",
    "AcceptAllPolicy",
    "acknowledge_cancellation",
    "book_new_job",
    "candidates",
    "accept_all_decision",
    "reject",
]
