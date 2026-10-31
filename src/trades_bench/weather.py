from __future__ import annotations

import calendar
import math
import sqlite3
import statistics
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import clock, db
from .config import RunConfig, probability_threshold
from .enums import KeyedStreamName, SequentialStreamName
from .errors import EngineError, ErrorCode
from .rng import Rng, derive, randint, uniform01

_STANDARD_NORMAL = statistics.NormalDist()

_UNIFORM_FLOAT_EPSILON = 2.0**-53
_UNIFORM_FLOAT_LOWER_BOUND = _UNIFORM_FLOAT_EPSILON
_UNIFORM_FLOAT_UPPER_BOUND = 1.0 - _UNIFORM_FLOAT_EPSILON

_ZONE_IDS: tuple[int, ...] = tuple(range(1, 11))


def _clamped_float(p: Decimal) -> float:
    return min(max(float(p), _UNIFORM_FLOAT_LOWER_BOUND), _UNIFORM_FLOAT_UPPER_BOUND)


def _to_tenths(value: Decimal) -> int:
    return int((Decimal(value) * 10).to_integral_value(rounding=ROUND_HALF_UP))


def _to_milli(value: Decimal) -> int:
    return int((Decimal(value) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def _tenths_to_whole(tenths: int) -> int:
    return int((Decimal(tenths) / Decimal(10)).to_integral_value(rounding=ROUND_HALF_UP))


def _bands_milli(config: RunConfig) -> list[tuple[str, int, int]]:
    bands = config.resolved["weather.storm_severity_bands"]
    return [(name, _to_milli(lo), _to_milli(hi)) for name, lo, hi in bands]


def _band_name(bands_milli: list[tuple[str, int, int]], value_milli: int) -> str:
    last = len(bands_milli) - 1
    for i, (name, lo, hi) in enumerate(bands_milli):
        if i == last:
            if lo <= value_milli <= hi:
                return name
        elif lo <= value_milli < hi:
            return name
    raise EngineError(
        ErrorCode.INVARIANT_VIOLATED,
        f"storm severity {value_milli} milli is not covered by the configured bands",
    )


def _storm_band_names(bands_milli: list[tuple[str, int, int]]) -> frozenset[str]:
    if len(bands_milli) <= 1:
        return frozenset(name for name, _lo, _hi in bands_milli)
    return frozenset(name for name, _lo, _hi in bands_milli[1:])


def _seasonal_day_ordinal(day: date) -> int:
    tm_yday = day.timetuple().tm_yday
    if calendar.isleap(day.year) and tm_yday >= 60:
        return tm_yday - 1
    return tm_yday


def _daily_condition_code(
    *,
    storm_flag: int,
    severity_band: str,
    storm_band_names: frozenset[str],
    high_tenths: int,
    wind_tenths: int,
    precip_tenths: int,
    extreme_heat_tenths: int,
    hot_tenths: int,
    cool_tenths: int,
    windy_tenths: int,
    rain_tenths: int,
) -> str:
    if storm_flag and severity_band in storm_band_names:
        return "storm"
    if precip_tenths >= rain_tenths:
        return "rain"
    if high_tenths >= extreme_heat_tenths:
        return "extreme_heat"
    if high_tenths >= hot_tenths:
        return "hot"
    if wind_tenths >= windy_tenths:
        return "windy"
    if high_tenths <= cool_tenths:
        return "cool"
    return "clear"


def precompute(conn: sqlite3.Connection, config: RunConfig, rng: Rng, *, created_at: int) -> None:
    start = date.fromisoformat(config.get_string("weather.precompute_start_date"))
    end = date.fromisoformat(config.get_string("weather.precompute_end_date"))
    if end < start:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            "weather.precompute_end_date precedes weather.precompute_start_date",
        )

    amplitude_lo, amplitude_hi = config.get_range("weather.seasonal_amplitude_f_range")
    phase_lo, phase_hi = config.get_range("weather.seasonal_phase_shift_days_range")
    mean_high_f = config.get_decimal("weather.seasonal_mean_high_f")
    period_days = config.get_int("weather.seasonal_period_days")
    daily_noise_sd_f = float(config.get_decimal("weather.daily_noise_sd_f"))
    mean_reversion_rate = config.get_decimal("weather.mean_reversion_rate")
    zone_offsets_f = config.resolved["weather.zone_offset_f"]
    diurnal_tenths = _to_tenths(config.get_decimal("weather.diurnal_range_f"))
    monsoon_start = config.get_string("weather.monsoon_window_start")
    monsoon_end = config.get_string("weather.monsoon_window_end")
    probability_in_window = config.get_decimal("weather.storm_daily_probability_in_window")
    probability_out_of_window = config.get_decimal("weather.storm_daily_probability_out_of_window")
    correlation_w = config.get_decimal("weather.storm_zone_correlation")
    persistence_p = config.get_decimal("weather.storm_persistence")
    severity_lo, severity_hi = config.resolved["weather.storm_severity_range"]
    bands_milli = _bands_milli(config)
    storm_band_names = _storm_band_names(bands_milli)
    precipitation_by_severity = config.resolved["weather.precipitation_by_severity"]
    wind_by_severity = config.resolved["weather.wind_by_severity"]
    thresholds = config.resolved["weather.condition_code_thresholds"]
    extreme_heat_tenths = _to_tenths(thresholds["extreme_heat_high_f"])
    hot_tenths = _to_tenths(thresholds["hot_high_f"])
    cool_tenths = _to_tenths(thresholds["cool_high_f"])
    windy_tenths = _to_tenths(thresholds["windy_wind_mph"])
    rain_tenths = _to_tenths(thresholds["rain_precipitation_mm"])
    threshold_w = probability_threshold(correlation_w)

    if conn.in_transaction:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            "weather.precompute requires no open transaction on the connection",
        )
    db.begin(conn)
    rng.enter_txn()
    try:
        stream = rng.stream(SequentialStreamName.WEATHER_PROCESS.value)
        amplitude_f = randint(stream.next(), amplitude_lo, amplitude_hi)
        phase_days = randint(stream.next(), phase_lo, phase_hi)

        prev_x_milli = 0
        prev_storm_flag = dict.fromkeys(_ZONE_IDS, 0)

        day = start
        while day <= end:
            u = stream.next()
            day_of_year = _seasonal_day_ordinal(day)
            seasonal_high_f = float(mean_high_f) + amplitude_f * math.sin(
                2 * math.pi * (day_of_year - phase_days) / period_days
            )
            seasonal_high_tenths = int(
                Decimal(seasonal_high_f * 10).to_integral_value(rounding=ROUND_HALF_UP)
            )

            epsilon_u01 = uniform01(derive(u, "epsilon"))
            noise_z = _STANDARD_NORMAL.inv_cdf(_clamped_float(epsilon_u01))
            epsilon_f = noise_z * daily_noise_sd_f
            epsilon_milli = int(Decimal(epsilon_f * 1000).to_integral_value(rounding=ROUND_HALF_UP))

            reverted_milli = (mean_reversion_rate * prev_x_milli).to_integral_value(
                rounding=ROUND_HALF_UP
            )
            x_milli = int(reverted_milli) + epsilon_milli
            x_tenths = int(
                (Decimal(x_milli) / Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP)
            )
            metro_high_tenths = seasonal_high_tenths + x_tenths

            month_day = day.strftime("%m-%d")
            in_window = monsoon_start <= month_day <= monsoon_end
            onset_p = probability_in_window if in_window else probability_out_of_window
            threshold_onset = probability_threshold(onset_p)
            metro_driver = derive(u, "metro_storm") < threshold_onset

            rows: list[tuple] = []
            new_storm_flag: dict[int, int] = {}
            for zone_id in _ZONE_IDS:
                if derive(u, f"zone_storm:{zone_id}") < threshold_w:
                    is_storm = metro_driver
                else:
                    zone_p = (
                        max(onset_p, persistence_p) if prev_storm_flag[zone_id] else onset_p
                    )
                    threshold_zone_p = probability_threshold(zone_p)
                    is_storm = derive(u, f"zone_storm_ind:{zone_id}") < threshold_zone_p
                storm_flag = 1 if is_storm else 0
                new_storm_flag[zone_id] = storm_flag

                offset_tenths = _to_tenths(zone_offsets_f[zone_id - 1])
                high_tenths = metro_high_tenths + offset_tenths
                low_tenths = high_tenths - diurnal_tenths

                if storm_flag:
                    severity_unit = uniform01(derive(u, f"severity:{zone_id}"))
                    severity_dec = severity_lo + severity_unit * (severity_hi - severity_lo)
                    severity_milli: int | None = _to_milli(severity_dec)
                    severity_band = _band_name(bands_milli, severity_milli)
                else:
                    severity_milli = None
                    severity_band = "none"

                wind_base_tenths = _to_tenths(wind_by_severity[severity_band])
                wind_variation_tenths = derive(u, f"wind:{zone_id}") % 201
                wind_tenths = wind_base_tenths + wind_variation_tenths

                precip_tenths = _to_tenths(precipitation_by_severity[severity_band])

                condition_code = _daily_condition_code(
                    storm_flag=storm_flag,
                    severity_band=severity_band,
                    storm_band_names=storm_band_names,
                    high_tenths=high_tenths,
                    wind_tenths=wind_tenths,
                    precip_tenths=precip_tenths,
                    extreme_heat_tenths=extreme_heat_tenths,
                    hot_tenths=hot_tenths,
                    cool_tenths=cool_tenths,
                    windy_tenths=windy_tenths,
                    rain_tenths=rain_tenths,
                )

                rows.append(
                    (
                        zone_id,
                        day.isoformat(),
                        high_tenths,
                        low_tenths,
                        precip_tenths,
                        wind_tenths,
                        storm_flag,
                        severity_milli,
                        condition_code,
                        created_at,
                    )
                )

            conn.executemany(
                "INSERT INTO _zone_weather_daily "
                "(zone_id, local_date, high_f_tenths, low_f_tenths, precipitation_mm_tenths, "
                "wind_mph_tenths, storm_flag, storm_severity_milli, condition_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

            prev_storm_flag = new_storm_flag
            prev_x_milli = x_milli
            day += timedelta(days=1)

        conn.execute(
            "INSERT INTO _weather_process_state "
            "(singleton, seasonal_amplitude_f, seasonal_phase_days, final_x_milli, "
            "precompute_start_date, precompute_end_date, created_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                amplitude_f,
                phase_days,
                prev_x_milli,
                start.isoformat(),
                end.isoformat(),
                created_at,
            ),
        )
    except Exception:
        rng.exit_txn()
        if conn.in_transaction:
            db.rollback(conn)
        raise
    else:
        rng.exit_txn()
        db.commit(conn)


