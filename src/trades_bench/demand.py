from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import clock, constants, db, origin_keys, pools
from .config import RunConfig, probability_threshold
from .enums import EventKind, KeyedStreamName
from .errors import EngineError, ErrorCode
from .events import EventHandler, Events
from .rng import Rng, chance, derive
from .txn import TxnContext

_MICROS = Decimal(1_000_000)

_SEGMENTS: frozenset[str] = frozenset({"residential", "commercial", "apartment"})

_MICROS_INT = 1_000_000

_SATURDAY = 5

_INSTALL_CATEGORY = "HVAC installation"
_SERVICE_CATEGORIES: frozenset[str] = (
    frozenset(constants.SUPPORTED_CATEGORIES) - {_INSTALL_CATEGORY}
)

_INVALID = "invalid"
_INSTALL = "install"
_SERVICE = "service"

_DAY_ENTITY = "demand_day"
_SLOT_ENTITY = "call_slot"


def _to_micros(value: Any) -> int:
    return int((Decimal(value) * _MICROS).to_integral_value(rounding=ROUND_HALF_UP))


def _from_micros(value: int) -> Decimal:
    return Decimal(value) / _MICROS


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


def _category_items(
    config: RunConfig,
    catalog_rows: list[sqlite3.Row],
    segment: str,
    names: frozenset[str],
    m_weather_by_name: dict[str, int] | None,
) -> list[tuple[tuple[int, str], int]]:
    category_weights = config.resolved["demand.category_weights"][segment]
    items: list[tuple[tuple[int, str], int]] = []
    for row in catalog_rows:
        if row["name"] not in names:
            continue
        weight = category_weights[row["name"]]
        if m_weather_by_name is not None:
            weight = weight * _from_micros(m_weather_by_name.get(row["name"], _to_micros(1)))
        items.append(((row["id"], row["name"]), _to_micros(weight)))
    return items


def demand_level_micros(conn: sqlite3.Connection, config: RunConfig, service_date: str) -> int:
    start = date.fromisoformat(config.get_string("run.start_date"))
    target = date.fromisoformat(service_date)
    gain = _to_micros(config.get_decimal("demand.level_gain_per_on_time_visit"))
    loss = _to_micros(config.get_decimal("demand.level_loss_per_bad_visit"))
    ceiling = _to_micros(config.get_decimal("demand.level_max"))
    completed: dict[str, int] = {}
    bad: dict[str, int] = {}
    rows = conn.execute(
        "SELECT scheduled_start, status FROM _appointments WHERE origin = 'run' "
        "AND status IN ('completed', 'no_show', 'failed') AND scheduled_start < ?",
        (clock.phoenix_instant(target.year, target.month, target.day, 0, 0),),
    ).fetchall()
    for row in rows:
        local_date = clock.service_date_for_ms(int(row["scheduled_start"]))
        tally = completed if row["status"] == "completed" else bad
        tally[local_date] = tally.get(local_date, 0) + 1
    level = _to_micros(config.get_decimal("demand.level_start"))
    day = start
    while day < target:
        key = day.isoformat()
        level = level + gain * completed.get(key, 0) - loss * bad.get(key, 0)
        level = min(max(level, 0), ceiling)
        day += timedelta(days=1)
    return level


def daily_call_target_micros(config: RunConfig, level_micros: int, service_date: str) -> int:
    if date.fromisoformat(service_date).weekday() < _SATURDAY:
        return level_micros
    multiplier_micros = _to_micros(config.get_decimal("demand.weekend_level_multiplier"))
    target = level_micros * multiplier_micros // _MICROS_INT
    floor_micros = _to_micros(config.get_decimal("demand.weekend_level_floor"))
    if level_micros >= floor_micros:
        target = max(target, floor_micros)
    return target


def _day_draw(rng: Rng, service_date: str, event_kind: str) -> int:
    return rng.draw(
        KeyedStreamName.DEMAND.value, _DAY_ENTITY, origin_keys.ENTITY_ID, service_date,
        event_kind, 0,
    )


