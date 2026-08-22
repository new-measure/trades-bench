from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trades_bench.config import (
    DEFAULT_CONFIG_VERSION,
    canonical_config_bytes,
    config_hash,
    load_run_config,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "config"
REGEN_ENV = "REGEN_GOLDEN_CONFIG"
DEFAULTS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "trades_bench" / "defaults.json"
)


def golden_path(config_version: str) -> Path:
    return GOLDEN_DIR / f"{config_version}.json"


def default_config():
    return load_run_config(defaults_json=DEFAULTS_PATH.read_text(), scenario_json="{}")


def recorded_bytes(config) -> bytes:
    canonical = canonical_config_bytes(config.config_version, config.resolved)
    document = {
        "config_version": config.config_version,
        "config_hash": config_hash(config.config_version, config.resolved),
        "key_count": len(config.resolved),
        "keys": json.loads(canonical)["keys"],
    }
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def test_golden_config_surface(tmp_path):
    config = default_config()
    recorded = recorded_bytes(config)
    path = golden_path(config.config_version)
    if os.environ.get(REGEN_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recorded)
        pytest.skip(f"{REGEN_ENV}=1 rewrote {path.name}")
    assert path.is_file(), (
        f"missing config golden {path}; rerun with {REGEN_ENV}=1 to record it deliberately"
    )
    stored = path.read_bytes()
    if stored != recorded:
        actual = tmp_path / "actual.json"
        actual.write_bytes(recorded)
        raise AssertionError(
            f"the shipped configuration surface changed for config_version "
            f"{config.config_version}; compare {path} with {actual}, bump the config "
            f"version if the change is a behavior change, and rerun with {REGEN_ENV}=1 "
            "only for a deliberate config change"
        )


def test_the_golden_config_hash_matches_the_golden_key_body():
    config = default_config()
    stored = json.loads(golden_path(config.config_version).read_bytes())
    canonical = json.dumps(
        {"config_version": stored["config_version"], "keys": stored["keys"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert canonical == canonical_config_bytes(config.config_version, config.resolved)
    assert stored["config_hash"] == config.config_hash
    assert stored["key_count"] == len(stored["keys"]) == len(config.resolved)


def test_the_golden_config_version_is_the_shipped_default():
    config = default_config()
    stored = json.loads(golden_path(config.config_version).read_bytes())

    assert config.config_version == DEFAULT_CONFIG_VERSION
    assert stored["config_version"] == DEFAULT_CONFIG_VERSION
    assert json.loads(DEFAULTS_PATH.read_text())["config_version"] == DEFAULT_CONFIG_VERSION


def test_an_added_key_moves_the_golden_config_surface():
    config = default_config()
    mutated = dict(config.resolved)
    mutated["ghost.added_key"] = 1

    assert config_hash(config.config_version, mutated) != config.config_hash


def test_a_removed_key_moves_the_golden_config_surface():
    config = default_config()
    mutated = dict(config.resolved)
    mutated.pop(sorted(mutated)[0])

    assert config_hash(config.config_version, mutated) != config.config_hash


def test_a_changed_default_value_moves_the_golden_config_surface():
    config = default_config()
    mutated = dict(config.resolved)
    mutated["finance.starting_cash"] = config.get_int("finance.starting_cash") + 1

    assert config_hash(config.config_version, mutated) != config.config_hash
