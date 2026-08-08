"""Tests for configuration loading, validation, canonical hashing (CAL-02), and
the deferred-value manifest machinery (CAL-01/04) — acceptance tests #5 and #6.

Synthetic test-only keys are registered through ``assemble_registry([...])`` to
exercise every rejection path (decision 3)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from csr_bench import config as cfg
from csr_bench.config import (
    DEFAULT_CONFIG_VERSION,
    RunConfig,
    assemble_registry,
    build_deferred_manifest,
    config_hash,
    load_run_config,
    probability_threshold,
    resolve_deferred_manifest,
)
from csr_bench.errors import EngineError, ErrorCode

DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "src" / "csr_bench" / "defaults.json"
DEFERRED_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "csr_bench" / "deferred_manifest.json"
)

# Synthetic, *non-constant* test keys covering each validation path. The price
# of overriding these is allowed; their *bad* values are rejected.
SYNTH = [
    ("test.prob", "probability", Decimal("0.5"), {}),
    ("test.cost_cents", "cost", 100, {}),
    ("test.range", "int_range", {"min": 1, "max": 10}, {}),
    ("test.grid", "block_grid", [[420, 540], [540, 660], [660, 780]], {}),
    ("test.cutoff", "int", 30, {"min": 15}),
    (
        "test.acceptance",
        "acceptance_curve",
        {"standard": [Decimal("0.9"), Decimal("0.5"), Decimal("0.1")]},
        {},
    ),
    ("test.opt", "int", 3, {"min": 0}),
    ("test.flag", "bool", True, {}),
]


def _reg() -> dict:
    return assemble_registry(extra_entries=SYNTH)


def _defaults_file() -> str:
    return DEFAULTS_PATH.read_text()


def _load(scenario: str | None = None, *, reg: dict | None = None) -> RunConfig:
    return load_run_config(scenario_json=scenario, registry=reg or _reg())


def _bad(scenario: str, code: ErrorCode, *, reg: dict | None = None) -> None:
    with pytest.raises(EngineError) as ei:
        _load(scenario, reg=reg)
    assert ei.value.code is code, f"{ei.value.code} != {code}: {ei.value.message}"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_defaults_file_loads_into_immutable_runconfig():
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    assert rc.config_version == DEFAULT_CONFIG_VERSION == "1.0.0"
    assert len(rc.resolved) == 24
    assert rc.config_hash and isinstance(rc.config_hash, str) and len(rc.config_hash) == 64
    # immutability
    with pytest.raises(TypeError):
        rc.resolved["constants.opening_hour"] = 9  # type: ignore[index]


def test_defaults_file_matches_registry_drift_guard():
    shipped = json.loads(_defaults_file())
    reg = assemble_registry()
    regenerated = json.loads(cfg.dump_defaults(reg, DEFAULT_CONFIG_VERSION))
    assert shipped == regenerated


def test_dump_defaults_serializes_a_decimal_default_without_crashing():
    # A prior bug fed defaults straight to json.dumps, which raises TypeError
    # on a Decimal — dormant only because no CSR-001 constant is Decimal-typed
    # yet. A future ticket registering a Decimal-default key (a probability,
    # say) must not break dump_defaults or the drift-guard test above.
    reg = assemble_registry(
        extra_entries=[("test.synthetic_decimal", "probability", Decimal("0.5"), {})]
    )
    dumped = cfg.dump_defaults(reg, DEFAULT_CONFIG_VERSION)
    # Must round-trip to an actual Decimal (not a string) through the same
    # parse_float=Decimal convention every other config layer in this module
    # is read with — a quoted "0.5" would fail _as_decimal when this file is
    # loaded for real.
    parsed = json.loads(dumped, parse_float=Decimal)
    assert parsed["keys"]["test.synthetic_decimal"] == Decimal("0.5")
    rc = load_run_config(defaults_json=dumped, registry=reg)
    assert rc.get_decimal("test.synthetic_decimal") == Decimal("0.5")


def test_constant_values_resolve_correctly():
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    assert rc.get_int("constants.opening_hour") == 6
    assert rc.get_int("constants.opening_minute") == 45
    assert rc.get_string_list("constants.zone_names")[0] == "Central Phoenix"
    assert rc.get_int("constants.zone_count") == 10
    grid = rc.resolved["constants.block_grid"]
    assert grid[-1] == [1020, 1140]  # after-hours 17:00–19:00


def test_valid_override_succeeds_for_non_constant_key():
    rc_default = _load()  # default
    rc2 = _load('{"keys": {"test.opt": 5}}')
    assert rc2.get_int("test.opt") == 5
    assert rc_default.get_int("test.opt") == 3  # default unchanged


# ---------------------------------------------------------------------------
# Acceptance #5 — config validation matrix (each rejection path, pre-init)
# ---------------------------------------------------------------------------


def test_reject_unknown_key_in_scenario():
    _bad('{"keys": {"bogus.nope": 1}}', ErrorCode.UNKNOWN_CONFIG_KEY)


def test_reject_unknown_key_in_defaults():
    with pytest.raises(EngineError) as ei:
        load_run_config(defaults_json='{"config_version":"1.0.0","keys":{"ghost.x":1}}')
    assert ei.value.code is ErrorCode.UNKNOWN_CONFIG_KEY


def test_reject_constant_override_even_same_value():
    _bad('{"keys": {"constants.opening_hour": 6}}', ErrorCode.CONSTANT_OVERRIDE)
    _bad('{"keys": {"constants.zone_names": ["x"]}}', ErrorCode.CONSTANT_OVERRIDE)


def test_reject_out_of_range_probability():
    _bad('{"keys": {"test.prob": 1.5}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.prob": -0.01}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.prob": true}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_negative_cost():
    _bad('{"keys": {"test.cost_cents": -5}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_min_gt_max_range():
    _bad('{"keys": {"test.range": {"min": 10, "max": 1}}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_gapped_block_grid():
    # missing the 540–660 block -> gap -> rejected
    _bad('{"keys": {"test.grid": [[420,540],[660,780]]}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_cancellation_cutoff_below_15():
    # min=15 synthetic int maps to the cancellation_cutoff_minutes ≥ 15 rule
    _bad('{"keys": {"test.cutoff": 10}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_non_monotone_acceptance_curve():
    _bad(
        '{"keys": {"test.acceptance": {"standard": [0.1, 0.9, 0.5]}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_partially_defined_acceptance_curve():
    _bad('{"keys": {"test.acceptance": {"standard": []}}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_bool_type_accepts_bool_and_rejects_int_int_type_rejects_bool():
    # int-typed key: a bool value is rejected (bool is not a number — _v_int).
    _bad('{"keys": {"test.opt": true}}', ErrorCode.CONFIG_VALUE_INVALID)
    # bool-typed key: an int value is rejected (bool != int — _v_bool), a real
    # bool is accepted and overrides the default.
    _bad('{"keys": {"test.flag": 1}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.flag": 0}}', ErrorCode.CONFIG_VALUE_INVALID)
    rc = _load('{"keys": {"test.flag": false}}')
    assert rc.get_bool("test.flag") is False
    assert _load().get_bool("test.flag") is True  # default unchanged


def test_reject_bad_date_string():
    from csr_bench.config import KeySpec, validate_value

    with pytest.raises(EngineError) as ei:
        validate_value(KeySpec("x", "date_string", "", "t"), "2026-13-99")
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


# ---------------------------------------------------------------------------
# CAL-02 — config hash stable across key ordering + deterministic
# ---------------------------------------------------------------------------


def test_config_hash_stable_across_scenario_key_order():
    a = _load('{"keys": {"test.opt": 2, "test.cutoff": 20}}')
    b = _load('{"keys": {"test.cutoff": 20, "test.opt": 2}}')
    assert a.config_hash == b.config_hash
    assert a.canonical_bytes == b.canonical_bytes


def test_config_hash_changes_only_when_values_change():
    a = _load('{"keys": {"test.opt": 2}}')
    b = _load('{"keys": {"test.opt": 3}}')
    assert a.config_hash != b.config_hash


def test_config_hash_is_deterministic_across_processes_no_wallclock():
    a = _load()
    b = _load()
    assert a.config_hash == b.config_hash


def test_canonical_decimals_normalized_no_float():
    reg = assemble_registry(extra_entries=[("p.prob", "probability", Decimal("0.50"), {})])
    rc = load_run_config(scenario_json='{"keys": {"p.prob": 0.5}}', registry=reg)
    # 0.5 and 0.50 normalize to the same canonical token.
    assert b'"p.prob":"0.5"' in rc.canonical_bytes or b'"p.prob":0.5' in rc.canonical_bytes


# ---------------------------------------------------------------------------
# Probability threshold (decision 1) — pure integer
# ---------------------------------------------------------------------------


def test_probability_threshold_integer_floor():
    assert probability_threshold(Decimal("0")) == 0
    assert probability_threshold(Decimal("0.5")) == 2**63
    assert probability_threshold(Decimal("1")) == 2**64
    # exactness: 0.25 -> 2^62
    assert probability_threshold(Decimal("0.25")) == 2**62


def test_get_prob_threshold_from_config():
    rc = _load()
    assert rc.get_prob_threshold("test.prob") == 2**63  # default 0.5
    rc2 = _load('{"keys": {"test.prob": 0.25}}')
    assert rc2.get_prob_threshold("test.prob") == 2**62


def test_importing_config_does_not_mutate_the_process_decimal_context():
    # A prior bug set getcontext().prec = 60 at import time — a process-wide
    # Decimal-context mutation that made csr_bench.rng.uniform01's output
    # depend on whether csr_bench.config had been imported yet (an
    # import-order-dependent result for a supposedly pure function; a replay-
    # determinism threat). probability_threshold now scopes its precision via
    # decimal.localcontext, so importing this module must leave the thread's
    # default Decimal context precision untouched.
    from decimal import DefaultContext, getcontext

    assert cfg is not None  # csr_bench.config is already imported in this process
    assert getcontext().prec == DefaultContext.prec


def test_uniform01_output_identical_before_and_after_importing_config():
    # Direct proof of the fix, run in fresh subprocesses so neither one has
    # csr_bench.config already cached: one never imports it, one imports it
    # before drawing. Both must compute the identical uniform01 value for a
    # fixed input.
    import subprocess
    import sys

    without_import = (
        "from csr_bench.rng import uniform01; import sys; "
        "sys.stdout.write(str(uniform01(123456789)))"
    )
    with_import = (
        "import csr_bench.config; "
        "from csr_bench.rng import uniform01; import sys; "
        "sys.stdout.write(str(uniform01(123456789)))"
    )
    proc_a = subprocess.run([sys.executable, "-c", without_import], capture_output=True, text=True)
    assert proc_a.returncode == 0, f"child failed: {proc_a.stderr}"
    proc_b = subprocess.run([sys.executable, "-c", with_import], capture_output=True, text=True)
    assert proc_b.returncode == 0, f"child failed: {proc_b.stderr}"
    assert proc_a.stdout == proc_b.stdout


def test_uniform01_is_independent_of_the_ambient_decimal_context():
    # Import order is not the only way to change the ambient Decimal context —
    # any embedder, plugin, or future module can mutate getcontext().prec
    # directly. uniform01 must be a pure function of u alone (decision 1): it
    # now pins its own precision via localcontext rather than inheriting
    # whatever the caller's context happens to be.
    from decimal import getcontext

    from csr_bench.rng import uniform01

    expected = uniform01(123456789)
    ctx = getcontext()
    saved_prec = ctx.prec
    try:
        ctx.prec = 5  # deliberately too coarse for an exact u / 2**64
        assert uniform01(123456789) == expected
    finally:
        ctx.prec = saved_prec


# ---------------------------------------------------------------------------
# Acceptance #6 — deferred-value manifest machinery (lenient now)
# ---------------------------------------------------------------------------


def test_manifest_json_is_machine_readable_well_formed():
    data = json.loads(build_deferred_manifest())
    assert "items" in data and data["items"]
    for item in data["items"]:
        assert "name" in item and isinstance(item["name"], str)
        assert "key_paths" in item and isinstance(item["key_paths"], list)
        assert item["status"] in {"provisional", "calibrated"}


def test_deferred_manifest_file_matches_build_deferred_manifest_drift_guard():
    # deferred_manifest.json is never read by any code path or test today
    # (docs/CSR-001.md chunk 6 audit); mirror the defaults.json drift guard so
    # the shipped file cannot silently drift from DEFERRED_ITEMS.
    shipped = DEFERRED_MANIFEST_PATH.read_bytes()
    assert shipped == build_deferred_manifest()


def test_resolve_manifest_loads_from_the_shipped_manifest_json_bytes():
    # Exercises the json.loads(manifest_json) branch in
    # resolve_deferred_manifest (config.py) using the real shipped file, not
    # just the in-memory DEFERRED_ITEMS default it falls back to on empty
    # input (config.py: data.get("items", DEFERRED_ITEMS)) — assert against
    # the file's own item names so a manifest of "{}" could not pass this too.
    manifest_bytes = DEFERRED_MANIFEST_PATH.read_bytes()
    rep = resolve_deferred_manifest(manifest_json=manifest_bytes, strict=False)
    assert rep.unresolved  # domain keys not registered in CSR-001 -> lenient OK
    assert rep.all_resolved() is False
    expected_names = {item["name"] for item in json.loads(manifest_bytes)["items"]}
    assert set(rep.resolved) | set(rep.unresolved) == expected_names


def test_resolve_manifest_lenient_does_not_raise_on_unresolved_domain_keys():
    rep = resolve_deferred_manifest(strict=False)
    assert rep.unresolved  # domain keys not registered in CSR-001 -> lenient OK
    assert rep.all_resolved() is False
    # no exception means machinery present and lenient


def test_resolve_manifest_strict_raises_when_keys_unresolved():
    with pytest.raises(EngineError) as ei:
        resolve_deferred_manifest(strict=True)
    assert ei.value.code is ErrorCode.DEFERRED_UNRESOLVED


def test_resolve_manifest_strict_raises_on_provisional_registered_keys():
    # Simulate a domain ticket registering the demand.* / acceptance.* keys.
    reg = assemble_registry(
        extra_entries=[
            ("demand.rate", "decimal", Decimal("1.0"), {}),
            ("acceptance.standard", "decimal", Decimal("0.5"), {}),
        ]
    )
    items = [
        {"name": "demand_rates", "key_paths": ["demand.*"], "status": "provisional"},
        {"name": "acceptance_curves", "key_paths": ["acceptance.*"], "status": "provisional"},
    ]
    # keys now resolve, but status is still provisional -> release guard fires.
    with pytest.raises(EngineError) as ei:
        resolve_deferred_manifest(items=items, registry=reg, strict=True)
    assert ei.value.code is ErrorCode.DEFERRED_UNRESOLVED


def test_resolve_manifest_strict_passes_when_calibrated_and_registered():
    reg = assemble_registry(
        extra_entries=[
            ("demand.rate", "decimal", Decimal("1.0"), {"deferred": "calibrated"}),
        ]
    )
    items = [{"name": "demand_rates", "key_paths": ["demand.*"], "status": "calibrated"}]
    rep = resolve_deferred_manifest(items=items, registry=reg, strict=True)
    assert rep.all_resolved() and rep.resolved == ("demand_rates",)


def test_config_hash_helper_matches_runconfig():
    rc = _load()
    assert rc.config_hash == config_hash(rc.config_version, rc.resolved)
