from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from . import clock, constants, db, origin_keys, pools
from .config import RunConfig, probability_threshold
from .enums import EventKind, KeyedStreamName, OriginEntity, SequentialStreamName
from .errors import EngineError, ErrorCode
from .events import EventHandler, Events
from .rng import Rng, chance, derive, uniform01
from .txn import TxnContext

_STANDARD_NORMAL = statistics.NormalDist()

_MICROS = Decimal(1_000_000)

_UNIFORM_FLOAT_EPSILON = 2.0**-53
_UNIFORM_FLOAT_LOWER_BOUND = _UNIFORM_FLOAT_EPSILON
_UNIFORM_FLOAT_UPPER_BOUND = 1.0 - _UNIFORM_FLOAT_EPSILON

_SEGMENTS: frozenset[str] = frozenset({"residential", "commercial", "apartment"})

_POISSON_PRECISION = 40
_POISSON_MAX_K = 400


def _to_micros(value: Any) -> int:
    return int((Decimal(value) * _MICROS).to_integral_value(rounding=ROUND_HALF_UP))


def _from_micros(value: int) -> Decimal:
    return Decimal(value) / _MICROS


def _clamped_float(p: Decimal) -> float:
    return min(max(float(p), _UNIFORM_FLOAT_LOWER_BOUND), _UNIFORM_FLOAT_UPPER_BOUND)


def _validate_segment(segment: str) -> str:
    if segment not in _SEGMENTS:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"segment must be one of {sorted(_SEGMENTS)}, got {segment!r}"
        )
    return segment


def _ymd(service_date: str) -> tuple[int, int, int]:
    d = date.fromisoformat(service_date)
    return d.year, d.month, d.day


def _validate_catalog(conn: sqlite3.Connection, config: RunConfig) -> None:
    configured_version = config.get_string("demand.service_catalog_version")
    rows = conn.execute(
        "SELECT catalog_version, name, supported_job_type_id FROM _service_catalog "
        "WHERE active = 1"
    ).fetchall()
    if any(row["catalog_version"] != configured_version for row in rows):
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            f"demand.service_catalog_version {configured_version!r} does not match every "
            "active _service_catalog row",
        )
    supported_names = {row["name"] for row in rows if row["supported_job_type_id"] is not None}
    if supported_names != set(constants.SUPPORTED_CATEGORIES):
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            "constants.SUPPORTED_CATEGORIES does not exactly match the active _service_catalog "
            f"rows with a supported_job_type_id: expected {sorted(constants.SUPPORTED_CATEGORIES)}, "
            f"got {sorted(supported_names)}",
        )
    unsupported_names = {row["name"] for row in rows if row["supported_job_type_id"] is None}
    configured_unsupported = set(config.get_string_list("demand.unsupported_categories"))
    if unsupported_names != configured_unsupported:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            "demand.unsupported_categories does not exactly match the active _service_catalog "
            f"rows with a NULL supported_job_type_id: expected {sorted(unsupported_names)}, "
            f"got {sorted(configured_unsupported)}",
        )


def _validate_pool_versions(config: RunConfig) -> None:
    person_version = config.get_string("customers.person_name_pool_version")
    if person_version != pools.PERSON_NAME_POOL_VERSION:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            f"customers.person_name_pool_version {person_version!r} does not match the "
            f"loaded pool {pools.PERSON_NAME_POOL_VERSION!r}",
        )
    business_version = config.get_string("customers.business_name_pool_version")
    if business_version != pools.BUSINESS_NAME_POOL_VERSION:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            f"customers.business_name_pool_version {business_version!r} does not match the "
            f"loaded pool {pools.BUSINESS_NAME_POOL_VERSION!r}",
        )


def _validate_address_pool_floors(conn: sqlite3.Connection, config: RunConfig) -> None:
    zone_weights = config.resolved["demand.zone_weights"]
    zone_ids = [row["id"] for row in conn.execute("SELECT id FROM _zones ORDER BY id").fetchall()]
    for zone_id in zone_ids:
        if zone_weights[zone_id - 1] <= 0:
            continue
        for segment in sorted(_SEGMENTS):
            in_zone = _new_caller_pool(zone_id, segment, outlying=False).size(conn)
            outlying = _new_caller_pool(zone_id, segment, outlying=True).size(conn)
            if in_zone == 0 and outlying == 0:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"no usable address pool for zone {zone_id} segment {segment!r}",
                )


