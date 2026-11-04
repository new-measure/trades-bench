from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from policies import accept_all
from policies.base import PolicyOutcome, PolicyRequest
from trades_bench import clock
from trades_bench.appointments import BLOCK_CODES, is_off_hours

POLICY_NAME = "oracle"
POLICY_VERSION = "2.1.0"

EARTH_DIAMETER_KM = 12742.0
ROAD_DETOUR_FACTOR = 1.3
PLANNING_SPEED_KMH = 40.0
MINIMUM_TRAVEL_MINUTES = 5
OPENING_MINUTE_OF_DAY = 6 * 60 + 45
MINUTE_MS = 60_000
INSTALL_JOB_TYPE_CODE = "hvac_installation"
PUBLISHED_DURATIONS: Mapping[str, Mapping[str, int]] = {
    "hvac_service": {"min": 45, "mode": 75, "max": 180},
    "hvac_installation": {"min": 120, "mode": 300, "max": 480},
    "plumbing": {"min": 45, "mode": 75, "max": 180},
    "electrical": {"min": 45, "mode": 90, "max": 210},
}

ROUTE_SQL = (
    "SELECT aa.technician_id AS technician_id, a.id AS appointment_id, "
    "a.arrival_window_start AS window_start, a.arrival_window_end AS window_end, "
    "l.latitude AS latitude, l.longitude AS longitude, jt.code AS job_type_code, "
    "jt.required_skill_code AS required_skill_code, "
    "a.status AS status, a.actual_start AS actual_start "
    "FROM appointments a "
    "JOIN appointment_assignments aa ON aa.appointment_id = a.id AND aa.status = 'active' "
    "JOIN jobs j ON j.id = a.job_id "
    "JOIN job_types jt ON jt.id = j.job_type_id "
    "JOIN locations l ON l.id = j.location_id "
    "WHERE a.status NOT IN ('completed', 'failed', 'no_show', 'cancelled') "
    "ORDER BY a.arrival_window_start, a.id"
)
HOMES_SQL = (
    "SELECT id, home_latitude, home_longitude FROM technicians "
    "WHERE employment_status = 'active' ORDER BY id"
)
LOCATION_SQL = "SELECT latitude, longitude FROM locations WHERE id = {location_id}"


@dataclass(frozen=True)
class Stop:

    window_start: int
    window_end: int
    point: tuple[float, float]
    duration_minutes: int
    started_at: int | None = None
    appointment_id: int | None = None
    skill_code: str | None = None
    movable: bool = False
    unstarted: bool = False


@dataclass(frozen=True)
class Slot:

    service_date: str
    block_index: int
    technician_id: int
    window: tuple[int, int]
    off_hours: bool
    added_minutes: int


def travel_minutes(a: tuple[float, float], b: tuple[float, float]) -> int:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    haversine = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    kilometres = EARTH_DIAMETER_KM * math.asin(math.sqrt(haversine)) * ROAD_DETOUR_FACTOR
    return max(MINIMUM_TRAVEL_MINUTES, round(kilometres / PLANNING_SPEED_KMH * 60))


def expected_minutes(durations: Mapping[str, Mapping[str, int]], job_type_code: str) -> int:
    triple = durations[job_type_code]
    total = int(triple["min"]) + int(triple["mode"]) + int(triple["max"])
    return (total + 1) // 3


def quantile_minutes(
    durations: Mapping[str, Mapping[str, int]], job_type_code: str, quantile: float
) -> int:
    triple = durations[job_type_code]
    low, mode, high = int(triple["min"]), int(triple["mode"]), int(triple["max"])
    if high == low:
        return low
    if quantile < (mode - low) / (high - low):
        value = low + math.sqrt(quantile * (high - low) * (mode - low))
    else:
        value = high - math.sqrt((1 - quantile) * (high - low) * (high - mode))
    return math.ceil(value)


def planned_load_minutes(stops: Sequence[Stop], driven: int) -> int:
    return driven + sum(stop.duration_minutes for stop in stops)


def _day_floor_ms(service_date: str) -> int:
    day = date.fromisoformat(service_date)
    return clock.phoenix_instant(
        day.year, day.month, day.day, OPENING_MINUTE_OF_DAY // 60, OPENING_MINUTE_OF_DAY % 60
    )


