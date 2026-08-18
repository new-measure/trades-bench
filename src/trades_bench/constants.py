from __future__ import annotations

from typing import Any

ConstantEntry = tuple[str, str, Any, dict[str, Any]]

PHOENIX_TIMEZONE: str = "America/Phoenix"
PHOENIX_SQLITE_UTC_OFFSET: str = "-7 hours"

ZONE_NAMES: tuple[str, ...] = (
    "Central Phoenix",
    "North Phoenix",
    "Scottsdale",
    "Tempe",
    "Mesa",
    "Chandler",
    "Gilbert",
    "Glendale",
    "Peoria",
    "West Valley/Goodyear",
)
assert len(ZONE_NAMES) == 10

OPENING_HOUR: int = 6
OPENING_MINUTE: int = 45
OPENING_TIME: tuple[int, int] = (OPENING_HOUR, OPENING_MINUTE)

CALL_WINDOW_START_MIN: int = 7 * 60
CALL_WINDOW_END_MIN: int = 19 * 60
CALL_WINDOW: tuple[int, int] = (CALL_WINDOW_START_MIN, CALL_WINDOW_END_MIN)

OFFICE_BLOCK_COUNT: int = 5
BLOCK_GRID_MINUTES: tuple[tuple[int, int], ...] = (
    (7 * 60, 9 * 60),
    (9 * 60, 11 * 60),
    (11 * 60, 13 * 60),
    (13 * 60, 15 * 60),
    (15 * 60, 17 * 60),
    (17 * 60, 19 * 60),
)
AFTER_HOURS_BLOCK_INDEX: int = 5

GRACE_MINUTES: int = 10
TIMING_VARIATION_LOW: int = -15
TIMING_VARIATION_MODE: int = 0
TIMING_VARIATION_HIGH: int = 15
TIMING_VARIATION: tuple[int, int, int] = (
    TIMING_VARIATION_LOW,
    TIMING_VARIATION_MODE,
    TIMING_VARIATION_HIGH,
)

INSTALL_BLOCKS: int = 2
INSTALL_MAX_START_MIN: int = 13 * 60

STARTING_TECHNICIANS: int = 4
RECENT_HIRE_TECHNICIAN_ID: int = 2
SICKNESS_DAYS_MIN: int = 1
SICKNESS_DAYS_MAX: int = 3
SICKNESS_DAYS: tuple[int, int] = (SICKNESS_DAYS_MIN, SICKNESS_DAYS_MAX)
WEEKDAY_PAY_MULTIPLIER: int = 10

UNANSWERED_ATTEMPTS: int = 3

WARM_START_START: str = "2026-04-01"
WARM_START_END: str = "2026-09-30"
WARM_START_RANGE: tuple[str, str] = (WARM_START_START, WARM_START_END)

SUPPORTED_CATEGORIES: tuple[str, ...] = (
    "HVAC service",
    "HVAC installation",
    "Plumbing",
    "Electrical",
)

JOB_TYPE_CODES: tuple[str, ...] = (
    "hvac_service",
    "hvac_installation",
    "plumbing",
    "electrical",
    "other",
)


CONSTANT_ENTRIES: list[ConstantEntry] = [
    ("constants.timezone", "string", PHOENIX_TIMEZONE, {}),
    ("constants.zone_names", "string_list", list(ZONE_NAMES), {}),
    ("constants.zone_count", "int", len(ZONE_NAMES), {"min": 1}),
    ("constants.opening_hour", "int", OPENING_HOUR, {"min": 0, "max": 23}),
    ("constants.opening_minute", "int", OPENING_MINUTE, {"min": 0, "max": 59}),
    ("constants.call_window_start_min", "int", CALL_WINDOW_START_MIN, {"min": 0, "max": 1439}),
    ("constants.call_window_end_min", "int", CALL_WINDOW_END_MIN, {"min": 0, "max": 1440}),
    ("constants.office_block_count", "int", OFFICE_BLOCK_COUNT, {"min": 1}),
    ("constants.block_grid", "block_grid", [list(b) for b in BLOCK_GRID_MINUTES], {}),
    ("constants.grace_minutes", "int", GRACE_MINUTES, {"min": 0}),
    ("constants.timing_variation_min", "int", TIMING_VARIATION_LOW, {}),
    ("constants.timing_variation_mode", "int", TIMING_VARIATION_MODE, {}),
    ("constants.timing_variation_max", "int", TIMING_VARIATION_HIGH, {}),
    ("constants.install_blocks", "int", INSTALL_BLOCKS, {"min": 1}),
    ("constants.install_max_start_min", "int", INSTALL_MAX_START_MIN, {"min": 0, "max": 1439}),
    ("constants.starting_technicians", "int", STARTING_TECHNICIANS, {"min": 1}),
    (
        "constants.recent_hire_technician_id",
        "int",
        RECENT_HIRE_TECHNICIAN_ID,
        {
            "min": 1,
            "max": 4,
            "description": "Which of the four starting technicians joined inside the warm-start "
            "window via a completed hiring request.",
        },
    ),
    ("constants.sickness_days_min", "int", SICKNESS_DAYS_MIN, {"min": 1}),
    ("constants.sickness_days_max", "int", SICKNESS_DAYS_MAX, {"min": 1}),
    ("constants.weekday_pay_multiplier", "int", WEEKDAY_PAY_MULTIPLIER, {"min": 0}),
    ("constants.unanswered_attempts", "int", UNANSWERED_ATTEMPTS, {"min": 1}),
    ("constants.warm_start_start", "date_string", WARM_START_START, {}),
    ("constants.warm_start_end", "date_string", WARM_START_END, {}),
]

CONSTANT_VALUES: dict[str, Any] = {
    path: default for path, _type, default, _meta in CONSTANT_ENTRIES
}


__all__ = [
    "AFTER_HOURS_BLOCK_INDEX",
    "BLOCK_GRID_MINUTES",
    "CALL_WINDOW",
    "CALL_WINDOW_END_MIN",
    "CALL_WINDOW_START_MIN",
    "CONSTANT_ENTRIES",
    "CONSTANT_VALUES",
    "ConstantEntry",
    "GRACE_MINUTES",
    "INSTALL_BLOCKS",
    "INSTALL_MAX_START_MIN",
    "JOB_TYPE_CODES",
    "OFFICE_BLOCK_COUNT",
    "OPENING_HOUR",
    "OPENING_MINUTE",
    "OPENING_TIME",
    "PHOENIX_TIMEZONE",
    "RECENT_HIRE_TECHNICIAN_ID",
    "SICKNESS_DAYS",
    "SICKNESS_DAYS_MAX",
    "SICKNESS_DAYS_MIN",
    "STARTING_TECHNICIANS",
    "SUPPORTED_CATEGORIES",
    "TIMING_VARIATION",
    "TIMING_VARIATION_HIGH",
    "TIMING_VARIATION_LOW",
    "TIMING_VARIATION_MODE",
    "UNANSWERED_ATTEMPTS",
    "WARM_START_END",
    "WARM_START_RANGE",
    "WARM_START_START",
    "WEEKDAY_PAY_MULTIPLIER",
    "ZONE_NAMES",
]