def initialize_state(conn: sqlite3.Connection, config: RunConfig, *, created_at: int) -> None:
    _validate_catalog(conn, config)
    _validate_pool_versions(config)
    _validate_address_pool_floors(conn, config)
    if conn.in_transaction:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            "demand.initialize_state requires no open transaction on the connection",
        )
    db.begin(conn)
    try:
        base_by_urgency = config.resolved["acceptance.base_probability_by_urgency"]
        decay_by_urgency = config.resolved["acceptance.lead_time_decay_by_urgency"]
        floor_by_urgency = config.resolved["acceptance.floor_probability_by_urgency"]
        conn.executemany(
            "INSERT INTO _acceptance_parameters "
            "(urgency_class, base_micros, decay_micros, floor_micros, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    urgency,
                    _to_micros(base_by_urgency[urgency]),
                    _to_micros(decay_by_urgency[urgency]),
                    _to_micros(floor_by_urgency[urgency]),
                    created_at,
                )
                for urgency in ("standard", "urgent")
            ],
        )
        conn.executemany(
            "INSERT INTO _word_of_mouth_state (segment, wom_micros, updated_at) VALUES (?, 0, ?)",
            [(segment, created_at) for segment in sorted(_SEGMENTS)],
        )
        conn.execute(
            "INSERT INTO _competitor_state "
            "(singleton, share_micros, capture_count, drift_micros, updated_at) "
            "VALUES (1, 0, 0, 0, ?)",
            (created_at,),
        )
    except Exception:
        db.rollback(conn)
        raise
    else:
        db.commit(conn)


def _weather_multiplier_micros(config: RunConfig, weather_row: sqlite3.Row, category_name: str) -> int:
    heat_sens = config.resolved["demand.heat_sensitivity_by_category"].get(category_name, Decimal(0))
    cold_sens = config.resolved["demand.cold_sensitivity_by_category"].get(category_name, Decimal(0))
    storm_sens = config.resolved["demand.storm_multiplier_by_category"].get(category_name, Decimal(0))
    heat_threshold = config.get_decimal("demand.heat_threshold_f")
    cold_threshold = config.get_decimal("demand.cold_threshold_f")
    norm = config.get_decimal("demand.temperature_normalization_f")

    high_f = Decimal(weather_row["high_f_tenths"]) / Decimal(10)
    low_f = Decimal(weather_row["low_f_tenths"]) / Decimal(10)

    heat_term = heat_sens * max(Decimal(0), high_f - heat_threshold) / norm
    cold_term = cold_sens * max(Decimal(0), cold_threshold - low_f) / norm
    if weather_row["storm_flag"]:
        severity = Decimal(weather_row["storm_severity_milli"]) / Decimal(1000)
        storm_term = storm_sens * severity
    else:
        storm_term = Decimal(0)

    raw = Decimal(1) + heat_term + cold_term + storm_term
    lo = config.get_decimal("demand.weather_multiplier_min")
    hi = config.get_decimal("demand.weather_multiplier_max")
    return _to_micros(min(max(raw, lo), hi))


def _weather_multipliers_by_name(config: RunConfig, weather_row: sqlite3.Row) -> dict[str, int]:
    return {
        name: _weather_multiplier_micros(config, weather_row, name)
        for name in constants.SUPPORTED_CATEGORIES
    }


def _category_distribution(
    config: RunConfig,
    catalog_rows: list[sqlite3.Row],
    segment: str,
    m_weather_by_name: dict[str, int] | None,
) -> list[tuple[int, str, Decimal]]:
    share = config.get_decimal("demand.unsupported_category_share")
    category_weights = config.resolved["demand.category_weights"][segment]

    supported = [row for row in catalog_rows if row["supported_job_type_id"] is not None]
    unsupported = [row for row in catalog_rows if row["supported_job_type_id"] is None]

    def _raw_supported(row: sqlite3.Row) -> Decimal:
        weight = category_weights[row["name"]]
        if m_weather_by_name is not None:
            weight = weight * _from_micros(m_weather_by_name.get(row["name"], _to_micros(1)))
        return weight

    def _raw_unsupported(row: sqlite3.Row) -> Decimal:
        return category_weights[row["name"]]

    supported_raw = [(row, _raw_supported(row)) for row in supported]
    unsupported_raw = [(row, _raw_unsupported(row)) for row in unsupported]
    supported_total = sum((w for _, w in supported_raw), Decimal(0))
    unsupported_total = sum((w for _, w in unsupported_raw), Decimal(0))

    dist: list[tuple[int, str, Decimal]] = []
    for row, weight in supported_raw:
        mass = (Decimal(1) - share) * weight / supported_total
        dist.append((row["id"], row["name"], mass))
    for row, weight in unsupported_raw:
        mass = share * weight / unsupported_total
        dist.append((row["id"], row["name"], mass))
    dist.sort(key=lambda item: item[0])
    return dist


def _volume_weather_multiplier_micros(
    config: RunConfig,
    catalog_rows: list[sqlite3.Row],
    weather_row: sqlite3.Row,
    m_weather_by_name: dict[str, int],
) -> int:
    segment_weights = config.resolved["demand.segment_weights"]
    composite: dict[int, Decimal] = {}
    category_m: dict[int, Decimal] = {}
    for segment, segment_weight in segment_weights.items():
        for category_id, name, mass in _category_distribution(config, catalog_rows, segment, None):
            composite[category_id] = composite.get(category_id, Decimal(0)) + segment_weight * mass
            category_m[category_id] = _from_micros(m_weather_by_name.get(name, _to_micros(1)))
    total_weight = sum(composite.values(), Decimal(0))
    if total_weight <= 0:
        return _to_micros(1)
    weighted_sum = sum(
        (composite[category_id] * category_m[category_id] for category_id in composite), Decimal(0)
    )
    return _to_micros(weighted_sum / total_weight)


