"""Tests for the field-visibility classification registry (spec 01 §Visibility
contract, §Interfaces; acceptance #8: "classification lookup works;
unclassified field fails"; CSR-002 chunk C7: the sweep that classifies every
migration-0001 column against spec 02 §7's boundary).

The completeness trio introspects the *migrated* schema (migrations.migrate on
an in-memory connection — the migration runner is the only legal DDL path) and
asserts the registry and the schema agree in both directions: every real
column is classified, and no registry entry names a column that does not
exist."""

from __future__ import annotations

import pytest

from csr_bench.db import in_memory
from csr_bench.enums import VisibilityClass
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.visibility import FIELD_REGISTRY, Field, assert_public, classify


@pytest.fixture
def conn():
    # The migrated schema, exactly as the engine builds it: migration runner
    # only, no hand-rolled DDL (spec 02 §9).
    c = in_memory()
    migrate(c)
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
    # Simulated time lives in `_clock` only (CSR-002 decision 3), so the
    # public field is `_clock.current_time_ms`, not a `_run` column.
    assert classify(Field("_clock", "current_time_ms")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_clock", "service_date")) is VisibilityClass.PUBLIC_SQL
    # The invocation timestamp equals the frozen clock by construction, but
    # nothing reads it live: `company_state`/`meta.as_of` read `_clock`, and
    # `_invocations` is not in spec 02 §5's readable-table exception list
    # (02:519). It surfaces post-run in the recorded trajectory (spec 08:705).
    assert classify(Field("_invocations", "ts_ms")) is VisibilityClass.POST_RUN_ONLY
    # Attempt count has no direct spec citation yet; grouped with the other
    # `_invocations` bookkeeping columns pending CSR-012.
    assert classify(Field("_invocations", "attempt_count")) is VisibilityClass.POST_RUN_ONLY
    # Also projected by `company_state` (`run_status`, spec 02 §4.9) and
    # `finance.get_cash_position()` (spec 08:250).
    assert classify(Field("_run", "status")) is VisibilityClass.PUBLIC_SQL
    # "reporting only" fields (spec 01:45) / result-labeling record (spec 08 §8.4).
    assert classify(Field("_run", "model_id")) is VisibilityClass.POST_RUN_ONLY
    assert classify(Field("_run", "master_seed")) is VisibilityClass.POST_RUN_ONLY
    # RNG stream state is explicitly private_engine (spec 01:282).
    assert classify(Field("_rng_state", "seed")) is VisibilityClass.PRIVATE_ENGINE
    # The idempotency cache must never leak, public or privileged alike
    # (spec 01:281 indistinguishability).
    assert classify(Field("_idempotency", "response")) is VisibilityClass.PRIVATE_ENGINE


def test_classify_accepts_a_plain_tuple_like_field():
    assert classify(("_clock", "current_time_ms")) is classify(Field("_clock", "current_time_ms"))


def test_classify_unclassified_field_raises_structured_error():
    with pytest.raises(EngineError) as ei:
        classify(Field("_run", "no_such_column"))
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD
    # Unlike UNAUTHORIZED this is a build-time/registry error, not a value that
    # reaches a public surface, so it is allowed to name the offending field.
    assert "_run.no_such_column" in ei.value.message


def test_classify_unclassified_table_raises_same_code():
    with pytest.raises(EngineError) as ei:
        classify(Field("no_such_table", "col"))
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


# ---------------------------------------------------------------------------
# assert_public()
# ---------------------------------------------------------------------------


def test_assert_public_accepts_an_all_public_column_set():
    # Must not raise.
    assert_public("_clock", ["current_time_ms", "service_date"])
    assert_public("_run", ["status"])
    assert_public("_customers", ["name", "segment"])


def test_assert_public_rejects_a_registered_private_column():
    with pytest.raises(EngineError) as ei:
        assert_public("_run", ["status", "master_seed"])
    assert ei.value.code is ErrorCode.UNAUTHORIZED
    # Uniform, secret-free message — the caller cannot learn which column or
    # table failed (spec 01:281 indistinguishability rule).
    assert ei.value.message == "unauthorized"
    assert ei.value.details == {}


def test_assert_public_rejects_an_unclassified_column():
    with pytest.raises(EngineError) as ei:
        assert_public("_run", ["not_a_real_column"])
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_assert_public_empty_columns_is_a_noop():
    assert_public("_run", [])


# ---------------------------------------------------------------------------
# The spec 02 §7 boundary, mechanically checked
# ---------------------------------------------------------------------------

#: The 15 root-entity tables that carry `origin` (spec 02 §1.3:02:58).
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

#: Exactly the columns spec 02 §7 puts in the private list beyond §5's tables:
#: `origin` everywhere it exists (§1.3), `effective_zone_id` and
#: `address_catalog_id` on `_locations`, `address_catalog_id` on `_calls`,
#: and the six `trip_*` columns on `_technician_locations` (02:596).
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

#: Private-by-exact-public-set exceptions to the §4 preamble's public-default
#: rule (02:220): fields whose §4 column list EXCLUDES them even though they
#: are not in spec 02 §7's named list. Today that is exactly
#: `_customers.modified_at` — the customers public set is the exact six-tuple
#: of spec 02 §4.1:02:241 / spec 05 §1.1 (final-branch audit F1).
EXACT_SET_PRIVATE_FIELDS = frozenset(
    {Field("_customers", "modified_at")}
)

#: The 16 tables spec 02 §5 lists as hidden state with NO public view: the
#: private catalogs (`_skills`, `_service_catalog`, `_address_catalog`,
#: `_zone_geometry`, `_dataset_versions`), the hidden performance/structural
#: tables (`_technician_true_skill`, `_technician_strain`,
#: `_technician_movements`, `_technician_block_holds`), and the run-contract
#: records (`_events`, `_idempotency`, `_rng_state`, `_random_audit`,
#: `_config`, `_visibility_registry`, `_schema_migrations`). Every column of
#: each is private_engine — no public_sql or post_run_only anywhere in them
#: (02:492-515, 02:596).
WHOLLY_PRIVATE_TABLES = frozenset(
    {
        "_skills",
        "_service_catalog",
        "_address_catalog",
        "_zone_geometry",
        "_dataset_versions",
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

#: Base tables behind the 30 data views of the §3.2 whitelist (company_state
#: reads `_clock`/`_ledger_entries`/`_run` separately). The §4 preamble's
#: default applies here: every column is public except the rule-2 private set.
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
    # Spot checks named by the chunk brief, then the full rule-2 set.
    assert classify(Field("_customers", "origin")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_locations", "effective_zone_id")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_calls", "address_catalog_id")) is VisibilityClass.PRIVATE_ENGINE
    assert classify(Field("_technician_locations", "trip_origin_latitude")) is VisibilityClass.PRIVATE_ENGINE
    for field in RULE_2_PRIVATE_FIELDS:
        assert classify(field) is VisibilityClass.PRIVATE_ENGINE, field
    # Every rule-2 field is real: none names a column the schema lacks.
    assert RULE_2_PRIVATE_FIELDS <= _ddl_fields(conn)


def test_public_view_tables_are_public_except_rule_2_private_fields(conn):
    # Spec 02 §4 preamble (02:220): the public view of the same name exposes
    # every column except those marked private and except `origin` — so on the
    # 30 view-backed base tables, everything outside the rule-2 private set
    # must be public_sql. This is the exact boundary the next chunk's view
    # passthrough test re-checks through the views themselves.
    #
    # Exception: the §4.1 exact-set note (02:241) makes `_customers.modified_at`
    # private even though §7 does not name it — the exact public set wins over
    # the default rule (final-branch audit F1).
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
        # Sanity: the private set only references columns that exist here.
        for field in RULE_2_PRIVATE_FIELDS | EXACT_SET_PRIVATE_FIELDS:
            if field.table == table:
                assert field in ddl


def test_clock_projects_exactly_the_two_company_state_fields():
    # spec 02 §5: `_clock` is the one §5 table a public view may read, "never
    # projected beyond `company_state.as_of_ms` and `service_date`" (02:519).
    assert classify(Field("_clock", "current_time_ms")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_clock", "service_date")) is VisibilityClass.PUBLIC_SQL
    assert classify(Field("_clock", "singleton")) is VisibilityClass.PRIVATE_ENGINE


def test_wholly_private_tables_contain_no_public_or_post_run_column(conn):
    # Mirror of the default-public boundary: spec 02 §5's tables have no
    # public view and every one of their columns must be private_engine —
    # "No public view reads any table in this section" (02:519) allows no
    # public_sql, and post_run_only is the class for the run-contract
    # bookkeeping spec 08 discloses in post-run analysis (`_run`,
    # `_invocations`); spec 02 §5/§7 keep the tables below engine-internal
    # for the whole run. Enumerated from the migrated DDL like the
    # completeness tests, so a future §5 table must be added to
    # WHOLLY_PRIVATE_TABLES or this test fails.
    ddl = _ddl_fields(conn)
    for table in WHOLLY_PRIVATE_TABLES:
        columns = {f.column for f in ddl if f.table == table}
        assert columns, f"sanity: {table} must exist in the migrated schema"
        for column in columns:
            assert classify(Field(table, column)) is VisibilityClass.PRIVATE_ENGINE, (
                f"{table}.{column} must be private_engine"
            )


# ---------------------------------------------------------------------------
# Registry completeness against the actual DDL (spec 01:283 coverage test
# shape) — a future migration adding a column without a matching registry
# entry fails this test, forcing the registry to be kept in sync (CSR-002
# convention: "Every new column is classified in visibility.py").
# ---------------------------------------------------------------------------


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
    # query and classify() itself detect it. The sweep classifies every
    # migration-0001 column, so the probe remains the one unmatched field.
    conn.execute("ALTER TABLE _run ADD COLUMN probe_col TEXT")
    probe = Field("_run", "probe_col")
    assert _ddl_fields(conn) - set(FIELD_REGISTRY) == {probe}
    with pytest.raises(EngineError) as ei:
        classify(probe)
    assert ei.value.code is ErrorCode.UNCLASSIFIED_FIELD


def test_every_registered_class_is_a_real_visibility_class():
    for value in FIELD_REGISTRY.values():
        assert isinstance(value, VisibilityClass)
