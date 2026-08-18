from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

from .constants import CONSTANT_ENTRIES, JOB_TYPE_CODES, SUPPORTED_CATEGORIES
from .enums import ConditionCode
from .errors import EngineError, ErrorCode

DEFAULT_CONFIG_VERSION: str = "1.0.0"

U64_MOD: int = 1 << 64

_THRESHOLD_PREC = 60

_NUL = chr(0)


@dataclass(frozen=True)
class KeySpec:

    path: str
    type: str
    default: Any
    constant: bool = False
    deferred: str | None = None
    min: int | None = None
    max: int | None = None
    length: int | None = None
    description: str = ""


_DOMAIN_ENTRIES: list[Any] = [
    (
        "run.start_date",
        "date_string",
        "2026-10-01",
        {
            "description": "First service date of the run calendar; must fall strictly after "
            "constants.warm_start_end so the warm-start boundary stays clean.",
        },
    ),
    (
        "run.end_date",
        "date_string",
        "2026-10-14",
        {
            "description": "Last service date of the run calendar; must not precede "
            "run.start_date. Default is a two-week run.",
        },
    ),
    (
        "finance.starting_cash",
        "cost",
        20_000_000,
        {
            "deferred": "provisional",
            "description": "Starting cash (integer cents) the warm-start opening adjustment pins "
            "the ledger to.",
        },
    ),
    (
        "finance.daily_operating_overhead",
        "cost",
        30_000,
        {
            "deferred": "provisional",
            "description": "Aggregate daily operating overhead (integer cents) posted by every "
            "daily settlement, with no internal vendor breakdown.",
        },
    ),
    (
        "hiring.base_hourly_wage_by_trade",
        "cost_map",
        {"hvac_service": 3500, "hvac_install": 4000, "plumbing": 3200, "electrical": 3400},
        {
            "deferred": "provisional",
            "description": "Base hourly wage in cents/hour per qualification code.",
        },
    ),
    (
        "payroll.off_hours_bonus_per_job",
        "cost",
        5000,
        {
            "deferred": "provisional",
            "description": "Flat bonus in cents posted alongside off-hours wages for each "
            "off-hours accrual mark that carries an assignee.",
        },
    ),
    (
        "geography.artifact_id",
        "string",
        "arizona-v1",
        {
            "description": "Identity of the packaged geography artifact.",
        },
    ),
    (
        "geography.artifact_sha256",
        "string",
        "5e526973b9a2b58641102bba760daa0b8b8b06701bc9264106e5c156cc0b92ec",
        {
            "description": "SHA-256 of the packaged geography artifact, verified at run "
            "initialization.",
        },
    ),
    (
        "geography.min_address_catalog_size",
        "int",
        30_000,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Floor on usable catalog rows.",
        },
    ),
    (
        "geography.address_catalog_safety_factor",
        "decimal",
        Decimal("3.0"),
        {
            "deferred": "provisional",
            "description": "Multiplier over projected annual distinct new-caller addresses.",
        },
    ),
    (
        "geography.min_addresses_per_zone",
        "int",
        1500,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Per-zone usable-address floor.",
        },
    ),
    (
        "geography.min_addresses_per_zone_segment",
        "int",
        100,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Per-zone, per-segment usable-address floor.",
        },
    ),
    (
        "geography.min_outlying_addresses",
        "int",
        1000,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Outlying-ring usable-address floor.",
        },
    ),
    (
        "geography.outlying_buffer_km",
        "decimal",
        Decimal("15.0"),
        {
            "deferred": "provisional",
            "description": "Crop buffer beyond the service core.",
        },
    ),
    (
        "geography.max_snap_distance_meters",
        "int",
        150,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Address to road-network snap tolerance.",
        },
    ),
    (
        "geography.duplicate_coordinate_meters",
        "decimal",
        Decimal("10.0"),
        {
            "deferred": "provisional",
            "description": "Coordinate-proximity dedupe radius.",
        },
    ),
    (
        "geography.min_route_distance_km",
        "decimal",
        Decimal("0.5"),
        {
            "deferred": "provisional",
            "description": "Floor on any route distance.",
        },
    ),
    (
        "geography.intra_cell_detour_factor",
        "decimal",
        Decimal("1.3"),
        {
            "deferred": "provisional",
            "description": "Same-anchor distance factor.",
        },
    ),
    (
        "geography.distance_model_max_median_error_pct",
        "decimal",
        Decimal("10.0"),
        {
            "deferred": "provisional",
            "description": "Packaging accuracy gate versus exact routed distances, median.",
        },
    ),
    (
        "geography.distance_model_max_p95_error_pct",
        "decimal",
        Decimal("25.0"),
        {
            "deferred": "provisional",
            "description": "Packaging accuracy gate versus exact routed distances, p95.",
        },
    ),
    (
        "geography.gps_ping_interval_minutes",
        "int",
        5,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Quantization of the public live-location row. min=1: this is a divisor "
            "in the quantization, so 0 would divide by zero.",
        },
    ),
    (
        "travel.time_of_day_bands",
        "time_of_day_bands",
        [
            ["night", 0, 360],
            ["morning_peak", 360, 540],
            ["midday", 540, 900],
            ["evening_peak", 900, 1080],
            ["evening", 1080, 1260],
            ["night_late", 1260, 1440],
        ],
        {
            "deferred": "provisional",
            "description": "Ordered contiguous partition of [00:00, 24:00).",
        },
    ),
    (
        "travel.speed_by_time_band",
        "speed_by_time_band",
        {
            "weekday": {
                "night": 62,
                "morning_peak": 38,
                "midday": 52,
                "evening_peak": 34,
                "evening": 50,
                "night_late": 60,
            },
            "weekend": {
                "night": 60,
                "morning_peak": 52,
                "midday": 55,
                "evening_peak": 50,
                "evening": 52,
                "night_late": 60,
            },
        },
        {
            "deferred": "provisional",
            "description": "km/h by day_type x band.",
        },
    ),
    (
        "travel.weather_travel_multipliers",
        "weather_multiplier_map",
        {
            "clear": Decimal("1.00"),
            "cool": Decimal("1.00"),
            "hot": Decimal("1.02"),
            "windy": Decimal("1.05"),
            "extreme_heat": Decimal("1.08"),
            "rain": Decimal("1.15"),
            "storm": Decimal("1.40"),
        },
        {
            "deferred": "provisional",
            "description": "Multiplier by the closed condition_code enum; total over every member.",
        },
    ),
    (
        "travel.noise.sigma",
        "decimal",
        Decimal("0.15"),
        {
            "deferred": "provisional",
            "description": "Log-normal shape, median 1.",
        },
    ),
    (
        "travel.noise.min_multiplier",
        "decimal",
        Decimal("0.75"),
        {
            "deferred": "provisional",
            "description": "Noise clamp floor, strictly positive.",
        },
    ),
    (
        "travel.noise.max_multiplier",
        "decimal",
        Decimal("1.50"),
        {
            "deferred": "provisional",
            "description": "Noise clamp ceiling.",
        },
    ),
    (
        "travel.min_travel_minutes",
        "int",
        5,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Floor on any realized trip.",
        },
    ),
    (
        "appointments.min_actual_duration_minutes",
        "int",
        30,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Floor on realized service duration.",
        },
    ),
    (
        "appointments.min_duration_multiplier",
        "decimal",
        Decimal("0.25"),
        {
            "deferred": "provisional",
            "description": "Floor on the duration multiplier.",
        },
    ),
    (
        "quality.duration_skill_coefficient",
        "decimal",
        Decimal("0"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Weight applied to the skill term of the execution duration "
            "multiplier. The skill input is fixed at zero until the hidden quality model "
            "ships.",
        },
    ),
    (
        "quality.duration_strain_coefficient",
        "decimal",
        Decimal("0"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Weight applied to the strain term of the execution duration "
            "multiplier. The strain input is fixed at zero until the hidden quality model "
            "ships.",
        },
    ),
    (
        "quality.duration_complexity_coefficient",
        "decimal",
        Decimal("0"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Weight applied to the complexity term of the execution duration "
            "multiplier. The complexity input is fixed at zero until the hidden quality "
            "model ships.",
        },
    ),
    (
        "quality.complexity_distribution",
        "decimal_map_map",
        {
            "hvac_service": {"min": Decimal("0.30"), "max": Decimal("0.70")},
            "hvac_installation": {"min": Decimal("0.50"), "max": Decimal("0.90")},
            "plumbing": {"min": Decimal("0.30"), "max": Decimal("0.70")},
            "electrical": {"min": Decimal("0.30"), "max": Decimal("0.70")},
            "other": {"min": Decimal("0.20"), "max": Decimal("0.60")},
        },
        {
            "deferred": "provisional",
            "description": "Per-job-type uniform range for the hidden complexity draw made "
            "once at job creation; installations skew higher.",
        },
    ),
    (
        "weather.precompute_start_date",
        "date_string",
        "2026-04-01",
        {
            "deferred": "provisional",
            "description": "Start of the once-at-init realized-weather precompute span; must equal "
            "constants.warm_start_start.",
        },
    ),
    (
        "weather.precompute_end_date",
        "date_string",
        "2026-10-21",
        {
            "deferred": "provisional",
            "description": "End of the once-at-init realized-weather precompute span; must reach "
            "run.end_date + weather.forecast_max_horizon_days.",
        },
    ),
    (
        "weather.seasonal_mean_high_f",
        "decimal",
        87,
        {
            "deferred": "provisional",
            "description": "Metro seasonal-term mean daily high, °F.",
        },
    ),
    (
        "weather.seasonal_amplitude_f_range",
        "int_range",
        {"min": 18, "max": 22},
        {
            "deferred": "provisional",
            "description": "Per-run draw range for the seasonal amplitude `A`.",
        },
    ),
    (
        "weather.seasonal_phase_shift_days_range",
        "int_range",
        {"min": 88, "max": 102},
        {
            "deferred": "provisional",
            "description": "Per-run draw range for the seasonal phase shift `φ`, in days.",
        },
    ),
    (
        "weather.daily_noise_sd_f",
        "decimal",
        Decimal("3.5"),
        {
            "deferred": "provisional",
            "description": "Standard deviation of the daily mean-reverting noise term ε(d), °F.",
        },
    ),
    (
        "weather.mean_reversion_rate",
        "probability",
        Decimal("0.7"),
        {
            "deferred": "provisional",
            "description": "Day-over-day mean-reversion coefficient of the noise process x(d).",
        },
    ),
    (
        "weather.zone_offset_f",
        "decimal_list",
        [1, -1, 0, 1, 0, 0, 0, 1, 0, 2],
        {
            "length": 10,
            "deferred": "provisional",
            "description": "Per-zone additive offset to the metro daily high, °F, one entry per "
            "constants.zone_names ordinal.",
        },
    ),
    (
        "weather.diurnal_range_f",
        "decimal",
        25,
        {
            "deferred": "provisional",
            "description": "high_f minus low_f for every zone-day.",
        },
    ),
    (
        "weather.monsoon_window_start",
        "month_day_string",
        "06-15",
        {
            "deferred": "provisional",
            "description": "First MM-DD of the elevated in-window storm-onset probability.",
        },
    ),
    (
        "weather.monsoon_window_end",
        "month_day_string",
        "09-30",
        {
            "deferred": "provisional",
            "description": "Last MM-DD of the elevated in-window storm-onset probability.",
        },
    ),
    (
        "weather.storm_daily_probability_in_window",
        "probability",
        Decimal("0.20"),
        {
            "deferred": "provisional",
            "description": "Daily storm onset probability inside the monsoon window.",
        },
    ),
    (
        "weather.storm_daily_probability_out_of_window",
        "probability",
        Decimal("0.03"),
        {
            "deferred": "provisional",
            "description": "Daily storm onset probability outside the monsoon window.",
        },
    ),
    (
        "weather.storm_zone_correlation",
        "probability",
        Decimal("0.6"),
        {
            "deferred": "provisional",
            "description": "Weight of the shared metro storm driver in each zone's storm process.",
        },
    ),
    (
        "weather.storm_persistence",
        "probability",
        Decimal("0.3"),
        {
            "deferred": "provisional",
            "description": "Day-to-day persistence of a zone's storm state.",
        },
    ),
    (
        "weather.storm_severity_range",
        "decimal_range",
        {"min": Decimal("0.2"), "max": Decimal("1.0")},
        {
            "deferred": "provisional",
            "description": "Numeric range storm_severity is drawn from on a storm day.",
        },
    ),
    (
        "weather.storm_severity_bands",
        "severity_bands",
        [
            ["light", Decimal("0.2"), Decimal("0.5")],
            ["moderate", Decimal("0.5"), Decimal("0.8")],
            ["severe", Decimal("0.8"), Decimal("1.0")],
        ],
        {
            "deferred": "provisional",
            "description": "Named bands partitioning weather.storm_severity_range exactly; names "
            "the public severity_band field.",
        },
    ),
    (
        "weather.precipitation_by_severity",
        "decimal_map",
        {"none": 0, "light": 2, "moderate": 8, "severe": 20},
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Realized precipitation_mm keyed by storm severity band, plus 'none'.",
        },
    ),
    (
        "weather.wind_by_severity",
        "decimal_map",
        {"none": 8, "light": 15, "moderate": 25, "severe": 40},
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Realized wind_mph keyed by storm severity band, plus 'none'.",
        },
    ),
    (
        "weather.condition_code_thresholds",
        "decimal_map",
        {
            "extreme_heat_high_f": 110,
            "hot_high_f": 100,
            "cool_high_f": 70,
            "windy_wind_mph": 25,
            "rain_precipitation_mm": Decimal("0.1"),
        },
        {
            "deferred": "provisional",
            "description": "Thresholds the engine uses to derive the closed condition_code enum "
            "from the stored numeric fields.",
        },
    ),
    (
        "weather.precipitation_probability_by_condition",
        "probability_map",
        {
            "clear": Decimal("0.02"),
            "cool": Decimal("0.05"),
            "hot": Decimal("0.02"),
            "windy": Decimal("0.10"),
            "extreme_heat": Decimal("0.02"),
            "rain": Decimal("0.8"),
            "storm": Decimal("0.95"),
        },
        {
            "deferred": "provisional",
            "description": "Public precipitation_probability keyed by condition_code; Must cover "
            "the ConditionCode enum exactly.",
        },
    ),
    (
        "weather.seasonal_period_days",
        "int",
        365,
        {
            "constant": True,
            "description": "Calendar year length of the seasonal term; a normative constant, not "
            "scenario-tunable.",
        },
    ),
    (
        "weather.forecast_max_horizon_days",
        "int",
        7,
        {
            "constant": True,
            "description": "Furthest date weather.get accepts beyond today; a normative constant, "
            "not scenario-tunable.",
        },
    ),
    (
        "weather.forecast_error_scale_by_horizon",
        "decimal_list",
        [0, 2, 3, 4, 5, 6, 7, 8],
        {
            "length": 8,
            "min": 0,
            "deferred": "provisional",
            "description": "Forecast temperature-noise scale by horizon day 0..7; must be "
            "nondecreasing with [0] == 0.",
        },
    ),
    (
        "weather.forecast_storm_flip_probability_by_horizon",
        "probability_list",
        [
            0,
            Decimal("0.05"),
            Decimal("0.08"),
            Decimal("0.12"),
            Decimal("0.15"),
            Decimal("0.18"),
            Decimal("0.20"),
            Decimal("0.22"),
        ],
        {
            "length": 8,
            "deferred": "provisional",
            "description": "Forecast storm-flip Bernoulli probability by horizon day 0..7; must be "
            "nondecreasing with [0] == 0.",
        },
    ),
    (
        "demand.base_new_caller_rate_per_day",
        "decimal",
        10,
        {
            "deferred": "provisional",
            "description": "Metro-wide base new-caller Poisson rate before multipliers.",
        },
    ),
    (
        "demand.zone_weights",
        "decimal_list",
        [
            Decimal("0.20"),
            Decimal("0.20"),
            Decimal("0.075"),
            Decimal("0.015"),
            Decimal("0.095"),
            Decimal("0.085"),
            Decimal("0.095"),
            Decimal("0.075"),
            Decimal("0.075"),
            Decimal("0.085"),
        ],
        {
            "length": 10,
            "min": 0,
            "deferred": "provisional",
            "description": "Per-zone share of new-caller volume, one entry per "
            "constants.zone_names ordinal.",
        },
    ),
    (
        "demand.segment_weights",
        "decimal_map",
        {"residential": Decimal("0.62"), "commercial": Decimal("0.23"), "apartment": Decimal("0.15")},
        {
            "min": 0,
            "deferred": "provisional",
            "description": "New-caller segment mix at generation.",
        },
    ),
    (
        "demand.category_weights",
        "decimal_map_map",
        {
            "residential": {
                "HVAC service": Decimal("0.44"),
                "Plumbing": Decimal("0.33"),
                "Electrical": Decimal("0.16"),
                "HVAC installation": Decimal("0.07"),
                "Appliance repair": Decimal("0.35"),
                "Garage-door repair": Decimal("0.15"),
                "Roofing": Decimal("0.10"),
                "Pool service": Decimal("0.15"),
                "Pest control": Decimal("0.15"),
                "Landscaping": Decimal("0.10"),
            },
            "commercial": {
                "HVAC service": Decimal("0.52"),
                "Plumbing": Decimal("0.27"),
                "Electrical": Decimal("0.17"),
                "HVAC installation": Decimal("0.04"),
                "Appliance repair": Decimal("0.35"),
                "Garage-door repair": Decimal("0.15"),
                "Roofing": Decimal("0.10"),
                "Pool service": Decimal("0.15"),
                "Pest control": Decimal("0.15"),
                "Landscaping": Decimal("0.10"),
            },
            "apartment": {
                "HVAC service": Decimal("0.40"),
                "Plumbing": Decimal("0.38"),
                "Electrical": Decimal("0.15"),
                "HVAC installation": Decimal("0.07"),
                "Appliance repair": Decimal("0.35"),
                "Garage-door repair": Decimal("0.15"),
                "Roofing": Decimal("0.10"),
                "Pool service": Decimal("0.15"),
                "Pest control": Decimal("0.15"),
                "Landscaping": Decimal("0.10"),
            },
        },
        {
            "deferred": "provisional",
            "description": "Relative requested-category weight per segment, over the full catalog; "
            "unsupported and supported are separate relative-weight groups split by "
            "demand.unsupported_category_share.",
        },
    ),
    (
        "demand.unsupported_category_share",
        "probability",
        Decimal("0.12"),
        {
            "deferred": "provisional",
            "description": "Share of new-caller requests drawn from the unsupported category "
            "group.",
        },
    ),
    (
        "demand.day_of_week_multipliers",
        "decimal_list",
        [
            Decimal("1.45"),
            Decimal("1.34"),
            Decimal("1.28"),
            Decimal("1.33"),
            Decimal("1.20"),
            Decimal("0.29"),
            Decimal("0.11"),
        ],
        {
            "length": 7,
            "min": 0,
            "deferred": "provisional",
            "description": "Day-of-week multiplier on new-caller and repeat-call volume, "
            "Monday..Sunday.",
        },
    ),
    (
        "demand.service_catalog_version",
        "string",
        "1.0.0",
        {
            "description": "Version tag of the fixed requested-category catalog.",
        },
    ),
    (
        "demand.unsupported_categories",
        "string_list",
        [
            "Appliance repair",
            "Garage-door repair",
            "Roofing",
            "Pool service",
            "Pest control",
            "Landscaping",
        ],
        {
            "description": "Named unsupported catalog categories.",
        },
    ),
    (
        "demand.outlying_address_share",
        "probability",
        Decimal("0.03"),
        {
            "deferred": "provisional",
            "description": "Probability a new-caller address draw comes from the outlying pool "
            "rather than the in-zone pool.",
        },
    ),
    (
        "demand.heat_threshold_f",
        "decimal",
        85,
        {
            "deferred": "provisional",
            "description": "high_f above which the heat demand term activates.",
        },
    ),
    (
        "demand.cold_threshold_f",
        "decimal",
        45,
        {
            "deferred": "provisional",
            "description": "low_f below which the cold demand term activates.",
        },
    ),
    (
        "demand.temperature_normalization_f",
        "decimal",
        20,
        {
            "deferred": "provisional",
            "description": "°F per unit of heat/cold sensitivity; not a normative constant.",
        },
    ),
    (
        "demand.heat_sensitivity_by_category",
        "decimal_map",
        {
            "HVAC service": 1,
            "HVAC installation": Decimal("0.8"),
            "Plumbing": Decimal("0.1"),
            "Electrical": Decimal("0.3"),
        },
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Per-category heat_term coefficient, over the four supported "
            "categories.",
        },
    ),
    (
        "demand.cold_sensitivity_by_category",
        "decimal_map",
        {
            "HVAC service": Decimal("0.3"),
            "HVAC installation": 0,
            "Plumbing": 0,
            "Electrical": 0,
        },
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Per-category cold_term coefficient, over the four supported "
            "categories.",
        },
    ),
    (
        "demand.storm_multiplier_by_category",
        "decimal_map",
        {
            "HVAC service": Decimal("0.2"),
            "HVAC installation": Decimal("0.1"),
            "Plumbing": Decimal("0.8"),
            "Electrical": 1,
        },
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Per-category storm_term coefficient, over the four supported "
            "categories.",
        },
    ),
    (
        "demand.weather_multiplier_min",
        "decimal",
        Decimal("0.5"),
        {
            "deferred": "provisional",
            "description": "Clamp floor of M_weather(z,d,c).",
        },
    ),
    (
        "demand.weather_multiplier_max",
        "decimal",
        3,
        {
            "deferred": "provisional",
            "description": "Clamp ceiling of M_weather(z,d,c).",
        },
    ),
    (
        "demand.reputation_neutral_stars",
        "decimal",
        Decimal("4.2"),
        {
            "deferred": "provisional",
            "description": "Star average at which M_reputation is 1.",
        },
    ),
    (
        "demand.reputation_sensitivity",
        "decimal",
        Decimal("0.25"),
        {
            "deferred": "provisional",
            "description": "Coefficient of (public_star_average - neutral_stars) in M_reputation.",
        },
    ),
    (
        "demand.reputation_window_days",
        "int",
        90,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Trailing window over published reviews for public_star_average.",
        },
    ),
    (
        "demand.reputation_multiplier_min",
        "decimal",
        Decimal("0.6"),
        {
            "deferred": "provisional",
            "description": "Clamp floor of M_reputation.",
        },
    ),
    (
        "demand.reputation_multiplier_max",
        "decimal",
        Decimal("1.15"),
        {
            "deferred": "provisional",
            "description": "Clamp ceiling of M_reputation.",
        },
    ),
    (
        "demand.word_of_mouth_sensitivity",
        "decimal",
        Decimal("0.5"),
        {
            "deferred": "provisional",
            "description": "Coefficient of the segment-weighted wom sum in M_word_of_mouth.",
        },
    ),
    (
        "demand.word_of_mouth_decay",
        "probability",
        Decimal("0.1"),
        {
            "deferred": "provisional",
            "description": "Sole smoothing/mean-reversion rate of the per-segment wom state "
            "update.",
        },
    ),
    (
        "demand.word_of_mouth_positive_weight",
        "decimal",
        1,
        {
            "deferred": "provisional",
            "description": "Weight `w` applied when a terminal appointment's satisfaction delta δ "
            "≥ 0.",
        },
    ),
    (
        "demand.word_of_mouth_negative_weight",
        "decimal",
        2,
        {
            "deferred": "provisional",
            "description": "Weight `w` applied when a terminal appointment's satisfaction delta δ "
            "< 0; Must be ≥ the positive weight.",
        },
    ),
    (
        "demand.word_of_mouth_segment_weights",
        "decimal_map",
        {
            "apartment": Decimal("0.5"),
            "commercial": Decimal("0.25"),
            "residential": Decimal("0.25"),
        },
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Per-segment weight in the M_word_of_mouth sum; apartment Must carry "
            "the largest weight.",
        },
    ),
    (
        "demand.word_of_mouth_multiplier_min",
        "decimal",
        Decimal("0.7"),
        {
            "deferred": "provisional",
            "description": "Clamp floor of M_word_of_mouth.",
        },
    ),
    (
        "demand.word_of_mouth_multiplier_max",
        "decimal",
        Decimal("1.3"),
        {
            "deferred": "provisional",
            "description": "Clamp ceiling of M_word_of_mouth.",
        },
    ),
    (
        "demand.arrival_time_of_day_weights",
        "decimal_list",
        [30, 91, 120, 127, 123, 104, 107, 100, 91, 64, 29, 14],
        {
            "length": 12,
            "min": 0,
            "deferred": "provisional",
            "description": "Categorical weight per call hour 07..18; Must have a positive sum.",
        },
    ),
    (
        "demand.office_hours_urgent_probability",
        "probability",
        Decimal("0.10"),
        {
            "deferred": "provisional",
            "description": "Base urgency probability for calls arriving before 17:00.",
        },
    ),
    (
        "demand.after_hours_urgent_probability",
        "probability",
        Decimal("0.30"),
        {
            "deferred": "provisional",
            "description": "Base urgency probability for calls arriving at or after 17:00; Must be "
            "≥ the office-hours base.",
        },
    ),
    (
        "demand.urgency_weather_bonus",
        "decimal",
        Decimal("0.15"),
        {
            "deferred": "provisional",
            "description": "Coefficient of max(0, M_weather - 1) added to p_urgent.",
        },
    ),
    (
        "demand.repeat_call_daily_base_probability",
        "probability",
        Decimal("0.004"),
        {
            "deferred": "provisional",
            "description": "Per-customer daily Bernoulli base rate before propensity and "
            "multipliers.",
        },
    ),
    (
        "demand.repeat_propensity_initial_by_segment",
        "decimal_map",
        {
            "residential": Decimal("0.8"),
            "apartment": Decimal("1.2"),
            "commercial": Decimal("1.5"),
        },
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Initial repeat-call propensity at customer creation, by segment.",
        },
    ),
    (
        "demand.rejection_repeat_propensity_multiplier",
        "decimal",
        Decimal("0.7"),
        {
            "deferred": "provisional",
            "description": "Multiplier applied to repeat propensity on every ordinary rejection; "
            "Must lie strictly inside (0, 1).",
        },
    ),
    (
        "demand.satisfaction_repeat_propensity_coefficient",
        "decimal",
        Decimal("0.15"),
        {
            "deferred": "provisional",
            "description": "Coefficient of the hidden satisfaction delta δ in the outcome-driven "
            "propensity update.",
        },
    ),
    (
        "demand.existing_customer_new_location_probability",
        "probability",
        Decimal("0.05"),
        {
            "deferred": "provisional",
            "description": "Probability an existing-customer call requests a fresh catalog address "
            "rather than a stored location.",
        },
    ),
    (
        "demand.multi_location_probability_by_segment",
        "probability_map",
        {
            "residential": Decimal("0.03"),
            "apartment": Decimal("0.45"),
            "commercial": Decimal("0.30"),
        },
        {
            "deferred": "provisional",
            "description": "Per-segment probability a customer gets more than one location at "
            "generation.",
        },
    ),
    (
        "demand.returning_prospect_probability",
        "probability",
        Decimal("0.10"),
        {
            "deferred": "provisional",
            "description": "Probability a new-caller slot is flagged a returning prospect, subject "
            "to a matching prospect existing.",
        },
    ),
    (
        "acceptance.base_probability_by_urgency",
        "probability_map",
        {"standard": Decimal("0.90"), "urgent": Decimal("0.95")},
        {
            "deferred": "provisional",
            "description": "`base` of p_accept by urgency class.",
        },
    ),
    (
        "acceptance.lead_time_decay_by_urgency",
        "decimal_map",
        {"standard": Decimal("0.15"), "urgent": Decimal("0.50")},
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Exponential lead-time decay rate of p_accept by urgency class.",
        },
    ),
    (
        "acceptance.floor_probability_by_urgency",
        "probability_map",
        {"standard": Decimal("0.25"), "urgent": Decimal("0.05")},
        {
            "deferred": "provisional",
            "description": "Floor of p_accept by urgency class; Must be ≤ base.",
        },
    ),
    (
        "cancellations.base_probability_by_segment",
        "probability_map",
        {"residential": Decimal("0.08"), "commercial": Decimal("0.05"), "apartment": Decimal("0.06")},
        {
            "deferred": "provisional",
            "description": "Base p_cancel by customer segment.",
        },
    ),
    (
        "cancellations.urgent_multiplier",
        "decimal",
        Decimal("0.5"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "p_cancel multiplier applied when the booking is urgent.",
        },
    ),
    (
        "cancellations.lead_days_coefficient",
        "decimal",
        Decimal("0.10"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Coefficient of lead_days in the p_cancel linear term.",
        },
    ),
    (
        "cancellations.storm_multiplier",
        "decimal",
        Decimal("1.4"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "p_cancel multiplier applied when the service date is a storm day at "
            "the location's effective zone.",
        },
    ),
    (
        "cancellations.cancellation_cutoff_minutes",
        "int",
        60,
        {
            "min": 15,
            "deferred": "provisional",
            "description": "Minutes before scheduled_start after which no cancellation arrival may "
            "be timestamped; Must be ≥ 15.",
        },
    ),
    (
        "competitors.attempt_probability",
        "probability",
        Decimal("0.5"),
        {
            "deferred": "provisional",
            "description": "Per-rejection Bernoulli probability of a competitor attempt.",
        },
    ),
    (
        "competitors.served_well_probability",
        "probability",
        Decimal("0.65"),
        {
            "deferred": "provisional",
            "description": "Given a competitor attempt, probability the customer is served well.",
        },
    ),
    (
        "competitors.served_well_repeat_multiplier",
        "decimal",
        Decimal("0.5"),
        {
            "deferred": "provisional",
            "description": "Repeat-propensity multiplier when served well by a competitor; Must "
            "lie strictly inside (0, 1).",
        },
    ),
    (
        "competitors.share_gain_per_capture",
        "decimal",
        Decimal("0.002"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Additive competitor_share gain per successful capture.",
        },
    ),
    (
        "competitors.share_decay_per_day",
        "decimal",
        Decimal("0.005"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Daily decay of competitor_share toward baseline when share recovery is "
            "enabled.",
        },
    ),
    (
        "competitors.share_recovery_enabled",
        "bool",
        False,
        {
            "deferred": "provisional",
            "description": "Whether competitor_share decays toward baseline; default off, "
            "erode-only.",
        },
    ),
    (
        "competitors.demand_erosion_coefficient",
        "decimal",
        Decimal("0.5"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Coefficient of competitor_share in the M_competitor demand erosion "
            "term.",
        },
    ),
    (
        "competitors.demand_multiplier_min",
        "decimal",
        Decimal("0.7"),
        {
            "deferred": "provisional",
            "description": "Clamp floor of M_competitor; Must lie in (0, 1].",
        },
    ),
    (
        "competitors.background_drift_sd",
        "decimal",
        Decimal("0.01"),
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Standard deviation of the per-date competitor_background drift step.",
        },
    ),
    (
        "customers.repeat_propensity_floor",
        "decimal",
        Decimal("0.05"),
        {
            "min": Decimal("0.000001"),
            "deferred": "provisional",
            "description": "Clamp floor of repeat-call propensity; Must be > 0 so no customer is "
            "ever permanently lost.",
        },
    ),
    (
        "customers.repeat_propensity_max",
        "decimal",
        3,
        {
            "deferred": "provisional",
            "description": "Clamp ceiling of repeat-call propensity.",
        },
    ),
    (
        "customers.person_name_pool_version",
        "string",
        "person-names-v1",
        {
            "description": "Version tag of the fixed person-name identity pool.",
        },
    ),
    (
        "customers.business_name_pool_version",
        "string",
        "business-names-v1",
        {
            "description": "Version tag of the fixed business-name identity pool.",
        },
    ),
    (
        "customers.phone_prefix_pool",
        "string_list",
        ["480555", "602555", "623555"],
        {
            "description": "Reserved fictional phone-number prefixes callers are drawn from.",
        },
    ),
    (
        "interface.lock_acquire_timeout_seconds",
        "int",
        5,
        {
            "min": 0,
            "deferred": "provisional",
            "description": "Wall-clock seconds a request waits to acquire the single global "
            "engine lock before it fails busy rather than change anything.",
        },
    ),
    (
        "interface.max_inflight_requests",
        "int",
        16,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Cap on requests accepted but not yet started; requests beyond "
            "the cap fail busy instead of buffering without bound.",
        },
    ),
    (
        "interface.request_deadline_seconds",
        "int",
        30,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Wall-clock seconds a single request may run before the engine "
            "aborts the work, rolls back, and reports the deadline as expired.",
        },
    ),
    (
        "interface.sql_statement_timeout_seconds",
        "int",
        10,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Wall-clock seconds an agent SQL statement may run before an "
            "interrupt handler aborts it mid-execution.",
        },
    ),
    (
        "interface.sql_max_rows",
        "int",
        5000,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Row cap on a single query result; a caller-supplied max_rows "
            "argument may only lower it.",
        },
    ),
    (
        "interface.sql_max_response_bytes",
        "int",
        2_000_000,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Byte cap on a single query response body; a response over the "
            "cap is truncated on a row boundary.",
        },
    ),
    (
        "interface.error_echo_max_chars",
        "int",
        200,
        {
            "min": 1,
            "deferred": "provisional",
            "description": "Maximum length of an agent-supplied value echoed back inside a "
            "structured error's details.",
        },
    ),
]