def _uniform_int(u: int, lo: int, hi: int) -> int:
    return lo + u % (hi - lo + 1)


def daily_call_mix(
    config: RunConfig, rng: Rng, *, service_date: str, target_micros: int
) -> tuple[int, int, int]:
    whole, fraction = divmod(target_micros, _MICROS_INT)
    extra = chance(
        _day_draw(rng, service_date, "call_count_fraction"),
        probability_threshold(_from_micros(fraction)),
    )
    total = whole + int(extra)
    invalid_drawn = _uniform_int(
        _day_draw(rng, service_date, "invalid_call_count"),
        config.get_int("demand.invalid_calls_min"),
        config.get_int("demand.invalid_calls_max"),
    )
    install_drawn = _uniform_int(
        _day_draw(rng, service_date, "install_call_count"),
        0,
        config.get_int("demand.install_calls_max"),
    )
    invalid = min(invalid_drawn, total)
    install = min(install_drawn, total - invalid)
    return total, invalid, install


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


def _pick_zone(config: RunConfig, zone_ids: list[int], u: int) -> int:
    weights = config.resolved["demand.zone_weights"]
    return _weighted_choice(
        [(zone_id, _to_micros(weights[zone_id - 1])) for zone_id in zone_ids], u
    )


def _pick_existing_customer(conn: sqlite3.Connection, u: int) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT c.id, c.segment, r.propensity_micros FROM _customers c "
        "JOIN _customer_repeat_state r ON r.customer_id = c.id "
        "WHERE c.status = 'active' AND r.propensity_micros > 0 "
        "AND EXISTS (SELECT 1 FROM _locations l WHERE l.customer_id = c.id AND l.active = 1) "
        "ORDER BY c.id"
    ).fetchall()
    if not rows:
        return None
    return _weighted_choice([(row, int(row["propensity_micros"])) for row in rows], u)


@dataclass(frozen=True)
class _DayContext:

    service_date: str
    zone_ids: list[int]
    catalog_rows: list[sqlite3.Row]
    install_category: tuple[int, str]
    m_weather_by_name_per_zone: dict[int, dict[str, int]]
    phone_prefixes: list[str]


def _slot_kind(slot_index: int, invalid: int, install: int) -> str:
    if slot_index < invalid:
        return _INVALID
    if slot_index < invalid + install:
        return _INSTALL
    return _SERVICE


def _slot_category(
    config: RunConfig, day: _DayContext, kind: str, segment: str, zone_id: int, u: int
) -> tuple[int, str]:
    if kind == _INSTALL:
        return day.install_category
    if kind == _INVALID:
        names = frozenset(config.get_string_list("demand.unsupported_categories"))
        return _weighted_choice(_category_items(config, day.catalog_rows, segment, names, None), u)
    return _weighted_choice(
        _category_items(
            config, day.catalog_rows, segment, _SERVICE_CATEGORIES,
            day.m_weather_by_name_per_zone[zone_id],
        ),
        u,
    )


def _slot_urgent(
    config: RunConfig, day: _DayContext, kind: str, zone_id: int, category_name: str,
    hour: int, u: int,
) -> bool:
    if kind == _INSTALL:
        return False
    m_category_micros = day.m_weather_by_name_per_zone[zone_id].get(category_name, _to_micros(1))
    return chance(u, probability_threshold(_urgency_probability(config, hour, m_category_micros)))


