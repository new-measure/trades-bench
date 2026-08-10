"""Immutable validated configuration + canonical hash + deferred-value manifest
(spec 01 §Configuration, §Deferred-value registry; docs/CSR-001.md decisions 2–3).

Layering: a versioned scenario JSON overlays versioned defaults JSON. Both are
parsed with ``parse_float=Decimal`` so probabilities never touch floats. Unknown
keys are rejected; out-of-range probabilities, negative costs, ``min > max``
ranges, non-contiguous block grids, non-monotone acceptance curves, and any
attempt to override a user-decided constant are each rejected *before*
initialization, each with a dedicated error code.

The resolved :class:`RunConfig` is immutable. Probability keys have their
``floor(p × 2^64)`` threshold computed once at resolution and stored as a plain
int (decision 1), so the decision path is pure-integer. The canonical
sorted-JSON serialization is SHA-256-hashed to give a config_hash stable across
input key ordering (CAL-02).

The assembled section registry is the union of the "config keys owned by this
file" sections of the domain specs. The **constants** section (``constants.*``,
``constant=True``) ships from :mod:`csr_bench.constants`; non-constant domain
keys (``finance.*``, ``weather.*``, …) are registered by their owning tickets
via :data:`_DOMAIN_ENTRIES`. A config-validation matrix test may register
synthetic test-only keys to exercise every rejection path — see
:func:`assemble_registry`'s ``extra_entries``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any

from .constants import CONSTANT_ENTRIES
from .errors import EngineError, ErrorCode

#: The config version this engine ships defaults for (conventions).
DEFAULT_CONFIG_VERSION: str = "1.0.0"

#: ``2 ** 64``, used for probability thresholds (decision 1) and RNG masking.
U64_MOD: int = 1 << 64

#: Decimal precision for ``Decimal(2**64) * probability`` in :func:`probability_threshold`
#: — applied via a local context there, never the process-wide default context, so
#: importing this module cannot change another module's Decimal arithmetic
#: (a global ``getcontext().prec`` mutation made ``rng.uniform01`` import-order
#: dependent — a replay-determinism threat).
_THRESHOLD_PREC = 60

#: Sentinel character for :func:`_dump_scalar`'s placeholder tokens — a control
#: character that cannot appear in any registered default's string/path value,
#: so token substring replacement in :func:`dump_defaults` cannot collide.
_NUL = chr(0)


# ---------------------------------------------------------------------------
# Registry assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    """A declarative description of one configuration key.

    Attributes:
        path: dotted key path, e.g. ``"constants.opening_hour"``.
        type: one of the supported validator types (see ``_VALIDATORS``).
        default: the default value (canonical for the shipped defaults JSON).
        owning_spec: the owning spec filename, e.g. ``"01-run-contracts.md"``.
        constant: True for user-decided constants — scenario override rejected.
        deferred: ``None`` | ``"provisional"`` | ``"calibrated"`` status marker.
        min/max: optional integer bounds for ``int`` keys.
    """

    path: str
    type: str
    default: Any
    owning_spec: str
    constant: bool = False
    deferred: str | None = None
    min: int | None = None
    max: int | None = None
    description: str = ""


#: Non-constant domain keys registered by their owning tickets (CSR-002+).
#: Entries have the same ``(path, type, default, meta)`` shape as
#: :data:`csr_bench.constants.CONSTANT_ENTRIES`; they default to
#: ``constant=False`` and may carry any of the KeySpec meta fields.
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
]


def assemble_registry(
    *,
    extra_entries: list[Any] | None = None,
    extra_owning_spec: str = "test",
) -> dict[str, KeySpec]:
    """Build the assembled section registry from the constant + domain + extra
    providers.

    Each entry is a ``(path, type, default, meta)`` tuple (see
    :data:`csr_bench.constants.CONSTANT_ENTRIES`) or a :class:`KeySpec`.
    Constant entries are marked ``constant=True``; domain entries
    (:data:`_DOMAIN_ENTRIES`) and synthetic test-only entries passed via
    ``extra_entries`` default to ``constant=False`` unless their meta says
    otherwise.
    """
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

    # Constants own every user-decided constant; scenario overrides must fail.
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


# ---------------------------------------------------------------------------
# Validators (raise EngineError(CONFIG_VALUE_INVALID) on any bad value)
# ---------------------------------------------------------------------------


def _is_bool(x: Any) -> bool:
    return isinstance(x, bool)


def _as_int(x: Any) -> int:
    """Coerce an int / integral Decimal to a Python int; reject bool & float."""
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
        n = _as_int(v)  # rejects bool / float / non-numeric
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
    "acceptance_curve": _v_acceptance_curve,
    "string": _v_string,
    "string_list": _v_string_list,
    "bool": _v_bool,
    "date_string": _v_date_string,
}


def validate_value(spec: KeySpec, value: Any) -> Any:
    """Validate one value against its :class:`KeySpec`; return the normalized value.

    Raises :class:`EngineError` with :data:`ErrorCode.CONFIG_VALUE_INVALID` on
    any structural / range / monotonicity failure.
    """
    validator = _VALIDATORS.get(spec.type)
    if validator is None:
        raise _bad(f"{spec.path}: unknown key type {spec.type!r}")
    return validator(spec, value)


# ---------------------------------------------------------------------------
# Canonical serialization + hash
# ---------------------------------------------------------------------------


def _canon_scalar(v: Any) -> Any:
    if isinstance(v, Decimal):
        # No float on the path; exact values only. Normalize/trailing zeros.
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
        return v.path  # not expected in canonical values
    raise _bad(f"cannot canonicalize {type(v).__name__} for hashing")


def canonical_config_bytes(config_version: str, resolved: Mapping[str, Any]) -> bytes:
    """Canonical sorted-JSON of the resolved config (config_version + keys)."""
    canon = {"config_version": str(config_version), "keys": {}}
    for path in sorted(resolved):
        canon["keys"][path] = _canon_scalar(resolved[path])
    return json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")


def config_hash(config_version: str, resolved: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonical config, stable across key ordering."""
    return hashlib.sha256(canonical_config_bytes(config_version, resolved)).hexdigest()