def _reputation_multiplier_micros(conn: sqlite3.Connection, config: RunConfig, now_ms: int) -> int:
    window_days = config.get_int("demand.reputation_window_days")
    window_ms = window_days * 86_400_000
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(stars), 0) s FROM _reviews "
        "WHERE published_at <= ? AND published_at > ?",
        (now_ms, now_ms - window_ms),
    ).fetchone()
    count = row["n"]
    if count == 0:
        return _to_micros(1)
    mean_stars = Decimal(row["s"]) / Decimal(count)
    neutral = config.get_decimal("demand.reputation_neutral_stars")
    sensitivity = config.get_decimal("demand.reputation_sensitivity")
    lo = config.get_decimal("demand.reputation_multiplier_min")
    hi = config.get_decimal("demand.reputation_multiplier_max")
    raw = Decimal(1) + sensitivity * (mean_stars - neutral)
    return _to_micros(min(max(raw, lo), hi))


def _word_of_mouth_multiplier_micros(conn: sqlite3.Connection, config: RunConfig) -> int:
    rows = conn.execute("SELECT segment, wom_micros FROM _word_of_mouth_state").fetchall()
    wom_by_segment = {row["segment"]: row["wom_micros"] for row in rows}
    weights = config.resolved["demand.word_of_mouth_segment_weights"]
    total = sum(
        (weights[segment] * _from_micros(wom_by_segment.get(segment, 0)) for segment in weights),
        Decimal(0),
    )
    sensitivity = config.get_decimal("demand.word_of_mouth_sensitivity")
    lo = config.get_decimal("demand.word_of_mouth_multiplier_min")
    hi = config.get_decimal("demand.word_of_mouth_multiplier_max")
    raw = Decimal(1) + sensitivity * total
    return _to_micros(min(max(raw, lo), hi))


def _competitor_multiplier_micros(conn: sqlite3.Connection, config: RunConfig) -> int:
    row = conn.execute("SELECT share_micros FROM _competitor_state WHERE singleton = 1").fetchone()
    if row is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "missing _competitor_state singleton row")
    share = _from_micros(row["share_micros"])
    coeff = config.get_decimal("competitors.demand_erosion_coefficient")
    lo = config.get_decimal("competitors.demand_multiplier_min")
    raw = Decimal(1) - coeff * share
    return _to_micros(min(max(raw, lo), Decimal(1)))


def poisson(lambda_micros: int, u: int) -> int:
    if lambda_micros == 0:
        return 0
    with localcontext() as ctx:
        ctx.prec = _POISSON_PRECISION
        lam = Decimal(lambda_micros) / _MICROS
        uniform = uniform01(u)
        term = (-lam).exp()
        cdf = term
        k = 0
        while uniform >= cdf and k < _POISSON_MAX_K:
            k += 1
            term *= lam / k
            cdf += term
        if k >= _POISSON_MAX_K and uniform >= cdf:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"poisson sample exceeded the k-cap of {_POISSON_MAX_K} for lambda={lam}; "
                "demand.base_new_caller_rate_per_day and its multipliers must keep the "
                "effective lambda within the cap",
            )
        return k


def _weighted_choice(items: list[tuple[Any, int]], u: int) -> Any:
    total = sum(weight for _, weight in items)
    if total <= 0:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "cannot choose from a non-positive weight total")
    r = u % total
    cumulative = 0
    for value, weight in items:
        cumulative += weight
        if r < cumulative:
            return value
    return items[-1][0]


def _pick_segment(config: RunConfig, u: int) -> str:
    weights = config.resolved["demand.segment_weights"]
    items = [(segment, _to_micros(weight)) for segment, weight in sorted(weights.items())]
    return _weighted_choice(items, u)


def _pick_arrival_hour(config: RunConfig, u: int) -> int:
    weights = config.resolved["demand.arrival_time_of_day_weights"]
    items = [(7 + i, _to_micros(weight)) for i, weight in enumerate(weights)]
    return _weighted_choice(items, u)


def _urgency_probability(config: RunConfig, hour: int, m_category_micros: int) -> Decimal:
    base = (
        config.get_decimal("demand.after_hours_urgent_probability")
        if hour >= 17
        else config.get_decimal("demand.office_hours_urgent_probability")
    )
    bonus_coeff = config.get_decimal("demand.urgency_weather_bonus")
    bonus = bonus_coeff * max(Decimal(0), _from_micros(m_category_micros) - Decimal(1))
    return min(max(base + bonus, Decimal(0)), Decimal(1))


def _segment_literal(segment: str) -> str:
    if segment not in _SEGMENTS:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"address pool segment must be one of {sorted(_SEGMENTS)}, got {segment!r}",
        )
    return segment


_ADDRESS_PROBE_LIMIT = 64