def _existing_customer_slot(
    conn: sqlite3.Connection,
    config: RunConfig,
    day: _DayContext,
    customer: sqlite3.Row,
    *,
    slot_index: int,
    draw: Callable[[int], int],
) -> dict[str, Any]:
    customer_id = int(customer["id"])
    segment = customer["segment"]
    zone_of = _zone_of_customer(conn, customer_id)
    category_id, category_name = _slot_category(config, day, _SERVICE, segment, zone_of, draw(11))
    want_new_location = chance(
        draw(12), config.get_prob_threshold("demand.existing_customer_new_location_probability")
    )
    u13 = draw(13)
    address_catalog_id = (
        _pick_available_address(conn, _new_location_pool(zone_of, segment), u13)
        if want_new_location
        else None
    )
    location_id: int | None = None
    if address_catalog_id is None:
        existing_location_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM _locations WHERE customer_id = ? AND active = 1 ORDER BY id",
                (customer_id,),
            ).fetchall()
        ]
        location_id = existing_location_ids[u13 % len(existing_location_ids)]
    hour = _pick_arrival_hour(config, draw(8))
    arrival_at = clock.phoenix_instant(*_ymd(day.service_date), hour, draw(9) % 60)
    customer_origin = origin_keys.of_customer(conn, customer_id)
    return {
        "zone_id": zone_of,
        "arrival_at": arrival_at,
        "channel": "existing_customer",
        "segment": segment,
        "requested_category_id": category_id,
        "urgent": _slot_urgent(config, day, _SERVICE, zone_of, category_name, hour, draw(5)),
        "customer_id": customer_id,
        "location_id": location_id,
        "address_catalog_id": address_catalog_id,
        "caller_name": _customer_caller_name(conn, customer_id),
        "caller_phone": _customer_caller_phone(conn, customer_id),
        "slot_index": slot_index,
        "origin_key": origin_keys.repeat_call(day.service_date, slot_index, customer_origin),
    }


def _new_caller_slot(
    conn: sqlite3.Connection,
    config: RunConfig,
    day: _DayContext,
    kind: str,
    *,
    slot_index: int,
    draw: Callable[[int], int],
) -> dict[str, Any]:
    zone_id = _pick_zone(config, day.zone_ids, draw(1))
    segment = _pick_segment(config, draw(0))
    category_id, category_name = _slot_category(config, day, kind, segment, zone_id, draw(2))
    want_outlying = chance(draw(3), config.get_prob_threshold("demand.outlying_address_share"))
    u4 = draw(4)
    address_catalog_id = _pick_available_address(
        conn, _new_caller_pool(zone_id, segment, outlying=want_outlying), u4
    )
    if address_catalog_id is None:
        address_catalog_id = _pick_available_address(
            conn, _new_caller_pool(zone_id, segment, outlying=not want_outlying), u4
        )
    if address_catalog_id is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"no usable address pool for zone {zone_id} segment {segment!r}",
        )
    hour = _pick_arrival_hour(config, draw(8))
    arrival_at = clock.phoenix_instant(*_ymd(day.service_date), hour, draw(9) % 60)
    caller_name, caller_phone = pools.compose_identity(draw(6), segment, day.phone_prefixes)
    return {
        "zone_id": zone_id,
        "arrival_at": arrival_at,
        "channel": "new_caller",
        "segment": segment,
        "requested_category_id": category_id,
        "urgent": _slot_urgent(config, day, kind, zone_id, category_name, hour, draw(5)),
        "customer_id": None,
        "location_id": None,
        "address_catalog_id": address_catalog_id,
        "caller_name": caller_name,
        "caller_phone": caller_phone,
        "slot_index": slot_index,
        "origin_key": origin_keys.new_caller(day.service_date, zone_id, slot_index),
    }


def _generate_slot(
    conn: sqlite3.Connection,
    config: RunConfig,
    rng: Rng,
    day: _DayContext,
    kind: str,
    *,
    slot_index: int,
) -> dict[str, Any]:
    scope = f"{day.service_date}:{slot_index}"

    def draw(ordinal: int) -> int:
        return rng.draw(
            KeyedStreamName.DEMAND.value, _SLOT_ENTITY, origin_keys.ENTITY_ID, scope,
            "call_slot_attributes", ordinal,
        )

    if kind == _SERVICE and chance(
        draw(10), config.get_prob_threshold("demand.existing_customer_call_share")
    ):
        customer = _pick_existing_customer(conn, draw(7))
        if customer is not None:
            return _existing_customer_slot(
                conn, config, day, customer, slot_index=slot_index, draw=draw
            )
    return _new_caller_slot(conn, config, day, kind, slot_index=slot_index, draw=draw)


