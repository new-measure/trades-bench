from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from csr_bench import config as cfg
from csr_bench.config import (
    DEFAULT_CONFIG_VERSION,
    KeySpec,
    RunConfig,
    assemble_registry,
    build_deferred_manifest,
    config_hash,
    load_run_config,
    probability_threshold,
    resolve_deferred_manifest,
    validate_value,
)
from csr_bench.errors import EngineError, ErrorCode

DEFAULTS_PATH = Path(__file__).resolve().parents[1] / "src" / "csr_bench" / "defaults.json"
DEFERRED_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "csr_bench" / "deferred_manifest.json"
)

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


def test_defaults_file_loads_into_immutable_runconfig():
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    assert rc.config_version == DEFAULT_CONFIG_VERSION == "1.0.0"
    assert len(rc.resolved) == 151
    assert rc.config_hash and isinstance(rc.config_hash, str) and len(rc.config_hash) == 64
    with pytest.raises(TypeError):
        rc.resolved["constants.opening_hour"] = 9  # type: ignore[index]


def test_defaults_file_matches_registry_drift_guard():
    shipped = json.loads(_defaults_file())
    reg = assemble_registry()
    regenerated = json.loads(cfg.dump_defaults(reg, DEFAULT_CONFIG_VERSION))
    assert shipped == regenerated


def test_dump_defaults_serializes_a_decimal_default_without_crashing():
    reg = assemble_registry(
        extra_entries=[("test.synthetic_decimal", "probability", Decimal("0.5"), {})]
    )
    dumped = cfg.dump_defaults(reg, DEFAULT_CONFIG_VERSION)
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
    assert grid[-1] == [1020, 1140]


def test_valid_override_succeeds_for_non_constant_key():
    rc_default = _load()
    rc2 = _load('{"keys": {"test.opt": 5}}')
    assert rc2.get_int("test.opt") == 5
    assert rc_default.get_int("test.opt") == 3


def test_domain_keys_resolve_from_shipped_defaults():
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    assert rc.get_int("finance.starting_cash") == 20_000_000
    assert rc.resolved["hiring.base_hourly_wage_by_trade"] == {
        "hvac_service": 3500,
        "hvac_install": 4000,
        "plumbing": 3200,
        "electrical": 3400,
    }


def test_cost_map_scenario_override_accepted():
    rc = _load(
        '{"keys": {"hiring.base_hourly_wage_by_trade": '
        '{"hvac_service": 4000, "hvac_install": 4500, "plumbing": 3300, "electrical": 3600}}}'
    )
    assert rc.resolved["hiring.base_hourly_wage_by_trade"] == {
        "hvac_service": 4000,
        "hvac_install": 4500,
        "plumbing": 3300,
        "electrical": 3600,
    }


def test_reject_unknown_key_in_scenario():
    _bad('{"keys": {"bogus.nope": 1}}', ErrorCode.UNKNOWN_CONFIG_KEY)


def test_reject_unknown_key_in_defaults():
    with pytest.raises(EngineError) as ei:
        load_run_config(defaults_json='{"config_version":"1.0.0","keys":{"ghost.x":1}}')
    assert ei.value.code is ErrorCode.UNKNOWN_CONFIG_KEY


def test_reject_constant_override_even_same_value():
    _bad('{"keys": {"constants.opening_hour": 6}}', ErrorCode.CONSTANT_OVERRIDE)
    _bad('{"keys": {"constants.zone_names": ["x"]}}', ErrorCode.CONSTANT_OVERRIDE)


def test_reject_override_of_recent_hire_constant():
    _bad(
        '{"keys": {"constants.recent_hire_technician_id": 2}}',
        ErrorCode.CONSTANT_OVERRIDE,
    )