# ---------------------------------------------------------------------------
# Probability threshold (decision 1) — computed once, plain int on the path
# ---------------------------------------------------------------------------


def probability_threshold(p: Decimal) -> int:
    """``floor(p × 2^64)`` with integer math only (decision 1).

    Comparison against a u64 draw is then ``u < threshold``, never float. The
    precision needed for ``Decimal(2**64) * probability`` is scoped to a
    ``localcontext`` here rather than mutating the process-wide default
    context, so importing this module never changes another module's Decimal
    arithmetic (e.g. :func:`csr_bench.rng.uniform01`).
    """
    if not isinstance(p, Decimal):
        p = Decimal(p)
    with localcontext() as ctx:
        ctx.prec = _THRESHOLD_PREC
        scaled = p * Decimal(U64_MOD)
        return int(scaled.to_integral_value(rounding=ROUND_FLOOR))


# ---------------------------------------------------------------------------
# RunConfig — the immutable resolved artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """The immutable resolved configuration artifact (spec 01 §Interfaces)."""

    config_version: str
    resolved: Mapping[str, Any]  # read-only mapping of path -> validated value
    _thresholds: Mapping[str, int] = field(default_factory=dict)  # type: ignore[var-annotated]
    _canonical_bytes: bytes = b""
    config_hash: str = ""

    def get(self, path: str) -> Any:
        return self.resolved[path]

    def get_int(self, path: str) -> int:
        v = self.resolved[path]
        if isinstance(v, tuple):  # int_range stored as (lo, hi)
            raise TypeError(f"{path!r} is a range, use get_range()")
        return int(v)

    def get_decimal(self, path: str) -> Decimal:
        v = self.resolved[path]
        return v if isinstance(v, Decimal) else Decimal(v)

    def get_prob_threshold(self, path: str) -> int:
        # Probability keys carry a precomputed floor(p × 2**64) threshold.
        if path in self._thresholds:
            return self._thresholds[path]
        # Fall back (e.g. a constant integral probability-like) by recomputing.
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
    """Recursive scalar transform for :func:`dump_defaults`.

    Unlike :func:`_canon_scalar` (used only for hash-only canonical bytes that
    are never re-parsed), a non-integral ``Decimal`` here must come back out of
    :func:`load_run_config`'s ``json.loads(..., parse_float=Decimal)`` as a
    ``Decimal`` — quoting it as a string would fail :func:`_as_decimal` on
    load. It is swapped for a unique placeholder token that :func:`dump_defaults`
    splices back in as a bare numeric literal after ``json.dumps``.
    """
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
    return v  # bool / int / str are already JSON-native


