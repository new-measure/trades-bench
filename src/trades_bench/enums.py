from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class RunStatus(StrEnum):
    INITIALIZING = "initializing"
    ACTIVE = "active"
    COMPLETED = "completed"
    BANKRUPT = "bankrupt"
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> frozenset[RunStatus]:
        return frozenset({cls.COMPLETED, cls.BANKRUPT, cls.FAILED})


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class InvocationType(StrEnum):
    OPENING = "opening"
    CALL = "call"
    CLOSING = "closing"


class PromptKind(StrEnum):
    OPENING = "opening"
    CALL = "call"
    REMINDER = "reminder"
    CLOSING = "closing"


class InvocationStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"


class VisibilityClass(StrEnum):
    PUBLIC_SQL = "public_sql"
    PUBLIC_SDK = "public_sdk"
    PROMPT_ONLY = "prompt_only"
    NOTIFICATION = "notification"
    PRIVATE_ENGINE = "private_engine"
    POST_RUN_ONLY = "post_run_only"

    @classmethod
    def public(cls) -> frozenset[VisibilityClass]:
        return frozenset(
            {
                cls.PUBLIC_SQL,
                cls.PUBLIC_SDK,
                cls.PROMPT_ONLY,
                cls.NOTIFICATION,
            }
        )


class LedgerCategory(StrEnum):

    OPENING_BALANCE_ADJUSTMENT = "opening_balance_adjustment"
    REVENUE = "revenue"
    MATERIAL_COST = "material_cost"
    PAYROLL_BASE = "payroll_base"
    PAYROLL_OFF_HOURS = "payroll_off_hours"
    PAYROLL_OFF_HOURS_BONUS = "payroll_off_hours_bonus"
    RECRUITING_COST = "recruiting_cost"
    TRAINING_COST = "training_cost"
    OPERATING_OVERHEAD = "operating_overhead"


class ConditionCode(StrEnum):

    CLEAR = "clear"
    HOT = "hot"
    EXTREME_HEAT = "extreme_heat"
    WINDY = "windy"
    RAIN = "rain"
    STORM = "storm"
    COOL = "cool"


class DayType(StrEnum):

    WEEKDAY = "weekday"
    WEEKEND = "weekend"

    @classmethod
    def from_iso_weekday(cls, iso_weekday: int) -> DayType:
        if (
            isinstance(iso_weekday, bool)
            or not isinstance(iso_weekday, int)
            or not 1 <= iso_weekday <= 7
        ):
            raise ValueError(f"iso_weekday must be an int in 1..7, got {iso_weekday!r}")
        return cls.WEEKDAY if iso_weekday <= 5 else cls.WEEKEND


class EntityType(StrEnum):

    RUN = "run"
    ZONE = "zone"
    CUSTOMER = "customer"
    APPOINTMENT = "appointment"
    TECHNICIAN = "technician"
    PENDING_CALL = "pending_call"


class EventKind(StrEnum):

    DAY_ROLLOVER = "day_rollover"
    EMPLOYMENT_TRANSITION = "employment_transition"
    WEATHER_REALIZED = "weather_realized"
    LABOR_ECONOMY_UPDATE = "labor_economy_update"
    SICKNESS_DRAW = "sickness_draw"
    ATTRITION_DRAW = "attrition_draw"
    TRAINING_RATES_DRAWN = "training_rates_drawn"
    SHIFT_MATERIALIZATION = "shift_materialization"
    DEMAND_GENERATION = "demand_generation"
    BLOCK_LOCK = "block_lock"
    APPOINTMENT_DISPATCH = "appointment_dispatch"
    APPOINTMENT_ARRIVAL = "appointment_arrival"
    WORK_STARTED = "work_started"
    WORK_COMPLETED = "work_completed"
    WORK_FAILED = "work_failed"
    COMPANY_NO_SHOW = "company_no_show"
    OFF_HOURS_ACCRUAL_MARK = "off_hours_accrual_mark"
    TECHNICIAN_RETURN_HOME = "technician_return_home"
    TECHNICIAN_HOME_ARRIVAL = "technician_home_arrival"
    REVIEW_PUBLISHED = "review_published"
    COMPETITOR_EFFECT = "competitor_effect"
    OPENING_INVOCATION = "opening_invocation"
    FIRST_BLOCK_LOCK = "first_block_lock"
    FIRST_TRIP_DISPATCH = "first_trip_dispatch"
    CALL_INVOCATION = "call_invocation"
    CLOSING_INVOCATION = "closing_invocation"
    DAILY_SETTLEMENT = "daily_settlement"
    CHECKPOINT = "checkpoint"
    RUN_FINALIZE = "run_finalize"


