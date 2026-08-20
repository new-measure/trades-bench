from __future__ import annotations

import hashlib

from _helpers import GEO_ARTIFACT_DIR
from test_har_equivalence import open_world, scripted_config
from trades_bench import SPEC_VERSION
from trades_bench.enums import VisibilityClass
from trades_bench.harness import prompts
from trades_bench.harness.scripted import ScriptedAdapter
from trades_bench.run_init import initialize_run
from trades_bench.visibility import Field, classify

SEED = 5

LABEL_FIELDS = (
    "track",
    "model_id",
    "harness_name",
    "harness_version",
    "context_policy",
    "attempt_definition",
    "system_prompt_hash",
    "spec_version",
    "config_version",
    "config_hash",
    "master_seed",
)


def _initialized_run(path, config, adapter, *, track="controlled"):
    metadata = adapter.describe()
    run_id = initialize_run(
        path,
        master_seed=SEED,
        config=config,
        artifact_dir=GEO_ARTIFACT_DIR,
        model_id=metadata.model_id,
        harness_name=metadata.name,
        harness_version=metadata.version,
        context_policy=metadata.context_policy,
        attempt_definition=metadata.attempt_definition,
        track=track,
        system_prompt_hash=prompts.system_prompt_hash(),
    )
    return run_id


def test_har07_run_record_carries_every_result_label(tmp_path):
    config = scripted_config()
    adapter = ScriptedAdapter(model_id="scripted-model")
    run_id = _initialized_run(tmp_path / "run.db", config, adapter)

    conn = open_world(tmp_path / "run.db")
    row = conn.execute("SELECT * FROM _run").fetchone()
    labels = {field: row[field] for field in LABEL_FIELDS}
    conn.close()

    assert row["run_id"] == run_id
    assert labels["track"] == "controlled"
    assert labels["model_id"] == "scripted-model"
    assert labels["harness_name"] == adapter.name
    assert labels["harness_version"] == adapter.version
    assert labels["context_policy"] == "custom:scripted"
    assert labels["attempt_definition"] == adapter.attempt_definition
    assert labels["spec_version"] == SPEC_VERSION
    assert labels["config_version"] == config.config_version
    assert labels["config_hash"] == config.config_hash
    assert labels["master_seed"] == SEED
    assert all(value is not None and value != "" for value in labels.values())


def test_har07_recorded_hash_is_the_hash_of_the_system_prompt_bytes(tmp_path):
    config = scripted_config()
    adapter = ScriptedAdapter()
    _initialized_run(tmp_path / "run.db", config, adapter)

    conn = open_world(tmp_path / "run.db")
    recorded = str(conn.execute("SELECT system_prompt_hash FROM _run").fetchone()[0])
    conn.close()

    expected = hashlib.sha256(prompts.SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert recorded == expected == prompts.system_prompt_hash()
    assert len(recorded) == 64


def test_har07_hash_changes_with_any_byte_of_the_system_prompt():
    baseline = prompts.system_prompt_hash()
    mutated = hashlib.sha256((prompts.SYSTEM_PROMPT + " ").encode("utf-8")).hexdigest()
    assert baseline != mutated


def test_har07_open_track_labels_differ_from_controlled_labels(tmp_path):
    config = scripted_config()
    controlled = ScriptedAdapter(model_id="model-a", name="controlled")
    scripted = ScriptedAdapter(model_id="model-b", name="scripted")
    _initialized_run(tmp_path / "controlled.db", config, controlled, track="controlled")
    _initialized_run(tmp_path / "open.db", config, scripted, track="open_agent")

    def labels(path):
        conn = open_world(path)
        row = conn.execute(
            "SELECT track, model_id, harness_name FROM _run"
        ).fetchone()
        conn.close()
        return tuple(row)

    assert labels(tmp_path / "controlled.db") != labels(tmp_path / "open.db")


def test_har07_labels_are_never_agent_visible():
    for field in ("track", "model_id", "harness_name", "harness_version", "context_policy",
                  "attempt_definition", "system_prompt_hash"):
        assert classify(Field("_run", field)) is VisibilityClass.POST_RUN_ONLY


def test_system_prompt_carries_no_benchmark_identifier():
    lowered = prompts.SYSTEM_PROMPT.lower()
    for forbidden in ("trades_bench", "trades-bench", "csr_bench", "bench", "benchmark"):
        assert forbidden not in lowered
    assert "Sunridge Services" in prompts.SYSTEM_PROMPT