def assemble_registry(
    *,
    extra_entries: list[Any] | None = None,
) -> dict[str, KeySpec]:
    registry: dict[str, KeySpec] = {}

    def _add(entry: object) -> None:
        if isinstance(entry, KeySpec):
            spec = entry
        else:
            path, type_, default, meta = entry  # type: ignore[misc]
            spec = KeySpec(
                path=path,
                type=type_,
                default=default,
                constant=bool(meta.get("constant", False)) if isinstance(meta, dict) else False,
                deferred=meta.get("deferred") if isinstance(meta, dict) else None,
                min=meta.get("min") if isinstance(meta, dict) else None,
                max=meta.get("max") if isinstance(meta, dict) else None,
                length=meta.get("length") if isinstance(meta, dict) else None,
                description=meta.get("description", "") if isinstance(meta, dict) else "",
            )
        if spec.path in registry:
            raise ValueError(f"duplicate config key {spec.path!r}")
        registry[spec.path] = spec

    for entry in CONSTANT_ENTRIES:
        path, type_, default, meta = entry  # type: ignore[misc]
        _add((path, type_, default, {**meta, "constant": True}))

    for entry in _DOMAIN_ENTRIES:
        _add(entry)

    for entry in extra_entries or []:
        _add(entry)

    return registry


