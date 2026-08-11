from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

from .constants import CONSTANT_ENTRIES
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
    owning_spec: str
    constant: bool = False
    deferred: str | None = None
    min: int | None = None
    max: int | None = None
    description: str = ""


_DOMAIN_ENTRIES: list[Any] = [
    (
        "finance.starting_cash",
        "cost",
        20_000_000,
        {
            "deferred": "provisional",
            "owning_spec": "02-data-and-initial-state.md",
            "description": "Starting cash (integer cents) the warm-start opening "
            "adjustment pins the ledger to (spec 02 §8).",
        },
    ),
    (
        "hiring.base_hourly_wage_by_trade",
        "cost_map",
        {"hvac_service": 3500, "hvac_install": 4000, "plumbing": 3200, "electrical": 3400},
        {
            "deferred": "provisional",
            "owning_spec": "04-workforce-and-labor.md",
            "description": "Base hourly wage in cents/hour per qualification code "
            "(spec 04 §14, hiring section).",
        },
    ),
    (
        "geography.artifact_id",
        "string",
        "",
        {
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Identity of the packaged geography artifact (03 §1.2, §13); "
            "pinned by CSR-003g, empty until then.",
        },
    ),
    (
        "geography.artifact_sha256",
        "string",
        "",
        {
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "SHA-256 of the packaged geography artifact, verified at run "
            "initialization (03 §1.2, §13); pinned by CSR-003g, empty until then.",
        },
    ),
    (
        "geography.min_address_catalog_size",
        "int",
        30_000,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Floor on usable catalog rows, PERF-05 (03 §3.2, §13).",
        },
    ),
    (
        "geography.address_catalog_safety_factor",
        "decimal",
        Decimal("3.0"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Multiplier over projected annual distinct new-caller "
            "addresses (03 §3.2, §13).",
        },
    ),
    (
        "geography.min_addresses_per_zone",
        "int",
        1500,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Per-zone usable-address floor (03 §3.2, §13).",
        },
    ),
    (
        "geography.min_addresses_per_zone_segment",
        "int",
        100,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Per-zone, per-segment usable-address floor (03 §3.2, §13).",
        },
    ),
    (
        "geography.min_outlying_addresses",
        "int",
        1000,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Outlying-ring usable-address floor (03 §3.2, §13).",
        },
    ),
    (
        "geography.outlying_buffer_km",
        "decimal",
        Decimal("15.0"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Crop buffer beyond the service core (03 §1.1, §13).",
        },
    ),
    (
        "geography.max_snap_distance_meters",
        "int",
        150,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Address to road-network snap tolerance (research/osm-data.md "
            "§4.8, 03 §13).",
        },
    ),
    (
        "geography.duplicate_coordinate_meters",
        "decimal",
        Decimal("10.0"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Coordinate-proximity dedupe radius (research/osm-data.md §4.4, "
            "03 §13).",
        },
    ),
    (
        "geography.min_route_distance_km",
        "decimal",
        Decimal("0.5"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Floor on any route distance (03 §4.2, §13).",
        },
    ),
    (
        "geography.intra_cell_detour_factor",
        "decimal",
        Decimal("1.3"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Same-anchor distance factor (03 §4.2, §13).",
        },
    ),
    (
        "geography.distance_model_max_median_error_pct",
        "decimal",
        Decimal("10.0"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Packaging accuracy gate versus exact routed distances, median "
            "(03 §4.2, §13; research/osm-data.md §5.3).",
        },
    ),
    (
        "geography.distance_model_max_p95_error_pct",
        "decimal",
        Decimal("25.0"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Packaging accuracy gate versus exact routed distances, p95 "
            "(03 §4.2, §13; research/osm-data.md §5.3).",
        },
    ),
    (
        "geography.gps_ping_interval_minutes",
        "int",
        5,
        {
            "min": 1,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Quantization of the public live-location row (03 §9.2, §13). "
            "min=1: this is a divisor in the §9.2 quantization, so 0 would divide by zero.",
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
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Ordered contiguous partition of [00:00, 24:00) (03 §5.1, §13).",
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
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "km/h by day_type x band (03 §5.1, §13).",
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
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Multiplier by 05's closed condition_code enum; total over every "
            "member (03 §5.1, §13; decision 9).",
        },
    ),
    (
        "travel.noise.sigma",
        "decimal",
        Decimal("0.15"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Log-normal shape, median 1 (03 §5.1, §13).",
        },
    ),
    (
        "travel.noise.min_multiplier",
        "decimal",
        Decimal("0.75"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Noise clamp floor, strictly positive (03 §5.1, §13, §631).",
        },
    ),
    (
        "travel.noise.max_multiplier",
        "decimal",
        Decimal("1.50"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Noise clamp ceiling (03 §5.1, §13, §631).",
        },
    ),
    (
        "travel.min_travel_minutes",
        "int",
        5,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Floor on any realized trip (03 §5.1, §13).",
        },
    ),
    (
        "appointments.min_actual_duration_minutes",
        "int",
        30,
        {
            "min": 0,
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Floor on realized service duration (03 §10.4, §13).",
        },
    ),
    (
        "appointments.min_duration_multiplier",
        "decimal",
        Decimal("0.25"),
        {
            "deferred": "provisional",
            "owning_spec": "03-geography-and-scheduling.md",
            "description": "Floor on the 06-owned duration multiplier (03 §10.4, §13).",
        },
    ),
]


def assemble_registry(
    *,
    extra_entries: list[Any] | None = None,
    extra_owning_spec: str = "test",
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
                owning_spec=(
                    meta.get("owning_spec", extra_owning_spec)
                    if isinstance(meta, dict)
                    else extra_owning_spec
                ),
                constant=bool(meta.get("constant", False)) if isinstance(meta, dict) else False,
                deferred=meta.get("deferred") if isinstance(meta, dict) else None,
                min=meta.get("min") if isinstance(meta, dict) else None,
                max=meta.get("max") if isinstance(meta, dict) else None,
                description=meta.get("description", "") if isinstance(meta, dict) else "",
            )
        if spec.path in registry:
            raise ValueError(
                f"duplicate config key {spec.path!r} declared by "
                f"{registry[spec.path].owning_spec} and {spec.owning_spec}"
            )
        registry[spec.path] = spec

    for entry in CONSTANT_ENTRIES:
        path, type_, default, meta = entry  # type: ignore[misc]
        _add(
            (
                path,
                type_,
                default,
                {
                    **meta,
                    "constant": True,
                    "owning_spec": meta.get("owning_spec", "01-run-contracts.md"),
                },
            )
        )

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
    return _as_decimal(value)


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
    import datetime as _dt

    try:
        _dt.date.fromisoformat(s)
    except ValueError:
        raise _bad(f"{spec.path}: {s!r} is not a valid YYYY-MM-DD date") from None
    return s


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
        raise _bad(f"travel.noise.min_multiplier: {lo} must be strictly positive (03:631)")
    if lo > hi:
        raise _bad(f"travel.noise.min_multiplier {lo} > travel.noise.max_multiplier {hi} (03:631)")


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


_CROSS_FIELD_VALIDATORS: tuple[tuple[frozenset[str], Any], ...] = (
    (
        frozenset({"travel.noise.min_multiplier", "travel.noise.max_multiplier"}),
        _validate_noise_clamp,
    ),
    (
        frozenset({"travel.time_of_day_bands", "travel.speed_by_time_band"}),
        _validate_speed_table_bands,
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