def test_reject_out_of_range_probability():
    _bad('{"keys": {"test.prob": 1.5}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.prob": -0.01}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.prob": true}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_negative_cost():
    _bad('{"keys": {"test.cost_cents": -5}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_cost_map_validation_matrix():
    _bad(
        '{"keys": {"hiring.base_hourly_wage_by_trade": {"hvac_service": -1}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    _bad(
        '{"keys": {"hiring.base_hourly_wage_by_trade": {"hvac_service": 3500.5}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    _bad(
        '{"keys": {"hiring.base_hourly_wage_by_trade": {"hvac_service": true}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    _bad(
        '{"keys": {"hiring.base_hourly_wage_by_trade": {"": 3500}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    reg = assemble_registry()
    with pytest.raises(EngineError) as ei:
        cfg.validate_value(reg["hiring.base_hourly_wage_by_trade"], {1: 3500})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_reject_min_gt_max_range():
    _bad('{"keys": {"test.range": {"min": 10, "max": 1}}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_gapped_block_grid():
    _bad('{"keys": {"test.grid": [[420,540],[660,780]]}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_cancellation_cutoff_below_15():
    _bad('{"keys": {"test.cutoff": 10}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_non_monotone_acceptance_curve():
    _bad(
        '{"keys": {"test.acceptance": {"standard": [0.1, 0.9, 0.5]}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_partially_defined_acceptance_curve():
    _bad('{"keys": {"test.acceptance": {"standard": []}}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_bool_type_accepts_bool_and_rejects_int_int_type_rejects_bool():
    _bad('{"keys": {"test.opt": true}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.flag": 1}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"test.flag": 0}}', ErrorCode.CONFIG_VALUE_INVALID)
    rc = _load('{"keys": {"test.flag": false}}')
    assert rc.get_bool("test.flag") is False
    assert _load().get_bool("test.flag") is True


def test_reject_bad_date_string():
    from csr_bench.config import KeySpec, validate_value

    with pytest.raises(EngineError) as ei:
        validate_value(KeySpec("x", "date_string", ""), "2026-13-99")
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_reject_non_canonical_date_string():
    from csr_bench.config import KeySpec, validate_value

    with pytest.raises(EngineError) as ei:
        validate_value(KeySpec("x", "date_string", ""), "20261001")
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_run_window_defaults_to_two_weeks():
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    assert rc.get_string("run.start_date") == "2026-10-01"
    assert rc.get_string("run.end_date") == "2026-10-14"
    reg = assemble_registry()
    assert reg["run.start_date"].constant is False
    assert reg["run.end_date"].constant is False


def test_run_window_is_scenario_settable():
    rc = _load(
        '{"keys": {"run.start_date": "2026-11-02", "run.end_date": "2026-11-08", '
        '"weather.precompute_end_date": "2026-11-15"}}'
    )
    assert rc.get_string("run.start_date") == "2026-11-02"
    assert rc.get_string("run.end_date") == "2026-11-08"


def test_run_window_allows_single_day_run():
    rc = _load('{"keys": {"run.start_date": "2026-10-01", "run.end_date": "2026-10-01"}}')
    assert rc.get_string("run.end_date") == "2026-10-01"


def test_reject_run_window_end_before_start():
    _bad(
        '{"keys": {"run.start_date": "2026-10-14", "run.end_date": "2026-10-01"}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_run_start_not_after_warm_start_end():
    _bad(
        '{"keys": {"run.start_date": "2026-09-30", "run.end_date": "2026-10-14"}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    _bad(
        '{"keys": {"run.start_date": "2026-06-15", "run.end_date": "2026-10-14"}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


_VALID_BANDS = [
    ["night", 0, 360],
    ["morning_peak", 360, 540],
    ["midday", 540, 900],
    ["evening_peak", 900, 1080],
    ["evening", 1080, 1260],
    ["night_late", 1260, 1440],
]

_VALID_SPEED = {
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
}

_VALID_WEATHER = {
    "clear": 1.0,
    "cool": 1.0,
    "hot": 1.02,
    "windy": 1.05,
    "extreme_heat": 1.08,
    "rain": 1.15,
    "storm": 1.4,
}


def test_geography_and_travel_defaults_resolve():
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    assert rc.get_string("geography.artifact_id") == "arizona-v1"
    assert (
        rc.get_string("geography.artifact_sha256")
        == "5e526973b9a2b58641102bba760daa0b8b8b06701bc9264106e5c156cc0b92ec"
    )
    assert rc.get_int("geography.min_address_catalog_size") == 30_000
    assert rc.resolved["travel.time_of_day_bands"][0] == ("night", 0, 360)
    assert rc.resolved["travel.speed_by_time_band"]["weekday"]["night"] == Decimal("62")
    from csr_bench.enums import ConditionCode

    assert set(rc.resolved["travel.weather_travel_multipliers"]) == {c.value for c in ConditionCode}
    assert rc.get_int("appointments.min_actual_duration_minutes") == 30


def test_reject_gps_ping_interval_zero():
    _bad(
        '{"keys": {"geography.gps_ping_interval_minutes": 0}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_time_of_day_bands_gap():
    bands = [b[:] for b in _VALID_BANDS]
    bands[0] = ["night", 0, 300]
    _bad(
        json.dumps({"keys": {"travel.time_of_day_bands": bands}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_time_of_day_bands_overlap():
    bands = [b[:] for b in _VALID_BANDS]
    bands[1] = ["morning_peak", 300, 540]
    _bad(
        json.dumps({"keys": {"travel.time_of_day_bands": bands}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_time_of_day_bands_gap_at_midnight():
    bands = [b[:] for b in _VALID_BANDS]
    bands[-1] = ["night_late", 1260, 1439]
    _bad(
        json.dumps({"keys": {"travel.time_of_day_bands": bands}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_speed_table_missing_band_key():
    speed = json.loads(json.dumps(_VALID_SPEED))
    del speed["weekday"]["night_late"]
    _bad(
        json.dumps({"keys": {"travel.speed_by_time_band": speed}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_speed_table_extra_band_key():
    speed = json.loads(json.dumps(_VALID_SPEED))
    speed["weekend"]["extra_band"] = 40
    _bad(
        json.dumps({"keys": {"travel.speed_by_time_band": speed}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_speed_table_non_positive_speed():
    speed = json.loads(json.dumps(_VALID_SPEED))
    speed["weekday"]["midday"] = 0
    _bad(
        json.dumps({"keys": {"travel.speed_by_time_band": speed}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    speed["weekday"]["midday"] = -5
    _bad(
        json.dumps({"keys": {"travel.speed_by_time_band": speed}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_speed_table_wrong_day_types():
    speed = {"weekday": _VALID_SPEED["weekday"]}
    _bad(
        json.dumps({"keys": {"travel.speed_by_time_band": speed}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_weather_multiplier_missing_condition_code_member():
    weather = dict(_VALID_WEATHER)
    del weather["storm"]
    _bad(
        json.dumps({"keys": {"travel.weather_travel_multipliers": weather}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_weather_multiplier_extra_key():
    weather = dict(_VALID_WEATHER)
    weather["blizzard"] = 2.0
    _bad(
        json.dumps({"keys": {"travel.weather_travel_multipliers": weather}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_weather_multiplier_non_positive():
    weather = dict(_VALID_WEATHER)
    weather["storm"] = 0
    _bad(
        json.dumps({"keys": {"travel.weather_travel_multipliers": weather}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_noise_min_multiplier_non_positive():
    _bad('{"keys": {"travel.noise.min_multiplier": 0}}', ErrorCode.CONFIG_VALUE_INVALID)
    _bad('{"keys": {"travel.noise.min_multiplier": -0.1}}', ErrorCode.CONFIG_VALUE_INVALID)


def test_reject_noise_min_greater_than_max():
    _bad(
        '{"keys": {"travel.noise.min_multiplier": 2.0, "travel.noise.max_multiplier": 1.5}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    rc = _load('{"keys": {"travel.noise.min_multiplier": 1.5}}')
    assert rc.get_decimal("travel.noise.min_multiplier") == Decimal("1.5")


def test_geography_artifact_fields_are_pinned_and_not_deferred():
    reg = assemble_registry()
    assert reg["geography.artifact_id"].default == "arizona-v1"
    assert (
        reg["geography.artifact_sha256"].default
        == "5e526973b9a2b58641102bba760daa0b8b8b06701bc9264106e5c156cc0b92ec"
    )
    assert reg["geography.artifact_id"].deferred is None
    assert reg["geography.artifact_sha256"].deferred is None


def test_time_of_day_bands_reject_duplicate_name():
    bands = [b[:] for b in _VALID_BANDS]
    bands[1][0] = "night"
    _bad(
        json.dumps({"keys": {"travel.time_of_day_bands": bands}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_time_of_day_bands_reject_non_list_triple():
    _bad(
        '{"keys": {"travel.time_of_day_bands": [["night", 0, 1440, "extra"]]}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


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
    assert b'"p.prob":"0.5"' in rc.canonical_bytes or b'"p.prob":0.5' in rc.canonical_bytes


def test_probability_threshold_integer_floor():
    assert probability_threshold(Decimal("0")) == 0
    assert probability_threshold(Decimal("0.5")) == 2**63
    assert probability_threshold(Decimal("1")) == 2**64
    assert probability_threshold(Decimal("0.25")) == 2**62


def test_get_prob_threshold_from_config():
    rc = _load()
    assert rc.get_prob_threshold("test.prob") == 2**63
    rc2 = _load('{"keys": {"test.prob": 0.25}}')
    assert rc2.get_prob_threshold("test.prob") == 2**62


def test_importing_config_does_not_mutate_the_process_decimal_context():
    from decimal import DefaultContext, getcontext

    assert cfg is not None
    assert getcontext().prec == DefaultContext.prec


def test_uniform01_output_identical_before_and_after_importing_config():
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
    from decimal import getcontext

    from csr_bench.rng import uniform01

    expected = uniform01(123456789)
    ctx = getcontext()
    saved_prec = ctx.prec
    try:
        ctx.prec = 5
        assert uniform01(123456789) == expected
    finally:
        ctx.prec = saved_prec


def test_manifest_json_is_machine_readable_well_formed():
    data = json.loads(build_deferred_manifest())
    assert "items" in data and data["items"]
    for item in data["items"]:
        assert "name" in item and isinstance(item["name"], str)
        assert "key_paths" in item and isinstance(item["key_paths"], list)
        assert item["status"] in {"provisional", "calibrated"}


def test_deferred_manifest_file_matches_build_deferred_manifest_drift_guard():
    shipped = DEFERRED_MANIFEST_PATH.read_bytes()
    assert shipped == build_deferred_manifest()


def test_resolve_manifest_loads_from_the_shipped_manifest_json_bytes():
    manifest_bytes = DEFERRED_MANIFEST_PATH.read_bytes()
    rep = resolve_deferred_manifest(manifest_json=manifest_bytes, strict=False)
    assert rep.unresolved
    assert rep.all_resolved() is False
    expected_names = {item["name"] for item in json.loads(manifest_bytes)["items"]}
    assert set(rep.resolved) | set(rep.unresolved) == expected_names


def test_resolve_manifest_lenient_does_not_raise_on_unresolved_domain_keys():
    rep = resolve_deferred_manifest(strict=False)
    assert rep.unresolved
    assert rep.all_resolved() is False


def test_resolve_manifest_strict_raises_when_keys_unresolved():
    with pytest.raises(EngineError) as ei:
        resolve_deferred_manifest(strict=True)
    assert ei.value.code is ErrorCode.DEFERRED_UNRESOLVED


def test_resolve_manifest_strict_raises_on_provisional_registered_keys():
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


def test_decimal_list_validator_accepts_and_rejects():
    spec = KeySpec("x", "decimal_list", [], min=0, length=3)
    assert validate_value(spec, [1, 2, 3]) == [Decimal("1"), Decimal("2"), Decimal("3")]
    with pytest.raises(EngineError) as ei:
        validate_value(spec, [1, 2])
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID
    with pytest.raises(EngineError) as ei:
        validate_value(spec, [1, -1, 2])
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_probability_list_validator_accepts_and_rejects():
    spec = KeySpec("x", "probability_list", [], length=2)
    assert validate_value(spec, [0, Decimal("0.5")]) == [Decimal("0"), Decimal("0.5")]
    with pytest.raises(EngineError) as ei:
        validate_value(spec, [0, Decimal("1.5")])
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID
    with pytest.raises(EngineError) as ei:
        validate_value(spec, [0])
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_decimal_map_validator_accepts_and_rejects():
    spec = KeySpec("x", "decimal_map", {}, min=0)
    assert validate_value(spec, {"a": 1, "b": 2}) == {"a": Decimal("1"), "b": Decimal("2")}
    with pytest.raises(EngineError) as ei:
        validate_value(spec, {"a": -1})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID
    with pytest.raises(EngineError) as ei:
        validate_value(spec, {})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_probability_map_validator_accepts_and_rejects():
    spec = KeySpec("x", "probability_map", {})
    assert validate_value(spec, {"a": Decimal("0.5")}) == {"a": Decimal("0.5")}
    with pytest.raises(EngineError) as ei:
        validate_value(spec, {"a": Decimal("1.5")})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_decimal_map_map_validator_accepts_and_rejects():
    spec = KeySpec("x", "decimal_map_map", {})
    assert validate_value(spec, {"a": {"x": 1}}) == {"a": {"x": Decimal("1")}}
    with pytest.raises(EngineError) as ei:
        validate_value(spec, {"a": {}})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID
    with pytest.raises(EngineError) as ei:
        validate_value(spec, {"a": 1})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_month_day_string_validator_accepts_and_rejects():
    spec = KeySpec("x", "month_day_string", "")
    assert validate_value(spec, "06-15") == "06-15"
    with pytest.raises(EngineError) as ei:
        validate_value(spec, "13-01")
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID
    with pytest.raises(EngineError) as ei:
        validate_value(spec, "6-15")
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_decimal_range_validator_accepts_and_rejects():
    spec = KeySpec("x", "decimal_range", {})
    assert validate_value(spec, {"min": Decimal("0.2"), "max": Decimal("1.0")}) == (
        Decimal("0.2"),
        Decimal("1.0"),
    )
    with pytest.raises(EngineError) as ei:
        validate_value(spec, {"min": Decimal("1.0"), "max": Decimal("0.2")})
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_severity_bands_validator_accepts_and_rejects():
    spec = KeySpec("x", "severity_bands", [])
    good = [["light", Decimal("0.2"), Decimal("0.5")], ["severe", Decimal("0.5"), Decimal("1.0")]]
    assert validate_value(spec, good) == [
        ("light", Decimal("0.2"), Decimal("0.5")),
        ("severe", Decimal("0.5"), Decimal("1.0")),
    ]
    gapped = [["light", Decimal("0.2"), Decimal("0.4")], ["severe", Decimal("0.5"), Decimal("1.0")]]
    with pytest.raises(EngineError) as ei:
        validate_value(spec, gapped)
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID
    with pytest.raises(EngineError) as ei:
        validate_value(spec, [["bad", Decimal("0.5"), Decimal("0.2")]])
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_reject_after_hours_urgent_below_office_hours():
    _bad(
        '{"keys": {"demand.after_hours_urgent_probability": 0.05}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_repeat_propensity_floor_not_positive():
    _bad(
        '{"keys": {"customers.repeat_propensity_floor": 0}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_repeat_propensity_floor_below_one_micro():
    _bad(
        '{"keys": {"customers.repeat_propensity_floor": 0.0000001}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_word_of_mouth_negative_weight_below_positive():
    _bad(
        '{"keys": {"demand.word_of_mouth_negative_weight": 0.5}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_outlying_address_share_at_boundary():
    _bad(
        '{"keys": {"demand.outlying_address_share": 0}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_forecast_error_scale_not_nondecreasing():
    _bad(
        '{"keys": {"weather.forecast_error_scale_by_horizon": [0, 5, 3, 4, 5, 6, 7, 8]}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_storm_severity_bands_not_partitioning_range():
    _bad(
        '{"keys": {"weather.storm_severity_bands": '
        '[["light", 0.2, 0.5], ["moderate", 0.5, 0.8], ["severe", 0.8, 0.9]]}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_acceptance_floor_above_base():
    _bad(
        '{"keys": {"acceptance.floor_probability_by_urgency": '
        '{"standard": 0.99, "urgent": 0.05}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_served_well_repeat_multiplier_at_boundary():
    _bad(
        '{"keys": {"competitors.served_well_repeat_multiplier": 1}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_segment_weights_missing_a_segment():
    _bad(
        '{"keys": {"demand.segment_weights": {"residential": 0.5, "commercial": 0.5}}}',
        ErrorCode.CONFIG_VALUE_INVALID,
    )


_VALID_COMPLEXITY_DISTRIBUTION = {
    "hvac_service": {"min": 0.30, "max": 0.70},
    "hvac_installation": {"min": 0.50, "max": 0.90},
    "plumbing": {"min": 0.30, "max": 0.70},
    "electrical": {"min": 0.30, "max": 0.70},
    "other": {"min": 0.20, "max": 0.60},
}


def test_reject_complexity_distribution_missing_job_type():
    dist = json.loads(json.dumps(_VALID_COMPLEXITY_DISTRIBUTION))
    del dist["other"]
    _bad(
        json.dumps({"keys": {"quality.complexity_distribution": dist}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_complexity_distribution_extra_job_type():
    dist = json.loads(json.dumps(_VALID_COMPLEXITY_DISTRIBUTION))
    dist["hvac_extra"] = {"min": 0.1, "max": 0.2}
    _bad(
        json.dumps({"keys": {"quality.complexity_distribution": dist}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_complexity_distribution_wrong_inner_keys():
    dist = json.loads(json.dumps(_VALID_COMPLEXITY_DISTRIBUTION))
    dist["other"] = {"min": 0.2, "max": 0.6, "mode": 0.4}
    _bad(
        json.dumps({"keys": {"quality.complexity_distribution": dist}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    dist["other"] = {"min": 0.2}
    _bad(
        json.dumps({"keys": {"quality.complexity_distribution": dist}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_complexity_distribution_min_greater_than_max_or_out_of_range():
    dist = json.loads(json.dumps(_VALID_COMPLEXITY_DISTRIBUTION))
    dist["other"] = {"min": 0.7, "max": 0.6}
    _bad(
        json.dumps({"keys": {"quality.complexity_distribution": dist}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )
    dist["other"] = {"min": 0.2, "max": 1.5}
    _bad(
        json.dumps({"keys": {"quality.complexity_distribution": dist}}),
        ErrorCode.CONFIG_VALUE_INVALID,
    )


def test_reject_scenario_override_of_weather_seasonal_period_days():
    _bad('{"keys": {"weather.seasonal_period_days": 365}}', ErrorCode.CONSTANT_OVERRIDE)


_DEMAND_MODEL_KEY_PREFIXES = (
    "acceptance.",
    "cancellations.",
    "competitors.",
    "customers.",
    "demand.",
    "weather.",
)


def test_all_demand_model_keys_resolve_from_shipped_defaults():
    reg = assemble_registry()
    rc = load_run_config(defaults_json=_defaults_file(), scenario_json="{}")
    owned = [path for path in reg if path.startswith(_DEMAND_MODEL_KEY_PREFIXES)]
    assert owned
    for path in owned:
        assert path in rc.resolved