def _is_bool(x: Any) -> bool:
    return isinstance(x, bool)


def _as_int(x: Any) -> int:
    if _is_bool(x):
        raise _bad("bool is not a number")
    if isinstance(x, int):
        return x
    if isinstance(x, Decimal):
        if x != x.to_integral_value():
            raise _bad(f"expected integer, got non-integral {x}")
        return int(x.to_integral_value())
    raise _bad(f"expected integer, got {type(x).__name__}")


def _as_decimal(x: Any) -> Decimal:
    if _is_bool(x):
        raise _bad("bool is not a number")
    if isinstance(x, Decimal):
        return x
    if isinstance(x, int):
        return Decimal(x)
    raise _bad(f"expected decimal, got {type(x).__name__}")


def _bad(msg: str) -> EngineError:
    return EngineError(ErrorCode.CONFIG_VALUE_INVALID, msg)


def _v_int(spec: KeySpec, value: Any) -> int:
    n = _as_int(value)
    if spec.min is not None and n < spec.min:
        raise _bad(f"{spec.path}: {n} below min {spec.min}")
    if spec.max is not None and n > spec.max:
        raise _bad(f"{spec.path}: {n} above max {spec.max}")
    return n


def _v_decimal(spec: KeySpec, value: Any) -> Decimal:
    d = _as_decimal(value)
    if spec.min is not None and d < spec.min:
        raise _bad(f"{spec.path}: {d} below min {spec.min}")
    if spec.max is not None and d > spec.max:
        raise _bad(f"{spec.path}: {d} above max {spec.max}")
    return d