def planned_starts(
    home: tuple[float, float], stops: Sequence[Stop], *, floor_ms: int
) -> Iterator[tuple[Stop, int, int]]:
    started = sorted(
        (stop for stop in stops if stop.started_at is not None),
        key=lambda stop: int(stop.started_at or 0),
    )
    remaining = sorted(
        (stop for stop in stops if stop.started_at is None),
        key=lambda stop: (stop.window_start, stop.point),
    )
    position = home
    clock_ms = floor_ms
    driven = 0
    first = not started
    for stop in started:
        clock_ms = max(clock_ms, int(stop.started_at or 0) + stop.duration_minutes * MINUTE_MS)
        position = stop.point
    while remaining:
        earliest = remaining[0].window_start
        same_window = [stop for stop in remaining if stop.window_start == earliest]
        stop = min(same_window, key=lambda candidate: travel_minutes(position, candidate.point))
        remaining.remove(stop)
        leg = travel_minutes(position, stop.point)
        driven += leg
        arrival = (
            max(stop.window_start, clock_ms + leg * MINUTE_MS)
            if first
            else clock_ms + leg * MINUTE_MS
        )
        start = max(arrival, stop.window_start)
        yield stop, start, driven
        clock_ms = start + stop.duration_minutes * MINUTE_MS
        position = stop.point
        first = False


def route_plan(
    home: tuple[float, float],
    stops: Sequence[Stop],
    *,
    floor_ms: int,
    margin_minutes: int,
) -> tuple[bool, int]:
    driven = 0
    for stop, start, driven in planned_starts(home, stops, floor_ms=floor_ms):
        if start > stop.window_end - margin_minutes * MINUTE_MS:
            return False, driven
    return True, driven


def late_stops(
    home: tuple[float, float], stops: Sequence[Stop], *, floor_ms: int
) -> list[Stop]:
    return [
        stop
        for stop, start, _ in planned_starts(home, stops, floor_ms=floor_ms)
        if start > stop.window_end
    ]


def reassignment_fits(
    source: tuple[tuple[float, float], Sequence[Stop]],
    target: tuple[tuple[float, float], Sequence[Stop]],
    *,
    moved: Stop,
    new_stop: Stop,
    floor_ms: int,
    margin_minutes: int,
) -> bool:
    source_home, source_stops = source
    target_home, target_stops = target
    kept = [stop for stop in source_stops if stop is not moved]
    source_fits, _ = route_plan(
        source_home, [*kept, new_stop], floor_ms=floor_ms, margin_minutes=margin_minutes
    )
    if not source_fits:
        return False
    target_fits, _ = route_plan(
        target_home, [*target_stops, moved], floor_ms=floor_ms, margin_minutes=margin_minutes
    )
    return target_fits


@dataclass(frozen=True)
class Move:

    appointment_id: int
    technician_id: int
    slot: Slot


@dataclass(frozen=True)
class Board:

    now_ms: int
    homes: dict[int, tuple[float, float]]
    routes: dict[tuple[int, str], list[Stop]]
    shifts_by_day: dict[str, list[tuple[int, int, int]]]
    skills: dict[int, frozenset[str]]


