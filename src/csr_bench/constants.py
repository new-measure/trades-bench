"""User-decided constants registry (00-decisions + docs/CSR-001.md decision 3).

The values here are **not scenario knobs**: the 06:45 opening, the call window
``[07:00, 19:00)``, the five office blocks plus 17:00–19:00 after-hours, the
10-minute grace, ``triangular(-15, 0, +15)`` timing variation, two-block
installs never starting after 13:00, four starting technicians, 1–3 day
sickness, ``10 × hourly_wage`` weekday pay, the three-attempt rule, the ten
named Phoenix zones, the 2026-04-01..2026-09-30 warm start, and the
2026-10-01..2027-09-30 ``America/Phoenix`` run calendar.

Every module reads these values from here rather than re-typing a literal, so a
fail-validation-if-overridden check has a single source of truth. The config
registry (see :mod:`csr_bench.config`) mirrors each entry as a ``constant=True``
key, which is why a scenario override fails validation with the dedicated
``constant_override`` error code.

Domain tunables (``finance.*``, ``weather.*``, …) are registered by their owning
tickets (CSR-002+); nothing here touches them.
"""

from __future__ import annotations

from typing import Any

#: Canonical constant spec tuples consumed by :mod:`csr_bench.config` to build
#: the assembled registry. Each entry: (path, type, default, extra_meta).
#: ``extra_meta`` may carry ``min``/``max`` for ints, nothing else for now.
ConstantEntry = tuple[str, str, Any, dict[str, Any]]

PHOENIX_TIMEZONE: str = "America/Phoenix"

#: Ten named Phoenix-area zones (00-decisions §Geography and weather).
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

# --- The 06:45 opening --------------------------------------------------------
OPENING_HOUR: int = 6
OPENING_MINUTE: int = 45
OPENING_TIME: tuple[int, int] = (OPENING_HOUR, OPENING_MINUTE)

# --- Call window [07:00, 19:00) -----------------------------------------------
CALL_WINDOW_START_MIN: int = 7 * 60
CALL_WINDOW_END_MIN: int = 19 * 60
CALL_WINDOW: tuple[int, int] = (CALL_WINDOW_START_MIN, CALL_WINDOW_END_MIN)

# --- Block grid: five office blocks + after-hours, as minute pairs ------------
# 07:00–09:00, 09:00–11:00, 11:00–13:00, 13:00–15:00, 15:00–17:00, 17:00–19:00.
OFFICE_BLOCK_COUNT: int = 5
BLOCK_GRID_MINUTES: tuple[tuple[int, int], ...] = (
    (7 * 60, 9 * 60),
    (9 * 60, 11 * 60),
    (11 * 60, 13 * 60),
    (13 * 60, 15 * 60),
    (15 * 60, 17 * 60),
    (17 * 60, 19 * 60),
)
AFTER_HOURS_BLOCK_INDEX: int = 5  # zero-based index of the after-hours block in the grid

# --- 10-minute grace, triangular timing variation -----------------------------
GRACE_MINUTES: int = 10
TIMING_VARIATION_LOW: int = -15
TIMING_VARIATION_MODE: int = 0
TIMING_VARIATION_HIGH: int = 15
TIMING_VARIATION: tuple[int, int, int] = (
    TIMING_VARIATION_LOW,
    TIMING_VARIATION_MODE,
    TIMING_VARIATION_HIGH,
)

# --- Installs: two consecutive office blocks, never starting after 13:00 -------
INSTALL_BLOCKS: int = 2
INSTALL_MAX_START_MIN: int = 13 * 60

# --- Workforce constants ------------------------------------------------------
STARTING_TECHNICIANS: int = 4
SICKNESS_DAYS_MIN: int = 1
SICKNESS_DAYS_MAX: int = 3
SICKNESS_DAYS: tuple[int, int] = (SICKNESS_DAYS_MIN, SICKNESS_DAYS_MAX)
WEEKDAY_PAY_MULTIPLIER: int = 10

# --- Three-attempt unanswered rule (00-decisions §Daily interaction loop) ----
UNANSWERED_ATTEMPTS: int = 3

# --- Calendars ---------------------------------------------------------------
WARM_START_START: str = "2026-04-01"
WARM_START_END: str = "2026-09-30"
RUN_START: str = "2026-10-01"
RUN_END: str = "2027-09-30"
WARM_START_RANGE: tuple[str, str] = (WARM_START_START, WARM_START_END)
RUN_CALENDAR: tuple[str, str] = (RUN_START, RUN_END)


#: A typed, declarative description of every user-decided constant. The config
#: registry is assembled from this; each entry is ``constant=True`` and therefore
#: cannot be overridden by a scenario.
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
    ("constants.sickness_days_min", "int", SICKNESS_DAYS_MIN, {"min": 1}),
    ("constants.sickness_days_max", "int", SICKNESS_DAYS_MAX, {"min": 1}),
    ("constants.weekday_pay_multiplier", "int", WEEKDAY_PAY_MULTIPLIER, {"min": 0}),
    ("constants.unanswered_attempts", "int", UNANSWERED_ATTEMPTS, {"min": 1}),
    ("constants.warm_start_start", "date_string", WARM_START_START, {}),
    ("constants.warm_start_end", "date_string", WARM_START_END, {}),
    ("constants.run_start", "date_string", RUN_START, {}),
    ("constants.run_end", "date_string", RUN_END, {}),
]

#: Mapping ``path -> default value`` for convenient access from other modules.
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
    "OFFICE_BLOCK_COUNT",
    "OPENING_HOUR",
    "OPENING_MINUTE",
    "OPENING_TIME",
    "PHOENIX_TIMEZONE",
    "RUN_CALENDAR",
    "RUN_END",
    "RUN_START",
    "SICKNESS_DAYS",
    "SICKNESS_DAYS_MAX",
    "SICKNESS_DAYS_MIN",
    "STARTING_TECHNICIANS",
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