def _validate_zone_id(conn: sqlite3.Connection, zone_id: object) -> int:
    if isinstance(zone_id, bool) or not isinstance(zone_id, int):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"zone_id must be an int, got {zone_id!r}"
        )
    row = conn.execute("SELECT 1 FROM _zones WHERE id = ?", (zone_id,)).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"zone_id {zone_id} is not one of the ten zones"
        )
    return zone_id


def _parse_iso_date(name: str, value: object) -> date:
    if not isinstance(value, str):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"{name} must be a YYYY-MM-DD string, got {value!r}"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"{name} must be a YYYY-MM-DD string, got {value!r}"
        ) from exc


def _validate_horizon(
    today: str, target_date: str, max_horizon_days: int
) -> tuple[int, date, date]:
    today_d = _parse_iso_date("today", today)
    target_d = _parse_iso_date("date", target_date)
    horizon = (target_d - today_d).days
    if horizon < 0 or horizon > max_horizon_days:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"date {target_date} is outside [today, today + {max_horizon_days}] for "
            f"today={today}",
        )
    return horizon, today_d, target_d


def _precipitation_probability(config: RunConfig, condition_code: str) -> float:
    table = config.resolved["weather.precipitation_probability_by_condition"]
    return float(table[condition_code])


def _forecast(
    conn: sqlite3.Connection,
    config: RunConfig,
    rng: Rng,
    *,
    zone_id: int,
    today: str,
    target_date: str,
    horizon: int,
    high_f_tenths: int,
    low_f_tenths: int,
    storm_flag: bool,
    severity_milli: int | None,
) -> dict[str, object]:
    scale = float(config.resolved["weather.forecast_error_scale_by_horizon"][horizon])
    flip_probability = config.resolved["weather.forecast_storm_flip_probability_by_horizon"][
        horizon
    ]

    scope = f"{today}:{target_date}"
    n1_u = rng.draw(
        KeyedStreamName.WEATHER_FORECAST.value,
        "zone",
        zone_id,
        scope,
        "forecast_temp_noise",
        0,
    )
    n2_u = rng.draw(
        KeyedStreamName.WEATHER_FORECAST.value,
        "zone",
        zone_id,
        scope,
        "forecast_temp_noise",
        1,
    )
    flip_u = rng.draw(
        KeyedStreamName.WEATHER_FORECAST.value,
        "zone",
        zone_id,
        scope,
        "forecast_storm_flip",
        0,
    )

    n1 = _STANDARD_NORMAL.inv_cdf(_clamped_float(uniform01(n1_u)))
    n2 = _STANDARD_NORMAL.inv_cdf(_clamped_float(uniform01(n2_u)))

    high_hat_f = float(high_f_tenths) / 10.0 + n1 * scale
    low_hat_f = float(low_f_tenths) / 10.0 + n2 * scale
    high_hat = int(Decimal(high_hat_f).to_integral_value(rounding=ROUND_HALF_UP))
    low_hat = int(Decimal(low_hat_f).to_integral_value(rounding=ROUND_HALF_UP))
    if low_hat > high_hat:
        low_hat = high_hat

    flip_threshold = probability_threshold(flip_probability)
    flipped = flip_u < flip_threshold
    storm_hat = storm_flag != flipped

    if storm_hat:
        if severity_milli is None:
            severity_lo, severity_hi = config.resolved["weather.storm_severity_range"]
            phantom_unit = uniform01(derive(flip_u, "phantom_severity"))
            severity_dec = severity_lo + phantom_unit * (severity_hi - severity_lo)
            severity_for_band = _to_milli(severity_dec)
        else:
            severity_for_band = severity_milli
        bands_milli = _bands_milli(config)
        band = _band_name(bands_milli, severity_for_band)
        storm_band_names = _storm_band_names(bands_milli)
        condition_code = "storm" if band in storm_band_names else "rain"
    else:
        band = None
        thresholds = config.resolved["weather.condition_code_thresholds"]
        extreme_heat_tenths = _to_tenths(thresholds["extreme_heat_high_f"])
        hot_tenths = _to_tenths(thresholds["hot_high_f"])
        cool_tenths = _to_tenths(thresholds["cool_high_f"])
        high_hat_tenths = int(
            Decimal(high_hat_f * 10).to_integral_value(rounding=ROUND_HALF_UP)
        )
        if high_hat_tenths >= extreme_heat_tenths:
            condition_code = "extreme_heat"
        elif high_hat_tenths >= hot_tenths:
            condition_code = "hot"
        elif high_hat_tenths <= cool_tenths:
            condition_code = "cool"
        else:
            condition_code = "clear"

    issued_at = clock.now(conn)
    conn.execute(
        "INSERT OR IGNORE INTO _weather_forecast_cache "
        "(zone_id, query_date, target_date, high_hat_f, low_hat_f, storm_hat, "
        "condition_code, first_issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (zone_id, today, target_date, high_hat, low_hat, int(storm_hat), condition_code, issued_at),
    )

    result: dict[str, object] = {
        "zone_id": zone_id,
        "date": target_date,
        "kind": "forecast",
        "horizon_days": horizon,
        "high_f": high_hat,
        "low_f": low_hat,
        "precipitation_probability": _precipitation_probability(config, condition_code),
        "storm": storm_hat,
    }
    if storm_hat:
        result["storm_severity"] = band
    return result