def _day_context(conn: sqlite3.Connection, config: RunConfig, service_date: str) -> _DayContext:
    zone_ids = [row["id"] for row in conn.execute("SELECT id FROM _zones ORDER BY id").fetchall()]
    catalog_rows = list(
        conn.execute(
            "SELECT id, name, supported_job_type_id FROM _service_catalog "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
    )
    install_rows = [row for row in catalog_rows if row["name"] == _INSTALL_CATEGORY]
    if len(install_rows) != 1:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"expected exactly one active {_INSTALL_CATEGORY!r} catalog row",
        )
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
        m_weather_by_name_per_zone[zone_id] = _weather_multipliers_by_name(config, weather_row)
    return _DayContext(
        service_date=service_date,
        zone_ids=zone_ids,
        catalog_rows=catalog_rows,
        install_category=(int(install_rows[0]["id"]), _INSTALL_CATEGORY),
        m_weather_by_name_per_zone=m_weather_by_name_per_zone,
        phone_prefixes=config.get_string_list("customers.phone_prefix_pool"),
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

    day = _day_context(conn, config, service_date)
    level_micros = demand_level_micros(conn, config, service_date)
    target_micros = daily_call_target_micros(config, level_micros, service_date)
    total, invalid, install = daily_call_mix(
        config, rng, service_date=service_date, target_micros=target_micros
    )

    call_specs = [
        _generate_slot(
            conn, config, rng, day, _slot_kind(slot_index, invalid, install),
            slot_index=slot_index,
        )
        for slot_index in range(total)
    ]
    existing_calls = sum(1 for spec in call_specs if spec["channel"] == "existing_customer")
    call_specs.sort(key=lambda s: (s["arrival_at"], s["origin_key"]))

    events = Events(conn, rng)
    for spec in call_specs:
        cur = conn.execute(
            "INSERT INTO _pending_calls "
            "(service_date, zone_id, arrival_at, call_kind, channel, segment, "
            "requested_category_id, urgent, customer_id, location_id, address_catalog_id, "
            "caller_name, caller_phone, prospect_id, slot_index, origin_key, created_at) "
            "VALUES (?, ?, ?, 'new_job', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                service_date,
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

    return {
        "service_date": service_date,
        "demand_level_micros": level_micros,
        "total_calls": total,
        "invalid_calls": invalid,
        "install_calls": install,
        "service_calls": total - invalid - install,
        "new_callers": total - existing_calls,
        "repeat_calls": existing_calls,
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


def apply_no_show_consequence(
    conn: sqlite3.Connection, config: RunConfig, *, now_ms: int, customer_id: int
) -> None:
    multiplier = config.get_decimal("demand.no_show_repeat_propensity_multiplier")
    ceiling = config.get_decimal("customers.repeat_propensity_max")
    row = conn.execute(
        "SELECT propensity_micros FROM _customer_repeat_state WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"missing _customer_repeat_state row for customer {customer_id}",
        )
    new_micros = _clamp_micros(
        _multiply_micros(row["propensity_micros"], multiplier), Decimal(0), ceiling
    )
    conn.execute(
        "UPDATE _customer_repeat_state SET propensity_micros = ?, last_outcome = ?, "
        "updated_at = ? WHERE customer_id = ?",
        (new_micros, "no_show", now_ms, customer_id),
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
    "apply_no_show_consequence",
    "daily_call_mix",
    "daily_call_target_micros",
    "demand_generation_handler",
    "demand_level_micros",
    "generate_for_date",
    "initialize_state",
    "register_customer_repeat_state",
    "transfer_prospect_to_customer",
    "update_word_of_mouth",
]