def _v_probability(spec: KeySpec, value: Any) -> Decimal:
    p = _as_decimal(value)
    if not (0 <= p <= 1):
        raise _bad(f"{spec.path}: probability {p} out of [0, 1]")
    return p


def _v_cost(spec: KeySpec, value: Any) -> int:
    n = _as_int(value)
    if n < 0:
        raise _bad(f"{spec.path}: negative cost {n}")
    return n


def _v_cost_map(spec: KeySpec, value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise _bad(f"{spec.path}: expected map, got {type(value).__name__}")
    out: dict[str, int] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            raise _bad(f"{spec.path}: cost-map keys must be non-empty strings")
        n = _as_int(v)
        if n < 0:
            raise _bad(f"{spec.path}: negative cost {n!r} for {k!r}")
        out[k] = n
    return out


def _v_int_range(spec: KeySpec, value: Any) -> tuple[int, int]:
    if not isinstance(value, dict) or set(value.keys()) != {"min", "max"}:
        raise _bad(f"{spec.path}: range needs {{'min','max'}}, got {value!r}")
    lo = _as_int(value["min"])
    hi = _as_int(value["max"])
    if lo > hi:
        raise _bad(f"{spec.path}: min {lo} > max {hi}")
    return (lo, hi)


def _v_block_grid(spec: KeySpec, value: Any) -> list[list[int]]:
    if not isinstance(value, list) or not value:
        raise _bad(f"{spec.path}: block_grid must be a non-empty list")
    prev_end: int | None = None
    pairs: list[list[int]] = []
    for i, b in enumerate(value):
        if not isinstance(b, (list, tuple)) or len(b) != 2 or _is_bool(b[0]) or _is_bool(b[1]):
            raise _bad(f"{spec.path}: block {i} must be a [start, end] pair")
        start = _as_int(b[0])
        end = _as_int(b[1])
        if start < 0 or end > 1440 or start >= end:
            raise _bad(f"{spec.path}: block {i} [{start},{end}] invalid within [0,1440)")
        if prev_end is not None and start != prev_end:
            raise _bad(f"{spec.path}: block grid is not contiguous (gap before block {i})")
        prev_end = end
        pairs.append([start, end])
    return pairs


_DAY_TYPES = frozenset({"weekday", "weekend"})


def _v_time_of_day_bands(spec: KeySpec, value: Any) -> list[tuple[str, int, int]]:
    if not isinstance(value, list) or not value:
        raise _bad(f"{spec.path}: time_of_day_bands must be a non-empty list")
    prev_end = 0
    seen: set[str] = set()
    out: list[tuple[str, int, int]] = []
    for i, b in enumerate(value):
        if not isinstance(b, (list, tuple)) or len(b) != 3:
            raise _bad(f"{spec.path}: band {i} must be a [name, start, end] triple")
        name, start_raw, end_raw = b
        if not isinstance(name, str) or not name:
            raise _bad(f"{spec.path}: band {i} name must be a non-empty string")
        if name in seen:
            raise _bad(f"{spec.path}: duplicate band name {name!r}")
        if _is_bool(start_raw) or _is_bool(end_raw):
            raise _bad(f"{spec.path}: band {name!r} start/end must be integers")
        start, end = _as_int(start_raw), _as_int(end_raw)
        if start < 0 or end > 1440 or start >= end:
            raise _bad(f"{spec.path}: band {name!r} [{start},{end}] invalid within [0,1440)")
        if start != prev_end:
            raise _bad(
                f"{spec.path}: band {name!r} starts at {start}, expected {prev_end} "
                "(bands must be contiguous and non-overlapping)"
            )
        prev_end = end
        seen.add(name)
        out.append((name, start, end))
    if prev_end != 1440:
        raise _bad(f"{spec.path}: bands end at {prev_end}, must cover [00:00,24:00) exactly")
    return out


def _v_speed_by_time_band(spec: KeySpec, value: Any) -> dict[str, dict[str, Decimal]]:
    if not isinstance(value, dict):
        raise _bad(f"{spec.path}: expected map, got {type(value).__name__}")
    if set(value.keys()) != _DAY_TYPES:
        raise _bad(
            f"{spec.path}: day_type keys must be exactly {sorted(_DAY_TYPES)}, got "
            f"{sorted(value.keys())}"
        )
    out: dict[str, dict[str, Decimal]] = {}
    for day_type, table in value.items():
        if not isinstance(table, dict) or not table:
            raise _bad(f"{spec.path}: {day_type} speed table must be a non-empty map")
        inner: dict[str, Decimal] = {}
        for band, raw in table.items():
            if not isinstance(band, str) or not band:
                raise _bad(f"{spec.path}: {day_type} band keys must be non-empty strings")
            speed = _as_decimal(raw)
            if not speed.is_finite() or speed <= 0:
                raise _bad(
                    f"{spec.path}: {day_type}.{band} speed {speed} must be finite and "
                    "strictly positive"
                )
            inner[band] = speed
        out[day_type] = inner
    return out


def _v_weather_multiplier_map(spec: KeySpec, value: Any) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        raise _bad(f"{spec.path}: expected map, got {type(value).__name__}")
    expected = {c.value for c in ConditionCode}
    got = set(value.keys())
    if got != expected:
        raise _bad(
            f"{spec.path}: must cover exactly the ConditionCode enum; "
            f"missing={sorted(expected - got)} extra={sorted(got - expected)}"
        )
    out: dict[str, Decimal] = {}
    for code, raw in value.items():
        m = _as_decimal(raw)
        if not m.is_finite() or m <= 0:
            raise _bad(
                f"{spec.path}: multiplier for {code!r} ({m}) must be finite and strictly positive"
            )
        out[code] = m
    return out


def _v_acceptance_curve(spec: KeySpec, value: Any) -> dict[str, list[Decimal]]:
    if not isinstance(value, dict) or not value:
        raise _bad(f"{spec.path}: acceptance_curve must be a non-empty map")
    out: dict[str, list[Decimal]] = {}
    for cls, curve in value.items():
        if not isinstance(cls, str):
            raise _bad(f"{spec.path}: acceptance_curve class must be a string")
        if not isinstance(curve, list) or not curve:
            raise _bad(f"{spec.path}: class {cls!r} curve must be a non-empty list")
        ps: list[Decimal] = []
        prev: Decimal | None = None
        for j, p in enumerate(curve):
            pv = _as_decimal(p)
            if not (0 <= pv <= 1):
                raise _bad(f"{spec.path}: class {cls!r} prob[{j}] {pv} out of [0,1]")
            if prev is not None and pv > prev:
                raise _bad(f"{spec.path}: class {cls!r} not monotone nonincreasing at index {j}")
            ps.append(pv)
            prev = pv
        out[cls] = ps
    return out


def _v_string(spec: KeySpec, value: Any) -> str:
    if not isinstance(value, str):
        raise _bad(f"{spec.path}: expected string, got {type(value).__name__}")
    return value


def _v_string_list(spec: KeySpec, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise _bad(f"{spec.path}: expected list, got {type(value).__name__}")
    out: list[str] = []
    for s in value:
        if not isinstance(s, str) or not s:
            raise _bad(f"{spec.path}: list items must be non-empty strings")
        out.append(s)
    return out


def _v_bool(spec: KeySpec, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{spec.path}: expected bool, got {type(value).__name__}")
    return value


def _v_date_string(spec: KeySpec, value: Any) -> str:
    s = _v_string(spec, value)
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        raise _bad(f"{spec.path}: {s!r} is not a valid YYYY-MM-DD date") from None
    if d.isoformat() != s:
        raise _bad(f"{spec.path}: {s!r} is not in canonical YYYY-MM-DD form")
    return s


def _v_month_day_string(spec: KeySpec, value: Any) -> str:
    s = _v_string(spec, value)
    parts = s.split("-")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        raise _bad(f"{spec.path}: {s!r} is not a valid MM-DD string")
    month_s, day_s = parts
    if not (month_s.isdigit() and day_s.isdigit()):
        raise _bad(f"{spec.path}: {s!r} is not a valid MM-DD string")
    try:
        d = datetime.date(2001, int(month_s), int(day_s))
    except ValueError:
        raise _bad(f"{spec.path}: {s!r} is not a valid MM-DD date") from None
    canonical = f"{d.month:02d}-{d.day:02d}"
    if canonical != s:
        raise _bad(f"{spec.path}: {s!r} is not in canonical MM-DD form")
    return s


def _v_decimal_list(spec: KeySpec, value: Any) -> list[Decimal]:
    if not isinstance(value, list) or not value:
        raise _bad(f"{spec.path}: expected a non-empty list, got {value!r}")
    if spec.length is not None and len(value) != spec.length:
        raise _bad(f"{spec.path}: expected length {spec.length}, got {len(value)}")
    out: list[Decimal] = []
    for i, x in enumerate(value):
        d = _as_decimal(x)
        if not d.is_finite():
            raise _bad(f"{spec.path}[{i}]: {d} must be finite")
        if spec.min is not None and d < spec.min:
            raise _bad(f"{spec.path}[{i}]: {d} below min {spec.min}")
        out.append(d)
    return out


def _v_probability_list(spec: KeySpec, value: Any) -> list[Decimal]:
    if not isinstance(value, list) or not value:
        raise _bad(f"{spec.path}: expected a non-empty list, got {value!r}")
    if spec.length is not None and len(value) != spec.length:
        raise _bad(f"{spec.path}: expected length {spec.length}, got {len(value)}")
    out: list[Decimal] = []
    for i, x in enumerate(value):
        p = _as_decimal(x)
        if not p.is_finite():
            raise _bad(f"{spec.path}[{i}]: {p} must be finite")
        if not (0 <= p <= 1):
            raise _bad(f"{spec.path}[{i}]: probability {p} out of [0, 1]")
        out.append(p)
    return out


def _v_decimal_map(spec: KeySpec, value: Any) -> dict[str, Decimal]:
    if not isinstance(value, dict) or not value:
        raise _bad(f"{spec.path}: expected a non-empty map, got {type(value).__name__}")
    out: dict[str, Decimal] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            raise _bad(f"{spec.path}: map keys must be non-empty strings")
        d = _as_decimal(v)
        if not d.is_finite():
            raise _bad(f"{spec.path}: value for {k!r} ({d}) must be finite")
        if spec.min is not None and d < spec.min:
            raise _bad(f"{spec.path}: value for {k!r} ({d}) below min {spec.min}")
        out[k] = d
    return out


def _v_probability_map(spec: KeySpec, value: Any) -> dict[str, Decimal]:
    if not isinstance(value, dict) or not value:
        raise _bad(f"{spec.path}: expected a non-empty map, got {type(value).__name__}")
    out: dict[str, Decimal] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            raise _bad(f"{spec.path}: map keys must be non-empty strings")
        p = _as_decimal(v)
        if not p.is_finite():
            raise _bad(f"{spec.path}: value for {k!r} ({p}) must be finite")
        if not (0 <= p <= 1):
            raise _bad(f"{spec.path}: probability for {k!r} ({p}) out of [0, 1]")
        out[k] = p
    return out


def _v_decimal_map_map(spec: KeySpec, value: Any) -> dict[str, dict[str, Decimal]]:
    if not isinstance(value, dict) or not value:
        raise _bad(f"{spec.path}: expected a non-empty map, got {type(value).__name__}")
    out: dict[str, dict[str, Decimal]] = {}
    for outer_k, inner in value.items():
        if not isinstance(outer_k, str) or not outer_k:
            raise _bad(f"{spec.path}: outer map keys must be non-empty strings")
        if not isinstance(inner, dict) or not inner:
            raise _bad(f"{spec.path}[{outer_k!r}]: expected a non-empty map")
        inner_out: dict[str, Decimal] = {}
        for k, v in inner.items():
            if not isinstance(k, str) or not k:
                raise _bad(f"{spec.path}[{outer_k!r}]: inner map keys must be non-empty strings")
            d = _as_decimal(v)
            if not d.is_finite():
                raise _bad(f"{spec.path}[{outer_k!r}][{k!r}]: {d} must be finite")
            inner_out[k] = d
        out[outer_k] = inner_out
    return out


def _v_decimal_range(spec: KeySpec, value: Any) -> tuple[Decimal, Decimal]:
    if not isinstance(value, dict) or set(value.keys()) != {"min", "max"}:
        raise _bad(f"{spec.path}: range needs {{'min','max'}}, got {value!r}")
    lo = _as_decimal(value["min"])
    hi = _as_decimal(value["max"])
    if not lo.is_finite():
        raise _bad(f"{spec.path}: min {lo} must be finite")
    if not hi.is_finite():
        raise _bad(f"{spec.path}: max {hi} must be finite")
    if lo > hi:
        raise _bad(f"{spec.path}: min {lo} > max {hi}")
    return (lo, hi)


def _v_severity_bands(spec: KeySpec, value: Any) -> list[tuple[str, Decimal, Decimal]]:
    if not isinstance(value, list) or not value:
        raise _bad(f"{spec.path}: severity_bands must be a non-empty list")
    seen: set[str] = set()
    prev_hi: Decimal | None = None
    out: list[tuple[str, Decimal, Decimal]] = []
    for i, b in enumerate(value):
        if not isinstance(b, (list, tuple)) or len(b) != 3:
            raise _bad(f"{spec.path}: band {i} must be a [name, lo, hi] triple")
        name, lo_raw, hi_raw = b
        if not isinstance(name, str) or not name:
            raise _bad(f"{spec.path}: band {i} name must be a non-empty string")
        if name in seen:
            raise _bad(f"{spec.path}: duplicate band name {name!r}")
        lo = _as_decimal(lo_raw)
        hi = _as_decimal(hi_raw)
        if not (lo.is_finite() and hi.is_finite()) or lo >= hi:
            raise _bad(f"{spec.path}: band {name!r} requires lo < hi, got [{lo}, {hi}]")
        if prev_hi is not None and lo != prev_hi:
            raise _bad(
                f"{spec.path}: band {name!r} lo {lo} does not equal previous hi {prev_hi} "
                "(bands must be contiguous)"
            )
        prev_hi = hi
        seen.add(name)
        out.append((name, lo, hi))
    return out


_VALIDATORS = {
    "int": _v_int,
    "decimal": _v_decimal,
    "probability": _v_probability,
    "cost": _v_cost,
    "cost_map": _v_cost_map,
    "int_range": _v_int_range,
    "block_grid": _v_block_grid,
    "time_of_day_bands": _v_time_of_day_bands,
    "speed_by_time_band": _v_speed_by_time_band,
    "weather_multiplier_map": _v_weather_multiplier_map,
    "acceptance_curve": _v_acceptance_curve,
    "string": _v_string,
    "string_list": _v_string_list,
    "bool": _v_bool,
    "date_string": _v_date_string,
    "month_day_string": _v_month_day_string,
    "decimal_list": _v_decimal_list,
    "probability_list": _v_probability_list,
    "decimal_map": _v_decimal_map,
    "probability_map": _v_probability_map,
    "decimal_map_map": _v_decimal_map_map,
    "decimal_range": _v_decimal_range,
    "severity_bands": _v_severity_bands,
}


def validate_value(spec: KeySpec, value: Any) -> Any:
    validator = _VALIDATORS.get(spec.type)
    if validator is None:
        raise _bad(f"{spec.path}: unknown key type {spec.type!r}")
    return validator(spec, value)


def _validate_noise_clamp(base: dict[str, Any]) -> None:
    lo = base["travel.noise.min_multiplier"]
    hi = base["travel.noise.max_multiplier"]
    if lo <= 0:
        raise _bad(f"travel.noise.min_multiplier: {lo} must be strictly positive")
    if lo > hi:
        raise _bad(f"travel.noise.min_multiplier {lo} > travel.noise.max_multiplier {hi}")


def _validate_speed_table_bands(base: dict[str, Any]) -> None:
    band_names = {name for name, _start, _end in base["travel.time_of_day_bands"]}
    for day_type, table in base["travel.speed_by_time_band"].items():
        if set(table.keys()) != band_names:
            missing = band_names - set(table)
            extra = set(table) - band_names
            raise _bad(
                f"travel.speed_by_time_band[{day_type}]: band keys must exactly match "
                f"travel.time_of_day_bands; missing={sorted(missing)} extra={sorted(extra)}"
            )


def _validate_run_window(base: dict[str, Any]) -> None:
    start = datetime.date.fromisoformat(base["run.start_date"])
    end = datetime.date.fromisoformat(base["run.end_date"])
    warm_start_end = datetime.date.fromisoformat(base["constants.warm_start_end"])
    if end < start:
        raise _bad(f"run.end_date {end} precedes run.start_date {start}")
    if start <= warm_start_end:
        raise _bad(
            f"run.start_date {start} must fall strictly after "
            f"constants.warm_start_end {warm_start_end}"
        )


_SEGMENTS = frozenset({"residential", "commercial", "apartment"})
_URGENCY_CLASSES = frozenset({"standard", "urgent"})
_SUPPORTED_CATEGORY_SET = frozenset(SUPPORTED_CATEGORIES)
_JOB_TYPE_CODE_SET = frozenset(JOB_TYPE_CODES)
_CONDITION_THRESHOLD_KEYS = frozenset(
    {"extreme_heat_high_f", "hot_high_f", "cool_high_f", "windy_wind_mph", "rain_precipitation_mm"}
)


def _validate_demand_urgency_ordering(base: dict[str, Any]) -> None:
    after = base["demand.after_hours_urgent_probability"]
    office = base["demand.office_hours_urgent_probability"]
    if after < office:
        raise _bad(
            f"demand.after_hours_urgent_probability {after} must be >= "
            f"demand.office_hours_urgent_probability {office}"
        )


def _validate_repeat_propensity_bounds(base: dict[str, Any]) -> None:
    floor = base["customers.repeat_propensity_floor"]
    ceiling = base["customers.repeat_propensity_max"]
    if floor <= 0:
        raise _bad(f"customers.repeat_propensity_floor {floor} must be > 0")
    for seg, val in base["demand.repeat_propensity_initial_by_segment"].items():
        if not (floor <= val <= ceiling):
            raise _bad(
                f"demand.repeat_propensity_initial_by_segment[{seg}] {val} outside "
                f"[customers.repeat_propensity_floor {floor}, "
                f"customers.repeat_propensity_max {ceiling}]"
            )


def _validate_word_of_mouth_weight_ordering(base: dict[str, Any]) -> None:
    pos = base["demand.word_of_mouth_positive_weight"]
    neg = base["demand.word_of_mouth_negative_weight"]
    if pos <= 0:
        raise _bad(f"demand.word_of_mouth_positive_weight {pos} must be > 0")
    if neg < pos:
        raise _bad(
            f"demand.word_of_mouth_negative_weight {neg} must be >= "
            f"demand.word_of_mouth_positive_weight {pos}"
        )


def _validate_outlying_address_share(base: dict[str, Any]) -> None:
    share = base["demand.outlying_address_share"]
    if not (0 < share < 1):
        raise _bad(
            f"demand.outlying_address_share {share} must lie strictly inside (0, 1)"
        )


def _validate_temperature_normalization(base: dict[str, Any]) -> None:
    v = base["demand.temperature_normalization_f"]
    if v <= 0:
        raise _bad(f"demand.temperature_normalization_f {v} must be > 0")


def _validate_forecast_arrays_nondecreasing(base: dict[str, Any]) -> None:
    for path in (
        "weather.forecast_error_scale_by_horizon",
        "weather.forecast_storm_flip_probability_by_horizon",
    ):
        arr = base[path]
        if arr[0] != 0:
            raise _bad(f"{path}[0] must equal 0, got {arr[0]}")
        for i in range(1, len(arr)):
            if arr[i] < arr[i - 1]:
                raise _bad(
                    f"{path} must be nondecreasing in horizon; "
                    f"[{i}]={arr[i]} < [{i - 1}]={arr[i - 1]}"
                )


def _validate_storm_severity_partition(base: dict[str, Any]) -> None:
    lo_range, hi_range = base["weather.storm_severity_range"]
    bands = base["weather.storm_severity_bands"]
    first_lo = bands[0][1]
    last_hi = bands[-1][2]
    if first_lo != lo_range:
        raise _bad(
            f"weather.storm_severity_bands first lo {first_lo} != "
            f"weather.storm_severity_range min {lo_range}"
        )
    if last_hi != hi_range:
        raise _bad(
            f"weather.storm_severity_bands last hi {last_hi} != "
            f"weather.storm_severity_range max {hi_range}"
        )


def _validate_precipitation_probability_condition_coverage(base: dict[str, Any]) -> None:
    expected = {c.value for c in ConditionCode}
    got = set(base["weather.precipitation_probability_by_condition"].keys())
    if got != expected:
        raise _bad(
            "weather.precipitation_probability_by_condition must cover exactly the "
            f"ConditionCode enum; missing={sorted(expected - got)} "
            f"extra={sorted(got - expected)}"
        )


def _validate_acceptance_maps(base: dict[str, Any]) -> None:
    maps = {
        "acceptance.base_probability_by_urgency": base["acceptance.base_probability_by_urgency"],
        "acceptance.lead_time_decay_by_urgency": base["acceptance.lead_time_decay_by_urgency"],
        "acceptance.floor_probability_by_urgency": base["acceptance.floor_probability_by_urgency"],
    }
    for path, m in maps.items():
        if set(m.keys()) != _URGENCY_CLASSES:
            raise _bad(
                f"{path} keys must be exactly {sorted(_URGENCY_CLASSES)}, got "
                f"{sorted(m.keys())}"
            )
    base_map = maps["acceptance.base_probability_by_urgency"]
    floor_map = maps["acceptance.floor_probability_by_urgency"]
    for cls in _URGENCY_CLASSES:
        if floor_map[cls] > base_map[cls]:
            raise _bad(
                f"acceptance.floor_probability_by_urgency[{cls}] {floor_map[cls]} > "
                f"base {base_map[cls]}"
            )


def _validate_unit_interval_strict_params(base: dict[str, Any]) -> None:
    for path in (
        "competitors.served_well_repeat_multiplier",
        "demand.rejection_repeat_propensity_multiplier",
    ):
        v = base[path]
        if not (0 < v < 1):
            raise _bad(f"{path} {v} must lie strictly inside (0, 1)")


def _validate_zone_weights_length(base: dict[str, Any]) -> None:
    weights = base["demand.zone_weights"]
    count = base["constants.zone_count"]
    if len(weights) != count:
        raise _bad(
            f"demand.zone_weights has {len(weights)} entries, expected "
            f"constants.zone_count {count}"
        )


def _validate_category_and_segment_key_sets(base: dict[str, Any]) -> None:
    category_weights = base["demand.category_weights"]
    if set(category_weights.keys()) != _SEGMENTS:
        raise _bad(
            f"demand.category_weights outer keys must be exactly {sorted(_SEGMENTS)}, got "
            f"{sorted(category_weights.keys())}"
        )
    expected_inner = _SUPPORTED_CATEGORY_SET | set(base["demand.unsupported_categories"])
    for seg, inner in category_weights.items():
        if set(inner.keys()) != expected_inner:
            missing = expected_inner - set(inner)
            extra = set(inner) - expected_inner
            raise _bad(
                f"demand.category_weights[{seg}] keys must exactly match the supported "
                f"categories plus demand.unsupported_categories; missing={sorted(missing)} "
                f"extra={sorted(extra)}"
            )
    for path in (
        "demand.segment_weights",
        "cancellations.base_probability_by_segment",
        "demand.multi_location_probability_by_segment",
        "demand.repeat_propensity_initial_by_segment",
        "demand.word_of_mouth_segment_weights",
    ):
        keys = set(base[path].keys())
        if keys != _SEGMENTS:
            raise _bad(f"{path} keys must be exactly {sorted(_SEGMENTS)}, got {sorted(keys)}")


def _validate_category_sensitivity_key_sets(base: dict[str, Any]) -> None:
    for path in (
        "demand.heat_sensitivity_by_category",
        "demand.cold_sensitivity_by_category",
        "demand.storm_multiplier_by_category",
    ):
        keys = set(base[path].keys())
        if keys != _SUPPORTED_CATEGORY_SET:
            raise _bad(
                f"{path} keys must be exactly {sorted(_SUPPORTED_CATEGORY_SET)}, got "
                f"{sorted(keys)}"
            )


def _validate_complexity_distribution(base: dict[str, Any]) -> None:
    dist = base["quality.complexity_distribution"]
    keys = set(dist.keys())
    if keys != _JOB_TYPE_CODE_SET:
        raise _bad(
            f"quality.complexity_distribution keys must be exactly "
            f"{sorted(_JOB_TYPE_CODE_SET)}, got {sorted(keys)}"
        )
    for job_type, inner in dist.items():
        if set(inner.keys()) != {"min", "max"}:
            raise _bad(
                f"quality.complexity_distribution[{job_type!r}] keys must be exactly "
                f"{{'min', 'max'}}, got {sorted(inner.keys())}"
            )
        lo, hi = inner["min"], inner["max"]
        if not (0 <= lo <= hi <= 1):
            raise _bad(
                f"quality.complexity_distribution[{job_type!r}] must satisfy "
                f"0 <= min <= max <= 1, got min={lo} max={hi}"
            )


def _validate_severity_keyed_maps(base: dict[str, Any]) -> None:
    band_names = {name for name, _lo, _hi in base["weather.storm_severity_bands"]}
    expected = {"none"} | band_names
    for path in ("weather.wind_by_severity", "weather.precipitation_by_severity"):
        keys = set(base[path].keys())
        if keys != expected:
            raise _bad(
                f"{path} keys must be exactly {{'none'}} plus weather.storm_severity_bands "
                f"names; got {sorted(keys)}, expected {sorted(expected)}"
            )


def _validate_condition_code_threshold_keys(base: dict[str, Any]) -> None:
    keys = set(base["weather.condition_code_thresholds"].keys())
    if keys != _CONDITION_THRESHOLD_KEYS:
        raise _bad(
            "weather.condition_code_thresholds keys must be exactly "
            f"{sorted(_CONDITION_THRESHOLD_KEYS)}, got {sorted(keys)}"
        )


def _validate_min_max_pairs(base: dict[str, Any]) -> None:
    lo, hi = base["demand.weather_multiplier_min"], base["demand.weather_multiplier_max"]
    if lo <= 0 or lo > hi:
        raise _bad(
            f"demand.weather_multiplier_min {lo} must be > 0 and <= "
            f"demand.weather_multiplier_max {hi}"
        )
    lo, hi = base["demand.reputation_multiplier_min"], base["demand.reputation_multiplier_max"]
    if lo > hi:
        raise _bad(
            f"demand.reputation_multiplier_min {lo} > demand.reputation_multiplier_max {hi}"
        )
    lo = base["demand.word_of_mouth_multiplier_min"]
    hi = base["demand.word_of_mouth_multiplier_max"]
    if lo > hi:
        raise _bad(
            f"demand.word_of_mouth_multiplier_min {lo} > "
            f"demand.word_of_mouth_multiplier_max {hi}"
        )
    v = base["competitors.demand_multiplier_min"]
    if not (0 < v <= 1):
        raise _bad(f"competitors.demand_multiplier_min {v} must lie in (0, 1]")


def _validate_weather_positive_scalars(base: dict[str, Any]) -> None:
    for path in (
        "weather.daily_noise_sd_f",
        "weather.diurnal_range_f",
        "demand.base_new_caller_rate_per_day",
    ):
        v = base[path]
        if v <= 0:
            raise _bad(f"{path} {v} must be strictly positive")


def _month_day_tuple(s: str) -> tuple[int, int]:
    month_s, day_s = s.split("-")
    return (int(month_s), int(day_s))


def _validate_monsoon_window_order(base: dict[str, Any]) -> None:
    start_s = base["weather.monsoon_window_start"]
    end_s = base["weather.monsoon_window_end"]
    if _month_day_tuple(start_s) > _month_day_tuple(end_s):
        raise _bad(
            f"weather.monsoon_window_start {start_s} must not fall after "
            f"weather.monsoon_window_end {end_s}"
        )


def _validate_weather_precompute_window(base: dict[str, Any]) -> None:
    precompute_start = base["weather.precompute_start_date"]
    warm_start_start = base["constants.warm_start_start"]
    if precompute_start != warm_start_start:
        raise _bad(
            f"weather.precompute_start_date {precompute_start} must equal "
            f"constants.warm_start_start {warm_start_start}"
        )
    precompute_end = datetime.date.fromisoformat(base["weather.precompute_end_date"])
    run_end = datetime.date.fromisoformat(base["run.end_date"])
    horizon = base["weather.forecast_max_horizon_days"]
    required_end = run_end + datetime.timedelta(days=horizon)
    if precompute_end < required_end:
        raise _bad(
            f"weather.precompute_end_date {precompute_end} must reach run.end_date "
            f"{run_end} + weather.forecast_max_horizon_days {horizon} = {required_end}"
        )


def _validate_arrival_time_weights_positive_sum(base: dict[str, Any]) -> None:
    total = sum(base["demand.arrival_time_of_day_weights"])
    if total <= 0:
        raise _bad(f"demand.arrival_time_of_day_weights must have a positive sum, got {total}")


_CROSS_FIELD_VALIDATORS: tuple[tuple[frozenset[str], Any], ...] = (
    (
        frozenset({"travel.noise.min_multiplier", "travel.noise.max_multiplier"}),
        _validate_noise_clamp,
    ),
    (
        frozenset({"travel.time_of_day_bands", "travel.speed_by_time_band"}),
        _validate_speed_table_bands,
    ),
    (
        frozenset({"run.start_date", "run.end_date", "constants.warm_start_end"}),
        _validate_run_window,
    ),
    (
        frozenset(
            {"demand.after_hours_urgent_probability", "demand.office_hours_urgent_probability"}
        ),
        _validate_demand_urgency_ordering,
    ),
    (
        frozenset(
            {
                "customers.repeat_propensity_floor",
                "customers.repeat_propensity_max",
                "demand.repeat_propensity_initial_by_segment",
            }
        ),
        _validate_repeat_propensity_bounds,
    ),
    (
        frozenset(
            {"demand.word_of_mouth_positive_weight", "demand.word_of_mouth_negative_weight"}
        ),
        _validate_word_of_mouth_weight_ordering,
    ),
    (
        frozenset({"demand.outlying_address_share"}),
        _validate_outlying_address_share,
    ),
    (
        frozenset({"demand.temperature_normalization_f"}),
        _validate_temperature_normalization,
    ),
    (
        frozenset(
            {
                "weather.forecast_error_scale_by_horizon",
                "weather.forecast_storm_flip_probability_by_horizon",
            }
        ),
        _validate_forecast_arrays_nondecreasing,
    ),
    (
        frozenset({"weather.storm_severity_range", "weather.storm_severity_bands"}),
        _validate_storm_severity_partition,
    ),
    (
        frozenset({"weather.precipitation_probability_by_condition"}),
        _validate_precipitation_probability_condition_coverage,
    ),
    (
        frozenset(
            {
                "acceptance.base_probability_by_urgency",
                "acceptance.lead_time_decay_by_urgency",
                "acceptance.floor_probability_by_urgency",
            }
        ),
        _validate_acceptance_maps,
    ),
    (
        frozenset(
            {
                "competitors.served_well_repeat_multiplier",
                "demand.rejection_repeat_propensity_multiplier",
            }
        ),
        _validate_unit_interval_strict_params,
    ),
    (
        frozenset({"demand.zone_weights", "constants.zone_count"}),
        _validate_zone_weights_length,
    ),
    (
        frozenset(
            {
                "demand.category_weights",
                "demand.unsupported_categories",
                "demand.segment_weights",
                "cancellations.base_probability_by_segment",
                "demand.multi_location_probability_by_segment",
                "demand.repeat_propensity_initial_by_segment",
                "demand.word_of_mouth_segment_weights",
            }
        ),
        _validate_category_and_segment_key_sets,
    ),
    (
        frozenset(
            {
                "demand.heat_sensitivity_by_category",
                "demand.cold_sensitivity_by_category",
                "demand.storm_multiplier_by_category",
            }
        ),
        _validate_category_sensitivity_key_sets,
    ),
    (
        frozenset({"quality.complexity_distribution"}),
        _validate_complexity_distribution,
    ),
    (
        frozenset(
            {
                "weather.storm_severity_bands",
                "weather.wind_by_severity",
                "weather.precipitation_by_severity",
            }
        ),
        _validate_severity_keyed_maps,
    ),
    (
        frozenset({"weather.condition_code_thresholds"}),
        _validate_condition_code_threshold_keys,
    ),
    (
        frozenset(
            {
                "demand.weather_multiplier_min",
                "demand.weather_multiplier_max",
                "demand.reputation_multiplier_min",
                "demand.reputation_multiplier_max",
                "demand.word_of_mouth_multiplier_min",
                "demand.word_of_mouth_multiplier_max",
                "competitors.demand_multiplier_min",
            }
        ),
        _validate_min_max_pairs,
    ),
    (
        frozenset(
            {
                "weather.daily_noise_sd_f",
                "weather.diurnal_range_f",
                "demand.base_new_caller_rate_per_day",
            }
        ),
        _validate_weather_positive_scalars,
    ),
    (
        frozenset({"weather.monsoon_window_start", "weather.monsoon_window_end"}),
        _validate_monsoon_window_order,
    ),
    (
        frozenset(
            {
                "weather.precompute_start_date",
                "weather.precompute_end_date",
                "constants.warm_start_start",
                "run.end_date",
                "weather.forecast_max_horizon_days",
            }
        ),
        _validate_weather_precompute_window,
    ),
    (
        frozenset({"demand.arrival_time_of_day_weights"}),
        _validate_arrival_time_weights_positive_sum,
    ),
)


def _validate_cross_field(base: dict[str, Any]) -> None:
    for required, check in _CROSS_FIELD_VALIDATORS:
        if required <= base.keys():
            check(base)


def _canon_scalar(v: Any) -> Any:
    if isinstance(v, Decimal):
        if v == v.to_integral_value() and abs(v) < Decimal(10**18):
            return int(v.to_integral_value())
        s = format(v, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    if _is_bool(v):
        return v
    if isinstance(v, int):
        if abs(v) >= 10**18:
            return str(v)
        return v
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return [_canon_scalar(x) for x in v]
    if isinstance(v, tuple):
        return [_canon_scalar(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _canon_scalar(x) for k, x in v.items()}
    if isinstance(v, KeySpec):
        return v.path
    raise _bad(f"cannot canonicalize {type(v).__name__} for hashing")


def canonical_config_bytes(config_version: str, resolved: Mapping[str, Any]) -> bytes:
    canon = {"config_version": str(config_version), "keys": {}}
    for path in sorted(resolved):
        canon["keys"][path] = _canon_scalar(resolved[path])
    return json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")


def config_hash(config_version: str, resolved: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_config_bytes(config_version, resolved)).hexdigest()


def probability_threshold(p: Decimal) -> int:
    if not isinstance(p, Decimal):
        p = Decimal(p)
    with localcontext() as ctx:
        ctx.prec = _THRESHOLD_PREC
        scaled = p * Decimal(U64_MOD)
        return int(scaled.to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class RunConfig:

    config_version: str
    resolved: Mapping[str, Any]
    _thresholds: Mapping[str, int] = field(default_factory=dict)  # type: ignore[var-annotated]
    _canonical_bytes: bytes = b""
    config_hash: str = ""

    def get(self, path: str) -> Any:
        return self.resolved[path]

    def get_int(self, path: str) -> int:
        v = self.resolved[path]
        if isinstance(v, tuple):
            raise TypeError(f"{path!r} is a range, use get_range()")
        return int(v)

    def get_decimal(self, path: str) -> Decimal:
        v = self.resolved[path]
        return v if isinstance(v, Decimal) else Decimal(v)

    def get_prob_threshold(self, path: str) -> int:
        if path in self._thresholds:
            return self._thresholds[path]
        return probability_threshold(self.get_decimal(path))

    def get_range(self, path: str) -> tuple[int, int]:
        v = self.resolved[path]
        if isinstance(v, tuple) and len(v) == 2:
            return (int(v[0]), int(v[1]))
        raise TypeError(f"{path!r} is not an int_range")

    def get_string(self, path: str) -> str:
        return str(self.resolved[path])

    def get_string_list(self, path: str) -> list[str]:
        return [str(s) for s in self.resolved[path]]

    def get_bool(self, path: str) -> bool:
        return bool(self.resolved[path])

    def keys(self) -> frozenset[str]:
        return frozenset(self.resolved.keys())

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes


def _registry_defaults(registry: dict[str, KeySpec]) -> dict[str, Any]:
    return {path: spec.default for path, spec in registry.items()}


def _dump_scalar(v: Any, placeholders: dict[str, str]) -> Any:
    if isinstance(v, Decimal):
        if v == v.to_integral_value():
            return int(v.to_integral_value())
        token = f"{_NUL}DECIMAL{len(placeholders)}{_NUL}"
        literal = format(v, "f")
        if "." in literal:
            literal = literal.rstrip("0").rstrip(".")
        placeholders[token] = literal
        return token
    if isinstance(v, (list, tuple)):
        return [_dump_scalar(x, placeholders) for x in v]
    if isinstance(v, dict):
        return {str(k): _dump_scalar(x, placeholders) for k, x in v.items()}
    return v


def dump_defaults(registry: dict[str, KeySpec], config_version: str) -> bytes:
    placeholders: dict[str, str] = {}
    keys = {
        path: _dump_scalar(default, placeholders)
        for path, default in _registry_defaults(registry).items()
    }
    payload = {"config_version": config_version, "keys": keys}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    for token, literal in placeholders.items():
        text = text.replace(json.dumps(token), literal)
    return text.encode("utf-8")


def _parse_layer(
    payload_json: str | bytes | None, *, source_name: str
) -> tuple[str | None, dict[str, Any]]:
    if payload_json in (None, ""):
        return None, {}
    try:
        data = json.loads(payload_json, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID, f"{source_name}: invalid JSON: {exc.msg}"
        ) from None
    if not isinstance(data, dict):
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID, f"{source_name}: top level must be an object"
        )
    if "keys" in data and not isinstance(data["keys"], dict):
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID, f"{source_name}: 'keys' must be an object"
        )
    cv = data.get("config_version")
    if cv is not None and not isinstance(cv, str):
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID, f"{source_name}: 'config_version' must be a string"
        )
    keys_raw = data.get("keys", {})
    if not isinstance(keys_raw, dict):
        keys_raw = {}
    cleaned: dict[str, Any] = {}
    for k, v in keys_raw.items():
        if not isinstance(k, str):
            raise EngineError(ErrorCode.UNKNOWN_CONFIG_KEY, f"non-string key {k!r}")
        cleaned[k] = v
    return cv, cleaned


def load_run_config(
    *,
    defaults_json: str | bytes | None = None,
    scenario_json: str | bytes | None = None,
    registry: dict[str, KeySpec] | None = None,
    config_version: str | None = None,
) -> RunConfig:
    reg = registry if registry is not None else assemble_registry()

    if defaults_json in (None, ""):
        base: dict[str, Any] = {}
        for path, spec in reg.items():
            base[path] = validate_value(spec, spec.default)
        cv = config_version or DEFAULT_CONFIG_VERSION
    else:
        defaults_cv, defaults_keys = _parse_layer(defaults_json, source_name="defaults")
        if config_version is not None:
            cv = config_version
        elif defaults_cv is not None:
            cv = defaults_cv
        else:
            cv = DEFAULT_CONFIG_VERSION
        base = {}
        for path, raw in defaults_keys.items():
            if path not in reg:
                raise EngineError(
                    ErrorCode.UNKNOWN_CONFIG_KEY,
                    f"defaults: unknown key {path!r}",
                )
            base[path] = validate_value(reg[path], raw)

    scenario_cv, scenario_keys = _parse_layer(scenario_json, source_name="scenario")
    if scenario_cv is not None:
        cv = scenario_cv
    for path, raw in scenario_keys.items():
        if path not in reg:
            raise EngineError(ErrorCode.UNKNOWN_CONFIG_KEY, f"scenario: unknown key {path!r}")
        if reg[path].constant:
            raise EngineError(
                ErrorCode.CONSTANT_OVERRIDE,
                f"scenario cannot override user-decided constant {path!r}",
            )
        base[path] = validate_value(reg[path], raw)

    missing = [p for p in reg if p not in base]
    if missing:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            f"missing required config keys: {sorted(missing)}",
        )

    _validate_cross_field(base)

    thresholds: dict[str, int] = {}
    for path, spec in reg.items():
        if spec.type == "probability":
            thresholds[path] = probability_threshold(base[path])

    canon = canonical_config_bytes(cv, base)
    digest = hashlib.sha256(canon).hexdigest()
    from types import MappingProxyType

    return RunConfig(
        config_version=cv,
        resolved=MappingProxyType(base),
        _thresholds=MappingProxyType(thresholds),
        _canonical_bytes=canon,
        config_hash=digest,
    )


DEFERRED_ITEMS: list[dict[str, Any]] = [
    {"name": "starting_cash", "key_paths": ["finance.starting_cash"], "status": "provisional"},
    {
        "name": "daily_operating_overhead_amount",
        "key_paths": ["finance.daily_operating_overhead"],
        "status": "provisional",
    },
    {
        "name": "off_hours_bonus_per_job",
        "key_paths": ["payroll.off_hours_bonus_per_job"],
        "status": "provisional",
    },
    {
        "name": "warm_start_record_count",
        "key_paths": ["calibration.warm_start.records"],
        "status": "provisional",
    },
    {"name": "demand_rates", "key_paths": ["demand.*"], "status": "provisional"},
    {"name": "acceptance_curves", "key_paths": ["acceptance.*"], "status": "provisional"},
    {"name": "cancellation_rates", "key_paths": ["cancellations.*"], "status": "provisional"},
    {"name": "callback_delays", "key_paths": ["callbacks.*"], "status": "provisional"},
    {"name": "competitor_probabilities", "key_paths": ["competitors.*"], "status": "provisional"},
    {"name": "review_sampling", "key_paths": ["reviews.*"], "status": "provisional"},
    {"name": "weather_severity", "key_paths": ["weather.*"], "status": "provisional"},
    {"name": "invoice_distributions", "key_paths": ["invoices.*"], "status": "provisional"},
    {"name": "material_percentages", "key_paths": ["materials.*"], "status": "provisional"},
    {
        "name": "skill_strain_coefficients",
        "key_paths": ["skill_strain.*", "quality.*"],
        "status": "provisional",
    },
    {"name": "training_gains_rates", "key_paths": ["training.*"], "status": "provisional"},
    {"name": "labor_economy_parameters", "key_paths": ["labor_economy.*"], "status": "provisional"},
    {"name": "hiring_costs_lead_times", "key_paths": ["hiring.*"], "status": "provisional"},
    {
        "name": "sickness_quit_probabilities",
        "key_paths": ["workforce_health.*", "workforce_attrition.*"],
        "status": "provisional",
    },
    {
        "name": "minimum_address_catalog_size",
        "key_paths": ["geography.min_address_catalog_size"],
        "status": "provisional",
    },
]


def build_deferred_manifest() -> bytes:
    return json.dumps({"items": DEFERRED_ITEMS}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class ResolveReport:
    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]
    provisional: tuple[str, ...]

    def all_resolved(self) -> bool:
        return not self.unresolved and not self.provisional


def _key_resolves(path_or_wild: str, registry: dict[str, KeySpec]) -> bool:
    if path_or_wild.endswith(".*"):
        prefix = path_or_wild[:-1]
        return any(p.startswith(prefix) for p in registry)
    return path_or_wild in registry


def resolve_deferred_manifest(
    manifest_json: str | bytes | None = None,
    *,
    registry: dict[str, KeySpec] | None = None,
    items: list[dict[str, Any]] | None = None,
    strict: bool = False,
) -> ResolveReport:
    reg = registry if registry is not None else assemble_registry()
    if items is None:
        if manifest_json in (None, ""):
            items = DEFERRED_ITEMS
        else:
            data = json.loads(manifest_json)
            items = data.get("items", DEFERRED_ITEMS)  # type: ignore[assignment]

    resolved: list[str] = []
    unresolved: list[str] = []
    provisional: list[str] = []
    for item in items:
        name = item["name"]
        key_paths = item.get("key_paths", [])
        status = item.get("status")
        this_resolves = all(_key_resolves(kp, reg) for kp in key_paths)
        if this_resolves:
            resolved.append(name)
            if strict and status == "provisional":
                provisional.append(name)
        else:
            unresolved.append(name)
            if strict:
                raise EngineError(
                    ErrorCode.DEFERRED_UNRESOLVED,
                    f"deferred item {name!r} maps to no registered config key: {key_paths}",
                )
    if strict and provisional:
        raise EngineError(
            ErrorCode.DEFERRED_UNRESOLVED,
            f"registered keys still provisional: {sorted(provisional)}",
        )
    return ResolveReport(
        resolved=tuple(resolved),
        unresolved=tuple(unresolved),
        provisional=tuple(provisional),
    )


__all__ = [
    "DEFAULT_CONFIG_VERSION",
    "DEFERRED_ITEMS",
    "KeySpec",
    "RunConfig",
    "U64_MOD",
    "assemble_registry",
    "build_deferred_manifest",
    "canonical_config_bytes",
    "config_hash",
    "dump_defaults",
    "load_run_config",
    "probability_threshold",
    "resolve",
    "resolve_deferred_manifest",
    "validate_value",
]
resolve = resolve_deferred_manifest