def get_weather(
    conn: sqlite3.Connection,
    config: RunConfig,
    rng: Rng,
    *,
    zone_id: int,
    date: str,
    today: str,
) -> dict[str, object]:
    zone_id = _validate_zone_id(conn, zone_id)
    max_horizon_days = config.get_int("weather.forecast_max_horizon_days")
    horizon, today_d, target_d = _validate_horizon(today, date, max_horizon_days)
    today_iso = today_d.isoformat()
    target_iso = target_d.isoformat()

    row = conn.execute(
        "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli, condition_code "
        "FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (zone_id, target_iso),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"no precomputed weather for zone {zone_id} on {target_iso}",
        )

    high_f = _tenths_to_whole(row["high_f_tenths"])
    low_f = _tenths_to_whole(row["low_f_tenths"])
    storm_flag = bool(row["storm_flag"])

    if horizon == 0:
        condition_code = row["condition_code"]
        result: dict[str, object] = {
            "zone_id": zone_id,
            "date": target_iso,
            "kind": "observed",
            "horizon_days": 0,
            "high_f": high_f,
            "low_f": low_f,
            "precipitation_probability": _precipitation_probability(config, condition_code),
            "storm": storm_flag,
        }
        if storm_flag:
            result["storm_severity"] = _band_name(_bands_milli(config), row["storm_severity_milli"])
        return result

    return _forecast(
        conn,
        config,
        rng,
        zone_id=zone_id,
        today=today_iso,
        target_date=target_iso,
        horizon=horizon,
        high_f_tenths=row["high_f_tenths"],
        low_f_tenths=row["low_f_tenths"],
        storm_flag=storm_flag,
        severity_milli=row["storm_severity_milli"],
    )


__all__ = ["get_weather", "precompute"]