@dataclass(frozen=True)
class _AddressPool:

    zone_clause: str
    zone_id: int
    segment: str

    def sql(self, projection: str, tail: str = "") -> str:
        return (
            f"SELECT {projection} FROM _address_catalog ac WHERE ac.status = 'usable' "
            f"AND {self.zone_clause} "
            f"AND (',' || ac.segment_eligibility || ',') LIKE '%,{_segment_literal(self.segment)},%'"
            f"{tail}"
        )

    def size(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute(self.sql("COUNT(*) AS n"), (self.zone_id,)).fetchone()["n"])

    def member(self, conn: sqlite3.Connection, index: int) -> int:
        row = conn.execute(
            self.sql("ac.id", " ORDER BY ac.id LIMIT 1 OFFSET ?"), (self.zone_id, index)
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"address pool for zone {self.zone_id} segment {self.segment!r} has no entry "
                f"at index {index}",
            )
        return int(row["id"])

    def available_members(self, conn: sqlite3.Connection) -> list[int]:
        unoccupied = (
            " AND NOT EXISTS (SELECT 1 FROM _locations l "
            "WHERE l.address_catalog_id = ac.id AND l.active = 1) ORDER BY ac.id"
        )
        return [int(row["id"]) for row in conn.execute(self.sql("ac.id", unoccupied), (self.zone_id,))]


def _new_caller_pool(zone_id: int, segment: str, *, outlying: bool) -> _AddressPool:
    zone_clause = "ac.zone_id IS NULL AND ac.effective_zone_id = ?" if outlying else "ac.zone_id = ?"
    return _AddressPool(zone_clause, zone_id, segment)


def _new_location_pool(zone_id: int, segment: str) -> _AddressPool:
    return _AddressPool("ac.effective_zone_id = ?", zone_id, segment)


def _address_occupied(conn: sqlite3.Connection, address_catalog_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM _locations WHERE address_catalog_id = ? AND active = 1 LIMIT 1",
        (address_catalog_id,),
    ).fetchone()
    return row is not None


def _pick_available_address(conn: sqlite3.Connection, pool: _AddressPool, u: int) -> int | None:
    size = pool.size(conn)
    if size == 0:
        return None
    for probe in range(_ADDRESS_PROBE_LIMIT):
        candidate = pool.member(conn, derive(u, f"address_probe:{probe}") % size)
        if not _address_occupied(conn, candidate):
            return candidate
    available = pool.available_members(conn)
    if not available:
        return None
    return max(available, key=lambda address_id: derive(u, f"address:{address_id}"))


def _zone_of_customer(conn: sqlite3.Connection, customer_id: int) -> int:
    row = conn.execute(
        "SELECT l.effective_zone_id AS zone_id FROM _jobs j "
        "JOIN _locations l ON l.id = j.location_id "
        "WHERE j.customer_id = ? AND j.status = 'completed' "
        "ORDER BY j.completed_at DESC, j.id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    if row is not None:
        return row["zone_id"]
    row = conn.execute(
        "SELECT effective_zone_id AS zone_id FROM _locations "
        "WHERE customer_id = ? AND active = 1 "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"customer {customer_id} has no active location to derive zone_of from",
        )
    return row["zone_id"]


def _customer_caller_name(conn: sqlite3.Connection, customer_id: int) -> str:
    row = conn.execute("SELECT name FROM _customers WHERE id = ?", (customer_id,)).fetchone()
    return row["name"] if row is not None else ""


def _customer_caller_phone(conn: sqlite3.Connection, customer_id: int) -> str:
    row = conn.execute(
        "SELECT value FROM _customer_contacts WHERE customer_id = ? AND contact_type = 'phone' "
        "ORDER BY id LIMIT 1",
        (customer_id,),
    ).fetchone()
    return row["value"] if row is not None else ""


def _generate_new_caller_slot(
    conn: sqlite3.Connection,
    config: RunConfig,
    rng: Rng,
    *,
    zone_id: int,
    service_date: str,
    slot_index: int,
    catalog_rows: list[sqlite3.Row],
    m_by_name: dict[str, int],
    phone_prefixes: list[str],
) -> dict[str, Any]:
    scope = f"{service_date}:{slot_index}"
    event_kind = "new_call_attributes"

    def draw(ordinal: int) -> int:
        return rng.draw(KeyedStreamName.DEMAND.value, "zone", zone_id, scope, event_kind, ordinal)

    segment = _pick_segment(config, draw(0))

    dist = _category_distribution(config, catalog_rows, segment, m_by_name)
    category_id, category_name = _weighted_choice(
        [((row_id, name), _to_micros(mass)) for row_id, name, mass in dist], draw(1)
    )

    want_outlying = chance(draw(2), config.get_prob_threshold("demand.outlying_address_share"))
    u3 = draw(3)
    address_catalog_id = _pick_available_address(
        conn, _new_caller_pool(zone_id, segment, outlying=want_outlying), u3
    )
    if address_catalog_id is None:
        address_catalog_id = _pick_available_address(
            conn, _new_caller_pool(zone_id, segment, outlying=not want_outlying), u3
        )
    if address_catalog_id is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"no usable address pool for zone {zone_id} segment {segment!r}",
        )

    hour = _pick_arrival_hour(config, draw(8))
    minute = draw(9) % 60
    arrival_at = clock.phoenix_instant(*_ymd(service_date), hour, minute)

    returning_flag = chance(draw(6), config.get_prob_threshold("demand.returning_prospect_probability"))
    prospects: list[sqlite3.Row] = []
    if returning_flag:
        prospects = conn.execute(
            "SELECT id, identity_key, caller_name, caller_phone FROM _prospect_state "
            "WHERE home_zone_id = ? AND segment = ? AND registered_customer_id IS NULL",
            (zone_id, segment),
        ).fetchall()
    if returning_flag and prospects:
        u7 = draw(7)
        prospect_row = max(
            prospects,
            key=lambda row: (derive(u7, f"prospect:{row['identity_key']}"), row["identity_key"]),
        )
        caller_name = prospect_row["caller_name"]
        caller_phone = prospect_row["caller_phone"]
        prospect_id: int | None = prospect_row["id"]
    else:
        caller_name, caller_phone = pools.compose_identity(draw(4), segment, phone_prefixes)
        prospect_id = None

    m_category_micros = m_by_name.get(category_name, _to_micros(1))
    p_urgent = _urgency_probability(config, hour, m_category_micros)
    urgent = chance(draw(5), probability_threshold(p_urgent))

    return {
        "service_date": service_date,
        "zone_id": zone_id,
        "arrival_at": arrival_at,
        "channel": "new_caller",
        "segment": segment,
        "requested_category_id": category_id,
        "urgent": urgent,
        "customer_id": None,
        "location_id": None,
        "address_catalog_id": address_catalog_id,
        "caller_name": caller_name,
        "caller_phone": caller_phone,
        "prospect_id": prospect_id,
        "slot_index": slot_index,
        "origin_key": origin_keys.new_caller(service_date, zone_id, slot_index),
    }