class OraclePolicy:

    def __init__(
        self,
        *,
        margin_minutes: int = 40,
        urgent_margin_minutes: int = 40,
        defer_off_hours: bool = False,
        reassign: bool = False,
        planning_quantile: float | None = 0.65,
        install_latest_start: str | None = None,
        max_planned_minutes: int | None = None,
        rescue: bool = False,
        durations: Mapping[str, Mapping[str, int]] | None = None,
        name: str = POLICY_NAME,
        version: str = POLICY_VERSION,
    ) -> None:
        self.defer_off_hours = defer_off_hours
        self.reassign = reassign
        self.planning_quantile = planning_quantile
        self.install_latest_start = install_latest_start
        self.max_planned_minutes = max_planned_minutes
        self.rescue = rescue
        self.durations = PUBLISHED_DURATIONS if durations is None else durations
        self.margin_minutes = margin_minutes
        self.urgent_margin_minutes = urgent_margin_minutes
        self.name = name
        self.version = version

    def decide(self, request: PolicyRequest) -> PolicyOutcome:
        if request.call.is_cancellation:
            return accept_all.acknowledge_cancellation(request)
        if not self.rescue:
            return self._book(request)
        notes = self._rescue(request)
        outcome = self._book(request)
        return PolicyOutcome(terminal=outcome.terminal, notes=(*notes, *outcome.notes))

    def _rescue(self, request: PolicyRequest) -> tuple[str, ...]:
        board = self._board(request)
        service_date = request.service_date
        floor_ms = self._floor(board, service_date)
        candidates: list[tuple[int, int, int, int]] = []
        for (technician_id, day), stops in sorted(board.routes.items()):
            if day != service_date or technician_id not in board.homes:
                continue
            home = board.homes[technician_id]
            for late in late_stops(home, stops, floor_ms=floor_ms):
                if not late.unstarted or late.appointment_id is None:
                    continue
                window = (late.window_start, late.window_end)
                for target_id in self._qualified(board, late.skill_code):
                    if target_id == technician_id or not self._can_staff(
                        board, target_id, service_date, window
                    ):
                        continue
                    target_stops = board.routes.get((target_id, service_date), [])
                    target_home = board.homes[target_id]
                    fits, driven = route_plan(
                        target_home, [*target_stops, late],
                        floor_ms=floor_ms, margin_minutes=self.margin_minutes,
                    )
                    if not fits:
                        continue
                    _, before = route_plan(
                        target_home, target_stops, floor_ms=floor_ms, margin_minutes=0
                    )
                    candidates.append(
                        (late.window_end, driven - before, late.appointment_id, target_id)
                    )
        if not candidates:
            return ()
        _, _, appointment_id, target_id = min(candidates)
        result = request.api.op(
            "dispatch.assign",
            {
                "appointment_id": appointment_id,
                "technician_id": target_id,
                "request_id": request.request_id("rescue", appointment_id),
            },
        )
        if not result.ok:
            return (result.describe(),)
        return (f"rescued appointment {appointment_id} to technician {target_id}",)

    def _book(self, request: PolicyRequest) -> PolicyOutcome:
        call = request.call
        notes: list[str] = []
        wanted = "".join(
            character if character.isalnum() else "_"
            for character in (call.requested_service or "").lower()
        )
        job_type = next(
            (row for row in request.api.sql(accept_all.JOB_TYPES_SQL) if str(row["code"]) == wanted),
            None,
        )
        if job_type is None:
            return accept_all.reject(request, f"we do not do {call.requested_service!r}")
        customer_id = call.customer_id
        if customer_id is None:
            customer_id, note = accept_all._register_customer(request)
            notes.append(note)
            if customer_id is None:
                return PolicyOutcome(notes=tuple(notes))
        location_id = call.location_id
        if location_id is None:
            location_id, note = accept_all._register_location(request, customer_id)
            notes.append(note)
            if location_id is None:
                return PolicyOutcome(notes=tuple(notes))
        row = request.api.sql(LOCATION_SQL.format(location_id=int(location_id)))[0]
        point = (float(row["latitude"]), float(row["longitude"]))
        new_stop_minutes = self._planned_minutes(str(job_type["code"]))
        skill = job_type["required_skill_code"]
        required_skill_code = None if skill is None else str(skill)
        install = str(job_type["code"]) == INSTALL_JOB_TYPE_CODE
        margin = self.urgent_margin_minutes if call.urgent else self.margin_minutes
        board = self._board(request)
        booking = {
            "call_id": call.call_id,
            "customer_id": customer_id,
            "location_id": location_id,
            "job_type_id": int(job_type["id"]),
        }
        slots = self._slots(
            request, board, point=point, duration_minutes=new_stop_minutes,
            required_skill_code=required_skill_code, margin_minutes=margin, install=install,
            load_cap=self.max_planned_minutes,
        )
        if not slots and self.reassign:
            move = self._move(
                request, board, point=point, duration_minutes=new_stop_minutes,
                required_skill_code=required_skill_code, margin_minutes=margin,
                install=install, urgent=call.urgent,
            )
            if move is not None:
                result = request.api.op(
                    "dispatch.assign",
                    {
                        "appointment_id": move.appointment_id,
                        "technician_id": move.technician_id,
                        "request_id": request.request_id("reassign", move.appointment_id),
                    },
                )
                if result.ok:
                    notes.append(
                        f"moved appointment {move.appointment_id} to technician "
                        f"{move.technician_id}"
                    )
                    slots = [move.slot]
                else:
                    notes.append(result.describe())
        if not slots:
            slots = self._slots(
                request, board, point=point, duration_minutes=new_stop_minutes,
                required_skill_code=required_skill_code, margin_minutes=0, install=install,
            )
        if install:
            slots = self._prefer_mornings(slots)
        attempt = 0
        for slot in self._ordered(slots, urgent=call.urgent):
            attempt += 1
            result = request.api.op(
                "jobs.book",
                {
                    **booking,
                    "service_date": slot.service_date,
                    "block_start": accept_all.BLOCK_STARTS[slot.block_index],
                    "technician_id": slot.technician_id,
                    "request_id": request.request_id("book", attempt),
                },
            )
            if result.ok:
                notes.append(
                    f"booking {result.outcome} on {slot.service_date} "
                    f"{accept_all.BLOCK_STARTS[slot.block_index]} with technician {slot.technician_id}"
                )
                return PolicyOutcome(terminal=True, notes=tuple(notes))
            if accept_all._advances(result):
                continue
            notes.append(result.describe())
            return PolicyOutcome(notes=tuple(notes))
        return accept_all.reject(request, f"no window fits the route in {attempt} attempts")

    def _planned_minutes(self, job_type_code: str) -> int:
        if self.planning_quantile is None:
            return expected_minutes(self.durations, job_type_code)
        return quantile_minutes(self.durations, job_type_code, self.planning_quantile)

    def _prefer_mornings(self, slots: list[Slot]) -> list[Slot]:
        if self.install_latest_start is None:
            return slots
        latest = self.install_latest_start
        mornings = [
            slot for slot in slots if accept_all.BLOCK_STARTS[slot.block_index] <= latest
        ]
        return mornings or slots

    def _ordered(self, slots: list[Slot], *, urgent: bool) -> list[Slot]:
        def key(slot: Slot) -> tuple[int, int, int, int]:
            deferred = 1 if self.defer_off_hours and not urgent and slot.off_hours else 0
            return (deferred, slot.window[0], slot.added_minutes, slot.technician_id)

        return sorted(slots, key=key)

    def _board(self, request: PolicyRequest) -> Board:
        api = request.api
        moment = request.call.moment
        now_ms = clock.phoenix_instant(
            moment.year, moment.month, moment.day, moment.hour, moment.minute
        )
        homes = {
            int(row["id"]): (float(row["home_latitude"]), float(row["home_longitude"]))
            for row in api.sql(HOMES_SQL)
        }
        routes: dict[tuple[int, str], list[Stop]] = {}
        for row in api.sql(ROUTE_SQL):
            started_at = None if row["actual_start"] is None else int(row["actual_start"])
            window_start = int(row["window_start"])
            if started_at is None and int(row["window_end"]) <= now_ms:
                continue
            skill = row["required_skill_code"]
            service_date = clock.service_date_for_ms(window_start)
            routes.setdefault((int(row["technician_id"]), service_date), []).append(
                Stop(
                    window_start=window_start,
                    window_end=int(row["window_end"]),
                    point=(float(row["latitude"]), float(row["longitude"])),
                    duration_minutes=self._planned_minutes(str(row["job_type_code"])),
                    started_at=started_at,
                    appointment_id=int(row["appointment_id"]),
                    skill_code=None if skill is None else str(skill),
                    movable=(
                        started_at is None
                        and str(row["status"]) == "scheduled"
                        and window_start > now_ms
                    ),
                    unstarted=started_at is None and str(row["status"]) == "scheduled",
                )
            )
        return Board(
            now_ms=now_ms,
            homes=homes,
            routes=routes,
            shifts_by_day=accept_all._shifts_by_day(api),
            skills=accept_all._skills_by_technician(api),
        )

    def _windows(
        self, request: PolicyRequest, board: Board, *, install: bool
    ) -> list[tuple[str, int, tuple[int, int], bool]]:
        first_day = date.fromisoformat(request.service_date)
        windows: list[tuple[str, int, tuple[int, int], bool]] = []
        for offset in range(int(request.horizon_days) + 1):
            service_date = (first_day + timedelta(days=offset)).isoformat()
            if not board.shifts_by_day.get(service_date):
                continue
            for block_index, block_code in enumerate(BLOCK_CODES):
                window = accept_all._block_window_ms(service_date, block_index)
                if window[0] <= board.now_ms:
                    continue
                off_hours = is_off_hours(service_date, (block_code,))
                if install and off_hours:
                    continue
                windows.append((service_date, block_index, window, off_hours))
        return windows

    def _can_staff(
        self, board: Board, technician_id: int, service_date: str, window: tuple[int, int]
    ) -> bool:
        return technician_id in board.homes and accept_all._covered_by_shift(
            board.shifts_by_day.get(service_date, []), technician_id, window
        )

    def _qualified(self, board: Board, required_skill_code: str | None) -> list[int]:
        return [
            technician_id
            for technician_id in sorted(board.homes)
            if required_skill_code is None
            or required_skill_code in board.skills.get(technician_id, frozenset())
        ]

    def _floor(self, board: Board, service_date: str) -> int:
        return max(_day_floor_ms(service_date), board.now_ms)

    def _slots(
        self,
        request: PolicyRequest,
        board: Board,
        *,
        point: tuple[float, float],
        duration_minutes: int,
        required_skill_code: str | None,
        margin_minutes: int,
        install: bool = False,
        load_cap: int | None = None,
    ) -> list[Slot]:
        slots: list[Slot] = []
        for service_date, block_index, window, off_hours in self._windows(
            request, board, install=install
        ):
            floor_ms = self._floor(board, service_date)
            for technician_id in self._qualified(board, required_skill_code):
                if not self._can_staff(board, technician_id, service_date, window):
                    continue
                existing = board.routes.get((technician_id, service_date), [])
                home = board.homes[technician_id]
                planned = [*existing, Stop(window[0], window[1], point, duration_minutes)]
                fits, driven = route_plan(
                    home, planned, floor_ms=floor_ms, margin_minutes=margin_minutes
                )
                if not fits:
                    continue
                if load_cap is not None and planned_load_minutes(planned, driven) > load_cap:
                    continue
                _, before = route_plan(
                    home, existing, floor_ms=floor_ms, margin_minutes=margin_minutes
                )
                slots.append(
                    Slot(
                        service_date=service_date,
                        block_index=block_index,
                        technician_id=technician_id,
                        window=window,
                        off_hours=off_hours,
                        added_minutes=driven - before,
                    )
                )
        return slots

    def _move(
        self,
        request: PolicyRequest,
        board: Board,
        *,
        point: tuple[float, float],
        duration_minutes: int,
        required_skill_code: str | None,
        margin_minutes: int,
        install: bool,
        urgent: bool,
    ) -> Move | None:
        moves: list[Move] = []
        for service_date, block_index, window, off_hours in self._windows(
            request, board, install=install
        ):
            floor_ms = self._floor(board, service_date)
            new_stop = Stop(window[0], window[1], point, duration_minutes)
            for source_id in self._qualified(board, required_skill_code):
                if not self._can_staff(board, source_id, service_date, window):
                    continue
                source_stops = board.routes.get((source_id, service_date), [])
                for moved in source_stops:
                    if not moved.movable or moved.appointment_id is None:
                        continue
                    moved_window = (moved.window_start, moved.window_end)
                    for target_id in self._qualified(board, moved.skill_code):
                        if target_id == source_id or not self._can_staff(
                            board, target_id, service_date, moved_window
                        ):
                            continue
                        if reassignment_fits(
                            (board.homes[source_id], source_stops),
                            (
                                board.homes[target_id],
                                board.routes.get((target_id, service_date), []),
                            ),
                            moved=moved,
                            new_stop=new_stop,
                            floor_ms=floor_ms,
                            margin_minutes=margin_minutes,
                        ):
                            moves.append(
                                Move(
                                    appointment_id=moved.appointment_id,
                                    technician_id=target_id,
                                    slot=Slot(
                                        service_date=service_date,
                                        block_index=block_index,
                                        technician_id=source_id,
                                        window=window,
                                        off_hours=off_hours,
                                        added_minutes=0,
                                    ),
                                )
                            )
        if not moves:
            return None
        ordered = self._ordered([move.slot for move in moves], urgent=urgent)
        first = ordered[0]
        return next(move for move in moves if move.slot is first)


__all__ = [
    "POLICY_NAME",
    "POLICY_VERSION",
    "PUBLISHED_DURATIONS",
    "OraclePolicy",
    "expected_minutes",
    "late_stops",
    "planned_starts",
    "planned_load_minutes",
    "quantile_minutes",
    "reassignment_fits",
    "route_plan",
    "travel_minutes",
]
