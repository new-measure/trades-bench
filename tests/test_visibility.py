from __future__ import annotations

import pytest

from csr_bench.db import in_memory
from csr_bench.enums import VisibilityClass
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.visibility import FIELD_REGISTRY, Field, assert_public, classify


@pytest.fixture
def conn():
    c = in_memory()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def test_classify_returns_expected_class_for_representative_fields():
    assert classify(Field("_clock", "current_time_ms")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_clock", "service_date")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_invocations", "ts_ms")) is VisibilityClass.POST_RUN_ONLY
    assert classify(Field("_invocations", "attempt_count")) is VisibilityClass.POST_RUN_ONLY
    assert classify(Field("_run", "status")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_run", "model_id")) is VisibilityClass.POST_RUN_ONLY
    assert classify(Field("_run", "master_seed")) is VisibilityClass.POST_RUN_ONLY
    assert classify(Field("_rng_state", "seed")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_idempotency", "response")) is VisibilityClass.PRIVATE_ENGINE


def test_classify_accepts_a_plain_tuple_like_field():
    assert classify(("_clock", "current_time_ms")) is classify(Field("_clock", "current_time_ms"))


def test_classify_unclassified_field_raises_structured_error():
    with pytest.raises(EngineError) as ei:
        classify(Field("_run", "no_such_column"))
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD
    assert "_run.no_such_column" in ei.value.message


def test_classify_unclassified_table_raises_same_code():
    with pytest.raises(EngineError) as ei:
        classify(Field("no_such_table", "col"))
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_assert_public_accepts_an_all_public_column_set():
    assert_public("_clock", ["current_time_ms", "service_date"])
    assert_public("_run", ["status"])
    assert_public("_customers", ["name", "segment"])


def test_assert_public_rejects_a_registered_private_column():
    with pytest.raises(EngineError) as ei:
        assert_public("_run", ["status", "master_seed"])
    assert ei.value.code is ErrorCode.UNAUTHORIZED
    assert ei.value.message == "unauthorized"
    assert ei.value.details == {}


def test_assert_public_rejects_an_unclassified_column():
    with pytest.raises(EngineError) as ei:
        assert_public("_run", ["not_a_real_column"])
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_assert_public_empty_columns_is_a_noop():
    assert_public("_run", [])


_ROOT_TABLES_WITH_ORIGIN = frozenset(
    {
        "_customers",
        "_customer_contacts",
        "_locations",
        "_location_contacts",
        "_calls",
        "_jobs",
        "_appointments",
        "_technicians",
        "_technician_skills",
        "_technician_shifts",
        "_on_call_rotations",
        "_invoices",
        "_payments",
        "_ledger_entries",
        "_reviews",
    }
)

RULE_2_PRIVATE_FIELDS = frozenset(
    {Field(table, "origin") for table in _ROOT_TABLES_WITH_ORIGIN}
    | {
        Field("_locations", "effective_zone_id"),
        Field("_locations", "address_catalog_id"),
        Field("_calls", "address_catalog_id"),
    }
    | {
        Field("_technician_locations", column)
        for column in (
            "trip_origin_latitude",
            "trip_origin_longitude",
            "trip_destination_latitude",
            "trip_destination_longitude",
            "trip_departed_at",
            "trip_travel_minutes",
        )
    }
)

EXACT_SET_PRIVATE_FIELDS = frozenset(
    {Field("_customers", "modified_at")}
)

WHOLLY_PRIVATE_TABLES = frozenset(
    {
        "_skills",
        "_service_catalog",
        "_address_catalog",
        "_zone_geometry",
        "_dataset_versions",
        "_routing_anchors",
        "_technician_true_skill",
        "_technician_strain",
        "_technician_movements",
        "_technician_block_holds",
        "_events",
        "_idempotency",
        "_rng_state",
        "_random_audit",
        "_config",
        "_visibility_registry",
        "_schema_migrations",
    }
)

PUBLIC_VIEW_BASE_TABLES = frozenset(
    {
        "_business_units",
        "_job_types",
        "_zones",
        "_customers",
        "_customer_contacts",
        "_locations",
        "_location_contacts",
        "_calls",
        "_call_attempts",
        "_jobs",
        "_appointments",
        "_appointment_blocks",
        "_appointment_assignments",
        "_appointment_events",
        "_technicians",
        "_technician_skills",
        "_technician_shifts",
        "_technician_locations",
        "_technician_absences",
        "_non_job_appointments",
        "_on_call_rotations",
        "_hiring_requests",
        "_training_rates",
        "_training_reservations",
        "_invoices",
        "_invoice_items",
        "_payments",
        "_ledger_entries",
        "_reviews",
        "_notifications",
    }
)


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


def test_rule_2_private_fields_are_private_engine(conn):
    assert classify(Field("_customers", "origin")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_locations", "effective_zone_id")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_calls", "address_catalog_id")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_technician_locations", "trip_origin_latitude")) is VisibilityClass.PRIVATE_ENGINE
    for field in RULE_2_PRIVATE_FIELDS:
        assert classify(field) is VisibilityClass.PRIVATE_ENGINE, field
    assert RULE_2_PRIVATE_FIELDS <= _ddl_fields(conn)


def test_public_view_tables_are_public_except_rule_2_private_fields(conn):
    ddl = _ddl_fields(conn)
    for table in PUBLIC_VIEW_BASE_TABLES:
        columns = {f.column for f in ddl if f.table == table}
        for column in columns:
            field = Field(table, column)
            if field in RULE_2_PRIVATE_FIELDS:
                continue
            if field in EXACT_SET_PRIVATE_FIELDS:
                assert classify(field) is VisibilityClass.PRIVATE_ENGINE, field
                continue
            assert classify(field) is VisibilityClass.PUBLIC_SQL, field
        for field in RULE_2_PRIVATE_FIELDS | EXACT_SET_PRIVATE_FIELDS:
            if field.table == table:
                assert field in ddl


def test_clock_projects_exactly_the_two_company_state_fields():
    assert classify(Field("_clock", "current_time_ms")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_clock", "service_date")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_clock", "singleton")) is VisibilityClass.PRIVATE_ENGINE


def test_wholly_private_tables_contain_no_public_or_post_run_column(conn):
    ddl = _ddl_fields(conn)
    for table in WHOLLY_PRIVATE_TABLES:
        columns = {f.column for f in ddl if f.table == table}
        assert columns, f"sanity: {table} must exist in the migrated schema"
        for column in columns:
            assert classify(Field(table, column)) is VisibilityClass.PRIVATE_ENGINE, (
                f"{table}.{column} must be private_engine"
            )


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
    conn.execute("ALTER TABLE _run ADD COLUMN probe_col TEXT")
    probe = Field("_run", "probe_col")
    assert _ddl_fields(conn) - set(FIELD_REGISTRY) == {probe}
    with pytest.raises(EngineError) as ei:
        classify(probe)
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_every_registered_class_is_a_real_visibility_class():
    for value in FIELD_REGISTRY.values():
        assert isinstance(value, VisibilityClass)