def _generate_repeat_slot(
    conn: sqlite3.Connection,
    config: RunConfig,
    rng: Rng,
    *,
    customer_id: int,
    segment: str,
    service_date: str,
    weekday: int,
    catalog_rows: list[sqlite3.Row],
    m_weather_volume_micros: dict[int, int],
    m_weather_by_name_per_zone: dict[int, dict[str, int]],
) -> dict[str, Any] | None:
    zone_of = _zone_of_customer(conn, customer_id)
    prop_row = conn.execute(
        "SELECT propensity_micros FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    if prop_row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"missing _customer_repeat_state row for customer {customer_id}",
        )
    customer_origin = origin_keys.of_customer(conn, customer_id)
    scope = origin_keys.scoped(customer_origin, service_date)

    dow_mult = config.resolved["demand.day_of_week_multipliers"][weekday]
    base_prob = config.get_decimal("demand.repeat_call_daily_base_probability")
    propensity = _from_micros(prop_row["propensity_micros"])
    m_weather_vol = _from_micros(m_weather_volume_micros[zone_of])
    p_call = min(max(propensity * base_prob * m_weather_vol * dow_mult, Decimal(0)), Decimal(1))

    u_hit = rng.draw(
        KeyedStreamName.DEMAND.value, OriginEntity.CUSTOMER.value, origin_keys.ENTITY_ID, scope,
        "repeat_call", 0,
    )
    if not chance(u_hit, probability_threshold(p_call)):
        return None

    event_kind = "repeat_call_attributes"

    def draw(ordinal: int) -> int:
        return rng.draw(
            KeyedStreamName.DEMAND.value, OriginEntity.CUSTOMER.value, origin_keys.ENTITY_ID, scope,
            event_kind, ordinal,
        )

    m_by_name = m_weather_by_name_per_zone[zone_of]
    dist = _category_distribution(config, catalog_rows, segment, m_by_name)
    category_id, category_name = _weighted_choice(
        [((row_id, name), _to_micros(mass)) for row_id, name, mass in dist], draw(0)
    )

    want_new_location = chance(
        draw(1), config.get_prob_threshold("demand.existing_customer_new_location_probability")
    )
    existing_location_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM _locations WHERE customer_id = ? AND active = 1 ORDER BY id",
            (customer_id,),
        ).fetchall()
    ]

    u2 = draw(2)
    address_catalog_id = (
        _pick_available_address(conn, _new_location_pool(zone_of, segment), u2)
        if want_new_location
        else None
    )
    location_id: int | None = None
    if address_catalog_id is None:
        if not existing_location_ids:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"customer {customer_id} has no active location to fall back to",
            )
        location_id = existing_location_ids[u2 % len(existing_location_ids)]

    hour = _pick_arrival_hour(config, draw(4))
    minute = draw(5) % 60
    arrival_at = clock.phoenix_instant(*_ymd(service_date), hour, minute)

    m_category_micros = m_by_name.get(category_name, _to_micros(1))
    p_urgent = _urgency_probability(config, hour, m_category_micros)
    urgent = chance(draw(3), probability_threshold(p_urgent))

    return {
        "service_date": service_date,
        "zone_id": zone_of,
        "arrival_at": arrival_at,
        "channel": "existing_customer",
        "segment": segment,
        "requested_category_id": category_id,
        "urgent": urgent,
        "customer_id": customer_id,
        "location_id": location_id,
        "address_catalog_id": address_catalog_id,
        "caller_name": _customer_caller_name(conn, customer_id),
        "caller_phone": _customer_caller_phone(conn, customer_id),
        "prospect_id": None,
        "slot_index": None,
        "origin_key": origin_keys.repeat_call(service_date, customer_origin),
    }


