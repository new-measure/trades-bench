from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import origin_keys
from .config import RunConfig
from .enums import ConditionCode, KeyedStreamName, OriginEntity
from .errors import EngineError, ErrorCode
from .rng import Rng

_DAY_TYPES = frozenset({"weekday", "weekend"})

_UNIFORM_FLOAT_EPSILON = 2.0**-53
_UNIFORM_FLOAT_LOWER_BOUND = _UNIFORM_FLOAT_EPSILON
_UNIFORM_FLOAT_UPPER_BOUND = 1.0 - _UNIFORM_FLOAT_EPSILON

_STANDARD_NORMAL = statistics.NormalDist()


@dataclass(frozen=True)
class TravelNoiseKey:

    entity_type: str
    entity_id: int
    scope: str
    event_kind: str
    draw_index: int


def inbound_trip_noise_key(
    appointment_origin: str, service_date: str, trip_index: int
) -> TravelNoiseKey:
    return TravelNoiseKey(
        OriginEntity.APPOINTMENT.value,
        origin_keys.ENTITY_ID,
        origin_keys.scoped(appointment_origin, service_date),
        "travel_noise",
        trip_index,
    )


def return_home_trip_noise_key(
    technician_id: int, service_date: str, block_index: int
) -> TravelNoiseKey:
    return TravelNoiseKey(
        "technician", technician_id, service_date, "return_home_travel", block_index
    )


def band_for_minute(config: RunConfig, minute_of_day: int) -> str:
    if not (0 <= minute_of_day < 1440):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"minute_of_day must be in [0,1440), got {minute_of_day}",
        )
    for name, start, end in config.resolved["travel.time_of_day_bands"]:
        if start <= minute_of_day < end:
            return name
    raise EngineError(
        ErrorCode.INVARIANT_VIOLATED,
        "travel.time_of_day_bands does not cover every minute of the day",
    )


def _validated_day_type(day_type: str) -> str:
    if day_type not in _DAY_TYPES:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"day_type must be one of {sorted(_DAY_TYPES)}, got {day_type!r}",
        )
    return day_type


def _clamped_uniform_float(p: Decimal) -> float:
    return min(max(float(p), _UNIFORM_FLOAT_LOWER_BOUND), _UNIFORM_FLOAT_UPPER_BOUND)


def _noise_multiplier_from_uniform01(p: Decimal, config: RunConfig) -> Decimal:
    z = _STANDARD_NORMAL.inv_cdf(_clamped_uniform_float(p))
    sigma = float(config.get_decimal("travel.noise.sigma"))
    raw = Decimal(math.exp(sigma * z))
    lo = config.get_decimal("travel.noise.min_multiplier")
    hi = config.get_decimal("travel.noise.max_multiplier")
    return min(max(raw, lo), hi)


def noise_multiplier(rng: Rng, config: RunConfig, key: TravelNoiseKey) -> Decimal:
    p = rng.uniform01(
        KeyedStreamName.TRAVEL_NOISE.value,
        key.entity_type,
        key.entity_id,
        key.scope,
        key.event_kind,
        key.draw_index,
    )
    return _noise_multiplier_from_uniform01(p, config)


def actual_travel_minutes(
    rng: Rng,
    config: RunConfig,
    *,
    distance_km: float,
    day_type: str,
    minute_of_day: int,
    condition_code: ConditionCode,
    noise_key: TravelNoiseKey,
) -> int:
    band = band_for_minute(config, minute_of_day)
    speed = config.resolved["travel.speed_by_time_band"][_validated_day_type(day_type)][band]
    weather_multiplier = config.resolved["travel.weather_travel_multipliers"][
        condition_code.value
    ]
    noise = noise_multiplier(rng, config, noise_key)
    raw_minutes = Decimal(60) * Decimal(str(distance_km)) / speed * weather_multiplier * noise
    minutes = int(raw_minutes.to_integral_value(rounding=ROUND_HALF_UP))
    return max(minutes, config.get_int("travel.min_travel_minutes"))


__all__ = [
    "TravelNoiseKey",
    "actual_travel_minutes",
    "band_for_minute",
    "inbound_trip_noise_key",
    "noise_multiplier",
    "return_home_trip_noise_key",
]