def dump_defaults(registry: dict[str, KeySpec], config_version: str) -> bytes:
    """Emit the versioned defaults JSON derived from the registry.

    A non-integral ``Decimal`` default (a probability, say) is emitted as a
    bare JSON numeric literal via :func:`_dump_scalar`, not the quoted string
    :func:`_canon_scalar` would produce — this file IS re-parsed by
    :func:`load_run_config`, so it must round-trip through
    ``json.loads(..., parse_float=Decimal)`` back to ``Decimal``.
    """
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
    """Parse a layer JSON, returning ``(config_version, keys)`` (or empty)."""
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
    # Reject non-string keys early (dotted paths are strings).
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
    """Resolve defaults + scenario into an immutable validated :class:`RunConfig`.

    Validation order: unknown keys, then constant-override, then per-key value
    validation — all *before* any run record is written. A scenario that merely
    mentions a ``constant=True`` key is rejected (user-decided constants are not
    scenario knobs).
    """
    reg = registry if registry is not None else assemble_registry()

    # --- base layer (defaults) ---------------------------------------------
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

    # --- scenario overlay ---------------------------------------------------
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

    # --- every registered key resolved? ------------------------------------
    missing = [p for p in reg if p not in base]
    if missing:
        raise EngineError(
            ErrorCode.CONFIG_VALUE_INVALID,
            f"missing required config keys: {sorted(missing)}",
        )

    # --- probability thresholds (decision 1) --------------------------------
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


# ---------------------------------------------------------------------------
# Deferred-value manifest (spec 01 §Deferred-value registry; decision 3)
# ---------------------------------------------------------------------------

#: Deferred numeric items from 00-decisions "Deferred to calibration or later
#: evaluation", each mapped to the registered config key path(s) it resolves to.
#: Status is provisional until the owning calibration ticket (CSR-015) lands.
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
        "key_paths": ["calibration.minimum_address_catalog_size"],
        "status": "provisional",
    },
]


def build_deferred_manifest() -> bytes:
    """Emit the machine-readable deferred-value manifest JSON."""
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
        prefix = path_or_wild[:-1]  # keep the dot
        return any(p.startswith(prefix) for p in registry)
    return path_or_wild in registry


def resolve_deferred_manifest(
    manifest_json: str | bytes | None = None,
    *,
    registry: dict[str, KeySpec] | None = None,
    items: list[dict[str, Any]] | None = None,
    strict: bool = False,
) -> ResolveReport:
    """Resolve a deferred-value manifest against the assembled registry.

    Lenient (``strict=False``, default) simply reports what resolves and what
    does not — the resolve-test starts lenient and tightens as domain tickets
    land (decision 3). Strict (``strict=True``) raises the moment any item maps
    to no registered key, or any *registered* key it maps to is still
    ``provisional`` (the release-build guard, CAL-01 / CAL-04).
    """
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
# expose `resolve` alias without duplicating the name in a way ruff flags
resolve = resolve_deferred_manifest