def _advance_competitor_background(
    conn: sqlite3.Connection, config: RunConfig, rng: Rng, *, now_ms: int
) -> None:
    u = rng.stream(SequentialStreamName.COMPETITOR_BACKGROUND.value).next()
    z = _STANDARD_NORMAL.inv_cdf(_clamped_float(uniform01(u)))
    sd = float(config.get_decimal("competitors.background_drift_sd"))
    drift_f = z * sd
    drift_micros = int(Decimal(drift_f * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))

    row = conn.execute("SELECT share_micros FROM _competitor_state WHERE singleton = 1").fetchone()
    if row is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "missing _competitor_state singleton row")
    share_micros = row["share_micros"]
    if config.get_bool("competitors.share_recovery_enabled"):
        decay_micros = _to_micros(config.get_decimal("competitors.share_decay_per_day"))
        share_micros = max(0, share_micros - decay_micros)

    conn.execute(
        "UPDATE _competitor_state SET share_micros = ?, drift_micros = ?, updated_at = ? "
        "WHERE singleton = 1",
        (share_micros, drift_micros, now_ms),
    )


def generate_for_date(
    conn: sqlite3.Connection, config: RunConfig, rng: Rng, *, service_date: str, now_ms: int
) -> dict[str, Any]:
    if not rng.is_txn_open():
        raise EngineError(
            ErrorCode.DRAW_OUTSIDE_TXN,
            "demand.generate_for_date requires an already-open rng transaction "
            "(db.begin + rng.enter_txn, or the privileged() event-handler path)",
        )

    weekday = date.fromisoformat(service_date).weekday()
    zone_ids = [row["id"] for row in conn.execute("SELECT id FROM _zones ORDER BY id").fetchall()]
    catalog_rows = list(
        conn.execute(
            "SELECT id, name, supported_job_type_id FROM _service_catalog "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
    )
    phone_prefixes = config.get_string_list("customers.phone_prefix_pool")
    base_rate = config.get_decimal("demand.base_new_caller_rate_per_day")
    zone_weights = config.resolved["demand.zone_weights"]
    dow_mult = config.resolved["demand.day_of_week_multipliers"][weekday]

    m_reputation_micros = _reputation_multiplier_micros(conn, config, now_ms)
    m_wom_micros = _word_of_mouth_multiplier_micros(conn, config)
    m_competitor_micros = _competitor_multiplier_micros(conn, config)

    m_weather_volume_micros: dict[int, int] = {}
    m_weather_by_name_per_zone: dict[int, dict[str, int]] = {}
    for zone_id in zone_ids:
        weather_row = conn.execute(
            "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli "
            "FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
            (zone_id, service_date),
        ).fetchone()
        if weather_row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"no precomputed weather for zone {zone_id} on {service_date} "
                "(weather.precompute must cover every service date)",
            )
        m_by_name = _weather_multipliers_by_name(config, weather_row)
        m_weather_by_name_per_zone[zone_id] = m_by_name
        m_weather_volume_micros[zone_id] = _volume_weather_multiplier_micros(
            config, catalog_rows, weather_row, m_by_name
        )

    call_specs: list[dict[str, Any]] = []
    new_caller_count = 0
    for zone_id in zone_ids:
        lam = (
            base_rate
            * zone_weights[zone_id - 1]
            * _from_micros(m_weather_volume_micros[zone_id])
            * dow_mult
            * _from_micros(m_reputation_micros)
            * _from_micros(m_wom_micros)
            * _from_micros(m_competitor_micros)
        )
        lambda_micros = _to_micros(lam)
        u_count = rng.draw(
            KeyedStreamName.DEMAND.value, "zone", zone_id, service_date, "new_caller_count", 0
        )
        n_new = poisson(lambda_micros, u_count)
        new_caller_count += n_new
        m_by_name = m_weather_by_name_per_zone[zone_id]
        for k in range(n_new):
            call_specs.append(
                _generate_new_caller_slot(
                    conn,
                    config,
                    rng,
                    zone_id=zone_id,
                    service_date=service_date,
                    slot_index=k,
                    catalog_rows=catalog_rows,
                    m_by_name=m_by_name,
                    phone_prefixes=phone_prefixes,
                )
            )

    repeat_hits = 0
    customer_rows = conn.execute(
        "SELECT c.id, c.segment FROM _customers c WHERE c.status = 'active' "
        "AND EXISTS (SELECT 1 FROM _locations l WHERE l.customer_id = c.id AND l.active = 1) "
        "ORDER BY c.id"
    ).fetchall()
    for customer_row in customer_rows:
        spec = _generate_repeat_slot(
            conn,
            config,
            rng,
            customer_id=customer_row["id"],
            segment=customer_row["segment"],
            service_date=service_date,
            weekday=weekday,
            catalog_rows=catalog_rows,
            m_weather_volume_micros=m_weather_volume_micros,
            m_weather_by_name_per_zone=m_weather_by_name_per_zone,
        )
        if spec is not None:
            call_specs.append(spec)
            repeat_hits += 1

    call_specs.sort(key=lambda s: (s["arrival_at"], s["origin_key"]))

    events = Events(conn, rng)
    for spec in call_specs:
        cur = conn.execute(
            "INSERT INTO _pending_calls "
            "(service_date, zone_id, arrival_at, call_kind, channel, segment, "
            "requested_category_id, urgent, customer_id, location_id, address_catalog_id, "
            "caller_name, caller_phone, prospect_id, slot_index, origin_key, created_at) "
            "VALUES (?, ?, ?, 'new_job', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spec["service_date"],
                spec["zone_id"],
                spec["arrival_at"],
                spec["channel"],
                spec["segment"],
                spec["requested_category_id"],
                int(spec["urgent"]),
                spec["customer_id"],
                spec["location_id"],
                spec["address_catalog_id"],
                spec["caller_name"],
                spec["caller_phone"],
                spec["prospect_id"],
                spec["slot_index"],
                spec["origin_key"],
                now_ms,
            ),
        )
        pending_id = int(cur.lastrowid)
        event_id = events.schedule(
            EventKind.CALL_INVOCATION,
            spec["arrival_at"],
            entity_type="pending_call",
            entity_id=pending_id,
            order_key=spec["origin_key"],
        )
        conn.execute("UPDATE _pending_calls SET event_id = ? WHERE id = ?", (event_id, pending_id))

    _advance_competitor_background(conn, config, rng, now_ms=now_ms)

    return {
        "service_date": service_date,
        "new_callers": new_caller_count,
        "repeat_calls": repeat_hits,
        "total_calls": len(call_specs),
    }


