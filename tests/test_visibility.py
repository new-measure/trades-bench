"""Tests for the field-visibility classification registry (spec 01 §Visibility
contract, §Interfaces; acceptance #8: "classification lookup works;
unclassified field fails")."""

from __future__ import annotations

import pytest

from csr_bench.db import in_memory, init_schema
from csr_bench.enums import VisibilityClass
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.visibility import FIELD_REGISTRY, Field, assert_public, classify


@pytest.fixture
def conn():
    c = in_memory()
    init_schema(c)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# classify() lookup
# ---------------------------------------------------------------------------


def test_classify_returns_expected_class_for_representative_fields():
    # The frozen simulated time is projected by the public `company_state`
    # view (`as_of_ms`, spec 02 §4.9) and required on every response's
    # `meta.as_of` (spec 08:133), so it is public_sql, not prompt-exclusive.
    assert classify(Field("run", "current_time_ms")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_invocations", "ts_ms")) is VisibilityClass.PUBLIC_SQL
    # Attempt count has no direct spec citation yet; grouped with the other
    # `_invocations` bookkeeping columns pending CSR-012.
    assert classify(Field("_invocations", "attempt_count")) is VisibilityClass.POST_RUN_ONLY
    # Also projected by `company_state` (`run_status`, spec 02 §4.9) and
    # `finance.get_cash_position()` (spec 08:250).
    assert classify(Field("run", "status")) is VisibilityClass.PUBLIC_SQL
    # "reporting only" fields (spec 01:45) / result-labeling record (spec 08 §8.4).
    assert classify(Field("run", "model_id")) is VisibilityClass.POST_RUN_ONLY
    assert classify(Field("run", "master_seed")) is VisibilityClass.POST_RUN_ONLY
    # RNG stream state is explicitly private_engine (spec 01:282).
    assert classify(Field("_rng_state", "seed")) is VisibilityClass.PRIVATE_ENGINE
    # The idempotency cache must never leak, public or privileged alike.
    assert classify(Field("_command_responses", "response")) is VisibilityClass.PRIVATE_ENGINE


def test_classify_accepts_a_plain_tuple_like_field():
    assert classify(("run", "current_time_ms")) is classify(Field("run", "current_time_ms"))


def test_classify_unclassified_field_raises_structured_error():
    with pytest.raises(EngineError) as ei:
        classify(Field("run", "no_such_column"))
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD
    # Unlike UNAUTHORIZED this is a build-time/registry error, not a value that
    # reaches a public surface, so it is allowed to name the offending field.
    assert "run.no_such_column" in ei.value.message


def test_classify_unclassified_table_raises_same_code():
    with pytest.raises(EngineError) as ei:
        classify(Field("no_such_table", "col"))
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


# ---------------------------------------------------------------------------
# assert_public()
# ---------------------------------------------------------------------------


def test_assert_public_accepts_an_all_public_column_set():
    # Must not raise.
    assert_public("run", ["current_time_ms", "status"])
    assert_public("_invocations", ["ts_ms"])


def test_assert_public_rejects_a_registered_private_column():
    with pytest.raises(EngineError) as ei:
        assert_public("run", ["current_time_ms", "master_seed"])
    assert ei.value.code is ErrorCode.UNAUTHORIZED
    # Uniform, secret-free message — the caller cannot learn which column or
    # table failed (spec 01:281 indistinguishability rule).
    assert ei.value.message == "unauthorized"
    assert ei.value.details == {}


def test_assert_public_rejects_an_unclassified_column():
    with pytest.raises(EngineError) as ei:
        assert_public("run", ["not_a_real_column"])
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_assert_public_empty_columns_is_a_noop():
    assert_public("run", [])


# ---------------------------------------------------------------------------
# Registry completeness against the actual DDL (spec 01:283 coverage test
# shape) — a future column added to db.py without a matching registry entry
# fails this test, forcing the registry to be kept in sync.
# ---------------------------------------------------------------------------


def _ddl_fields(conn) -> set[Field]:
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    fields: set[Field] = set()
    for table in tables:
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
            fields.add(Field(table, row["name"]))
    return fields


def test_every_ddl_column_is_classified(conn):
    ddl_fields = _ddl_fields(conn)
    assert ddl_fields, "sanity: schema introspection must find at least one field"
    unclassified = ddl_fields - set(FIELD_REGISTRY)
    assert not unclassified, f"DDL columns missing from the visibility registry: {unclassified}"


def test_registry_has_no_stale_entries_beyond_the_ddl(conn):
    ddl_fields = _ddl_fields(conn)
    stale = set(FIELD_REGISTRY) - ddl_fields
    assert not stale, f"registry entries with no matching DDL column: {stale}"


def test_registry_completeness_catches_a_missing_column(conn):
    # Directly exercise the "future DDL change forces a registry update" shape
    # against a real schema change, not a dict-arithmetic restatement: add a
    # column the registry cannot know about and confirm both the completeness
    # query and classify() itself detect it.
    conn.execute("ALTER TABLE run ADD COLUMN probe_col TEXT")
    probe = Field("run", "probe_col")
    assert _ddl_fields(conn) - set(FIELD_REGISTRY) == {probe}
    with pytest.raises(EngineError) as ei:
        classify(probe)
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_every_registered_class_is_a_real_visibility_class():
    for value in FIELD_REGISTRY.values():
        assert isinstance(value, VisibilityClass)