EVENT_PRIORITY: Mapping[EventKind, int] = MappingProxyType(
    {
        EventKind.DAY_ROLLOVER: 100,
        EventKind.EMPLOYMENT_TRANSITION: 110,
        EventKind.WEATHER_REALIZED: 120,
        EventKind.LABOR_ECONOMY_UPDATE: 130,
        EventKind.SICKNESS_DRAW: 140,
        EventKind.ATTRITION_DRAW: 150,
        EventKind.TRAINING_RATES_DRAWN: 160,
        EventKind.SHIFT_MATERIALIZATION: 170,
        EventKind.DEMAND_GENERATION: 180,
        EventKind.BLOCK_LOCK: 200,
        EventKind.APPOINTMENT_DISPATCH: 300,
        EventKind.APPOINTMENT_ARRIVAL: 310,
        EventKind.WORK_STARTED: 320,
        EventKind.WORK_COMPLETED: 330,
        EventKind.WORK_FAILED: 332,
        EventKind.COMPANY_NO_SHOW: 335,
        EventKind.OFF_HOURS_ACCRUAL_MARK: 340,
        EventKind.TECHNICIAN_RETURN_HOME: 350,
        EventKind.TECHNICIAN_HOME_ARRIVAL: 360,
        EventKind.REVIEW_PUBLISHED: 410,
        EventKind.COMPETITOR_EFFECT: 420,
        EventKind.OPENING_INVOCATION: 900,
        EventKind.FIRST_BLOCK_LOCK: 905,
        EventKind.FIRST_TRIP_DISPATCH: 906,
        EventKind.CALL_INVOCATION: 910,
        EventKind.CLOSING_INVOCATION: 920,
        EventKind.DAILY_SETTLEMENT: 950,
        EventKind.CHECKPOINT: 960,
        EventKind.RUN_FINALIZE: 970,
    }
)


class SequentialStreamName(StrEnum):

    WEATHER_PROCESS = "weather_process"
    LABOR_ECONOMY = "labor_economy"
    COMPETITOR_BACKGROUND = "competitor_background"
    WARM_START = "warm_start"


class KeyedStreamName(StrEnum):

    WEATHER_FORECAST = "weather_forecast"
    DEMAND = "demand"
    ACCEPTANCE = "acceptance"
    CANCELLATION = "cancellation"
    COMPETITOR_LEAK = "competitor_leak"
    TRAVEL_NOISE = "travel_noise"
    WORKFORCE_HEALTH = "workforce_health"
    WORKFORCE_ATTRITION = "workforce_attrition"
    HIRING = "hiring"
    TRAINING = "training"
    TECHNICIAN_QUALITY = "technician_quality"
    JOB_EXECUTION = "job_execution"
    JOB_QUALITY = "job_quality"
    JOB_FINANCE = "job_finance"
    CALLBACKS = "callbacks"
    REVIEWS = "reviews"


ALL_STREAM_NAMES: frozenset[str] = frozenset(
    str(s) for s in (*SequentialStreamName, *KeyedStreamName)
)

SEQUENTIAL_STREAM_COUNT = 4
KEYED_STREAM_COUNT = 16