def _validate_service_date(value: Any) -> str:
    if not isinstance(value, str):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"service_date must be a YYYY-MM-DD string, got {value!r}"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"service_date must be a YYYY-MM-DD string, got {value!r}"
        ) from exc
    if parsed.isoformat() != value:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"service_date must be a canonical YYYY-MM-DD string, got {value!r}"
        )
    return value


def demand_generation_handler(config: RunConfig) -> EventHandler:

    def handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        service_date = _validate_service_date(payload.get("service_date"))
        generate_for_date(ctx.conn, config, ctx.rng, service_date=service_date, now_ms=ctx.now_ms)
        return None

    return handler


def _multiply_micros(value_micros: int, multiplier: Decimal) -> int:
    return _to_micros(_from_micros(value_micros) * multiplier)


def _clamp_micros(value_micros: int, lo: Decimal, hi: Decimal) -> int:
    return min(max(value_micros, _to_micros(lo)), _to_micros(hi))


def register_customer_repeat_state(
    conn: sqlite3.Connection,
    config: RunConfig,
    *,
    customer_id: int,
    segment: str,
    origin_key: str,
    now_ms: int,
) -> None:
    _validate_segment(segment)
    initial = config.resolved["demand.repeat_propensity_initial_by_segment"][segment]
    conn.execute(
        "INSERT INTO _customer_repeat_state "
        "(customer_id, propensity_micros, last_outcome, competitor_captured_at, updated_at, "
        "origin_key) VALUES (?, ?, NULL, NULL, ?, ?)",
        (customer_id, _to_micros(initial), now_ms, origin_key),
    )


def apply_rejection_consequence(
    conn: sqlite3.Connection,
    config: RunConfig,
    *,
    now_ms: int,
    customer_id: int | None = None,
    prospect: dict[str, Any] | None = None,
) -> int | None:
    if (customer_id is None) == (prospect is None):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            "apply_rejection_consequence requires exactly one of customer_id or prospect",
        )
    multiplier = config.get_decimal("demand.rejection_repeat_propensity_multiplier")
    floor = config.get_decimal("customers.repeat_propensity_floor")
    ceiling = config.get_decimal("customers.repeat_propensity_max")

    if customer_id is not None:
        row = conn.execute(
            "SELECT propensity_micros FROM _customer_repeat_state WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"missing _customer_repeat_state row for customer {customer_id}",
            )
        new_micros = _clamp_micros(_multiply_micros(row["propensity_micros"], multiplier), floor, ceiling)
        conn.execute(
            "UPDATE _customer_repeat_state SET propensity_micros = ?, last_outcome = ?, "
            "updated_at = ? WHERE customer_id = ?",
            (new_micros, "rejected", now_ms, customer_id),
        )
        return None

    assert prospect is not None
    segment = _validate_segment(prospect["segment"])
    caller_name = prospect["caller_name"]
    caller_phone = prospect["caller_phone"]
    home_zone_id = prospect.get("home_zone_id")
    identity_key = f"{caller_phone}|{caller_name}"

    row = conn.execute(
        "SELECT id, propensity_micros FROM _prospect_state WHERE identity_key = ?", (identity_key,)
    ).fetchone()
    if row is None:
        initial = config.resolved["demand.repeat_propensity_initial_by_segment"][segment]
        cur = conn.execute(
            "INSERT INTO _prospect_state "
            "(identity_key, caller_name, caller_phone, segment, home_zone_id, propensity_micros, "
            "last_outcome, competitor_captured_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                identity_key, caller_name, caller_phone, segment, home_zone_id,
                _to_micros(initial), now_ms, now_ms,
            ),
        )
        prospect_id = int(cur.lastrowid)
        current_micros = _to_micros(initial)
    else:
        prospect_id = row["id"]
        current_micros = row["propensity_micros"]

    new_micros = _clamp_micros(_multiply_micros(current_micros, multiplier), floor, ceiling)
    conn.execute(
        "UPDATE _prospect_state SET propensity_micros = ?, last_outcome = ?, updated_at = ? "
        "WHERE id = ?",
        (new_micros, "rejected", now_ms, prospect_id),
    )
    return prospect_id


