from __future__ import annotations

from enum import StrEnum


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


class EntityType(StrEnum):

    CONTRACT_TEST_ENTITY = "contract_test_entity"
    CONTRACT_TEST_ENTITY_ALT = "contract_test_entity_alt"


class EventKind(StrEnum):

    CONTRACT_TEST_EVENT = "contract_test_event"
    CONTRACT_TEST_SETTLE_DEBIT = "contract_test_settle_debit"
    CONTRACT_TEST_SETTLE_CREDIT = "contract_test_settle_credit"
    DEMAND_GENERATION = "demand_generation"
    CALL_INVOCATION = "call_invocation"


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
