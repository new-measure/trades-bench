from __future__ import annotations

import uuid

from _helpers import GEO_ARTIFACT_DIR, geo_run_config, register_conn
from trades_bench.db import get_run
from trades_bench.run_init import initialize_run, open_run_db

_METADATA_COLUMNS = (
    "model_id",
    "harness_name",
    "harness_version",
    "context_policy",
    "attempt_definition",
    "track",
    "system_prompt_hash",
)


def _init(tmp_path, **metadata):
    db_path = tmp_path / f"run-{uuid.uuid4().hex[:8]}.db"
    run_id = initialize_run(
        db_path,
        master_seed=1,
        config=geo_run_config(),
        artifact_dir=GEO_ARTIFACT_DIR,
        **metadata,
    )
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    return conn, run_id


def test_run_metadata_kwargs_are_written_to_the_run_row(tmp_path):
    conn, run_id = _init(
        tmp_path,
        model_id="gpt-5.6-luna",
        harness_name="controlled",
        harness_version="0.1.0",
        context_policy="controlled_daily_reset",
        attempt_definition="one completed model turn with no terminal action committed",
        track="controlled",
        system_prompt_hash="a" * 64,
    )
    row = get_run(conn, run_id)
    assert row["model_id"] == "gpt-5.6-luna"
    assert row["harness_name"] == "controlled"
    assert row["harness_version"] == "0.1.0"
    assert row["context_policy"] == "controlled_daily_reset"
    assert (
        row["attempt_definition"]
        == "one completed model turn with no terminal action committed"
    )
    assert row["track"] == "controlled"
    assert row["system_prompt_hash"] == "a" * 64


def test_omitting_run_metadata_leaves_every_label_column_null(tmp_path):
    conn, run_id = _init(tmp_path)
    row = get_run(conn, run_id)
    for column in _METADATA_COLUMNS:
        assert row[column] is None


def test_partial_run_metadata_writes_only_what_was_supplied(tmp_path):
    conn, run_id = _init(tmp_path, track="open_agent", harness_name="pi")
    row = get_run(conn, run_id)
    assert row["track"] == "open_agent"
    assert row["harness_name"] == "pi"
    assert row["model_id"] is None
    assert row["attempt_definition"] is None
    assert row["system_prompt_hash"] is None