def apply_competitor_capture(
    conn: sqlite3.Connection,
    config: RunConfig,
    *,
    now_ms: int,
    customer_id: int | None = None,
    prospect_id: int | None = None,
) -> None:
    if (customer_id is None) == (prospect_id is None):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            "apply_competitor_capture requires exactly one of customer_id or prospect_id",
        )
    multiplier = config.get_decimal("competitors.served_well_repeat_multiplier")
    floor = config.get_decimal("customers.repeat_propensity_floor")
    ceiling = config.get_decimal("customers.repeat_propensity_max")

    if customer_id is not None:
        row = conn.execute(
            "SELECT propensity_micros FROM _customer_repeat_state WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"missing _customer_repeat_state row for customer {customer_id}",
            )
        new_micros = _clamp_micros(_multiply_micros(row["propensity_micros"], multiplier), floor, ceiling)
        conn.execute(
            "UPDATE _customer_repeat_state SET propensity_micros = ?, competitor_captured_at = ?, "
            "updated_at = ? WHERE customer_id = ?",
            (new_micros, now_ms, now_ms, customer_id),
        )
    else:
        row = conn.execute(
            "SELECT propensity_micros FROM _prospect_state WHERE id = ?", (prospect_id,)
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED, f"missing _prospect_state row for id {prospect_id}"
            )
        new_micros = _clamp_micros(_multiply_micros(row["propensity_micros"], multiplier), floor, ceiling)
        conn.execute(
            "UPDATE _prospect_state SET propensity_micros = ?, competitor_captured_at = ?, "
            "updated_at = ? WHERE id = ?",
            (new_micros, now_ms, now_ms, prospect_id),
        )

    comp = conn.execute(
        "SELECT share_micros, capture_count FROM _competitor_state WHERE singleton = 1"
    ).fetchone()
    if comp is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "missing _competitor_state singleton row")
    gain_micros = _to_micros(config.get_decimal("competitors.share_gain_per_capture"))
    new_share = min(comp["share_micros"] + gain_micros, 1_000_000)
    conn.execute(
        "UPDATE _competitor_state SET share_micros = ?, capture_count = ?, updated_at = ? "
        "WHERE singleton = 1",
        (new_share, comp["capture_count"] + 1, now_ms),
    )


def update_word_of_mouth(
    conn: sqlite3.Connection,
    config: RunConfig,
    *,
    segment: str,
    satisfaction_delta: Decimal,
    now_ms: int,
) -> None:
    _validate_segment(segment)
    row = conn.execute(
        "SELECT wom_micros FROM _word_of_mouth_state WHERE segment = ?", (segment,)
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, f"missing _word_of_mouth_state row for segment {segment}"
        )
    decay = config.get_decimal("demand.word_of_mouth_decay")
    positive_weight = config.get_decimal("demand.word_of_mouth_positive_weight")
    negative_weight = config.get_decimal("demand.word_of_mouth_negative_weight")
    delta = Decimal(satisfaction_delta)
    weight = positive_weight if delta >= 0 else negative_weight
    current = _from_micros(row["wom_micros"])
    updated = current * (Decimal(1) - decay) + decay * weight * delta
    conn.execute(
        "UPDATE _word_of_mouth_state SET wom_micros = ?, updated_at = ? WHERE segment = ?",
        (_to_micros(updated), now_ms, segment),
    )


def transfer_prospect_to_customer(
    conn: sqlite3.Connection, *, prospect_id: int, customer_id: int, origin_key: str, now_ms: int
) -> None:
    row = conn.execute(
        "SELECT propensity_micros, last_outcome, competitor_captured_at, registered_customer_id "
        "FROM _prospect_state WHERE id = ?",
        (prospect_id,),
    ).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"prospect {prospect_id} not found")
    if row["registered_customer_id"] is not None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"prospect {prospect_id} is already registered as customer "
            f"{row['registered_customer_id']}",
        )
    conn.execute(
        "INSERT INTO _customer_repeat_state "
        "(customer_id, propensity_micros, last_outcome, competitor_captured_at, updated_at, "
        "origin_key) VALUES (?, ?, ?, ?, ?, ?)",
        (
            customer_id, row["propensity_micros"], row["last_outcome"],
            row["competitor_captured_at"], now_ms, origin_key,
        ),
    )
    conn.execute(
        "UPDATE _prospect_state SET registered_customer_id = ? WHERE id = ?",
        (customer_id, prospect_id),
    )


__all__ = [
    "apply_competitor_capture",
    "apply_rejection_consequence",
    "demand_generation_handler",
    "generate_for_date",
    "initialize_state",
    "poisson",
    "register_customer_repeat_state",
    "transfer_prospect_to_customer",
    "update_word_of_mouth",
]
