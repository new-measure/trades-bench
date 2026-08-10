"""Field-visibility classification registry (spec 01 §Visibility contract,
§Interfaces; acceptance #8; docs/CSR-001.md decision 5 / acceptance test 8).

Every stored field is classified in source metadata as exactly one
:class:`csr_bench.enums.VisibilityClass` member — this module is that source
metadata. :func:`classify` is the single lookup every other module must use
instead of inventing its own visibility rule (spec 01 §Purpose: "A module Must
not invent its own ... visibility rule"). An unclassified field is a build
failure, not a runtime maybe (spec 01:283: "A registry-driven coverage test
Must enumerate every public view column, SDK response field, prompt template
field, and notification message field against the metadata registry; an
unclassified field fails the build").

:func:`assert_public` is the enforcement point: given a proposed public
surface (a whitelisted view, an SDK response, ...) and the columns it exposes,
every column must resolve to one of the public classes
(:meth:`VisibilityClass.public`). A column that is registered but resolves to
a private class raises the same uniform :data:`ErrorCode.UNAUTHORIZED` used
for every other privileged/hidden probe, so the error itself cannot be used to
learn which field or table failed (spec 01:281: "Errors for a hidden table and
for a nonexistent table Must be indistinguishable").

Registry completeness (this ticket): every column of every table migration
``0001_baseline.sql`` creates — the full 49-table spec-02 schema, one run
database (CSR-002 decision 1) — is classified below. The boundary is spec 02
§7: public classes for the columns the whitelisted views of §3.2 expose,
``private_engine`` for everything in §5 (the private-table registry, the §7
private-column list, and the catalogs) and ``post_run_only`` for the
run-contract bookkeeping disclosed only in post-run analysis. The rule the §4
preamble states is applied mechanically: *"The public view of the same name
exposes every listed column except those marked private and except ``origin``"
*(spec 02:220) — so every column of an operational §4 base table defaults to
``public_sql`` unless a §4 note marks it private or it is ``origin`` (§1.3).
Per-surface enforcement (SDK/prompt/notification sweeps, the adversarial SQL
corpus) is CSR-012; the view registry (next chunk) reads these base-table
classifications through the same ``classify`` lookup.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .enums import VisibilityClass
from .errors import EngineError, ErrorCode


@dataclass(frozen=True)
class Field:
    """A ``(table, column)`` pair — the unit the visibility registry classifies."""

    table: str
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


V = VisibilityClass

# ---------------------------------------------------------------------------
# Registry: every column of every migration-0001 base table (49 tables, spec
# 02 §2/§4/§5). Mechanical default: columns of §4 operational tables are
# `public_sql` (spec 02:220 — the §4 preamble) unless a §4 note marks them
# private or they are `origin` (§1.3). Tables of §5 and the private §2
# catalogs are `private_engine` throughout. Comments cite the spec line that
# justifies a non-default choice only; the default needs no citation.
# ---------------------------------------------------------------------------

_REGISTRY: dict[Field, VisibilityClass] = {
    # =====================================================================
    # A. Run-contract records (spec 02 §5; physical form of spec 01's
    # records) — see also CSR-002 decisions 2–5.
    # =====================================================================

    # -- _run: identity, calendar, status, result-identification (01:18) -------
    # Reported post-run for result labeling / trajectory tracking, never on a
    # live agent-facing surface (spec 08 §8.4 "Every run artifact Must record":
    # spec_version, config_version, config_hash, master_seed — 08:767; harness
    # also records run_id per completed invocation — 08:705).
    Field("_run", "run_id"): V.POST_RUN_ONLY,
    Field("_run", "spec_version"): V.POST_RUN_ONLY,
    Field("_run", "config_version"): V.POST_RUN_ONLY,
    Field("_run", "config_hash"): V.POST_RUN_ONLY,
    # Listed in the post-run "run artifact" record (08:767) for reproducibility
    # / result labeling, but must never leave the engine while the run is live
    # or a replay attack could predict draws — post_run_only captures exactly
    # that "hidden during, disclosed after" shape.
    Field("_run", "master_seed"): V.POST_RUN_ONLY,
    # Fixed calendar constants (docs/CSR-001.md decision 3: same run calendar
    # and timezone every run), known to the agent out-of-band; the run record
    # itself stays off the public surface (CSR-002 decision 3 moved simulated
    # time to `_clock`, which carries the two public projections).
    Field("_run", "start_at_ms"): V.PRIVATE_ENGINE,
    Field("_run", "first_service_date"): V.PRIVATE_ENGINE,
    Field("_run", "last_service_date"): V.PRIVATE_ENGINE,
    Field("_run", "timezone"): V.PRIVATE_ENGINE,
    # Projected as `run_status` by the public `company_state` view
    # (spec 02 §4.9) and re-stated field by field by
    # `finance.get_cash_position()` (specs/08-agent-interface-and-harness.md:
    # 250) — a live public-SQL surface, not merely a post-run reporting field.
    Field("_run", "status"): V.PUBLIC_SQL,
    # "These fields exist for reporting only" (01:45), verbatim.
    Field("_run", "model_id"): V.POST_RUN_ONLY,
    Field("_run", "harness_name"): V.POST_RUN_ONLY,
    Field("_run", "harness_version"): V.POST_RUN_ONLY,
    Field("_run", "context_policy"): V.POST_RUN_ONLY,
    Field("_run", "system_prompt_hash"): V.POST_RUN_ONLY,

    # -- _config: durable resolved-config copy (CSR-002 decision 5) ---------
    # The raw resolved config blob can carry domain tunables that are
    # private_engine on their own (e.g. acceptance curves, 01:282); spec 02 §5
    # lists `_config` among the run-contract records, which are engine-internal.
    Field("_config", "run_id"): V.PRIVATE_ENGINE,
    Field("_config", "config_version"): V.PRIVATE_ENGINE,
    Field("_config", "canonical_json"): V.PRIVATE_ENGINE,

    # -- _events: the world-event envelope (queue machinery, not a domain
    # record) -------------------------------------------------------------
    # "At minimum the following are private_engine: future calls, queue size
    # and order; ... pending cancellation events" (01:282); "no future call,
    # queue length, or unhappened row is observable" (08:783); spec 02 §5 lists
    # `_events` among the run-contract records. Domain modules surface
    # *derived* operational tables/views over applied effects; the raw envelope
    # row itself never does.
    Field("_events", "event_id"): V.PRIVATE_ENGINE,
    Field("_events", "kind"): V.PRIVATE_ENGINE,
    Field("_events", "scheduled_at_ms"): V.PRIVATE_ENGINE,
    Field("_events", "priority"): V.PRIVATE_ENGINE,
    Field("_events", "entity_type"): V.PRIVATE_ENGINE,
    Field("_events", "entity_id"): V.PRIVATE_ENGINE,
    Field("_events", "payload_version"): V.PRIVATE_ENGINE,
    Field("_events", "payload"): V.PRIVATE_ENGINE,
    Field("_events", "status"): V.PRIVATE_ENGINE,
    Field("_events", "created_at_ms"): V.PRIVATE_ENGINE,
    Field("_events", "applied_at_ms"): V.PRIVATE_ENGINE,

    # -- _idempotency: response cache (public AND privileged) ---------------
    # Stores raw response bytes for both public and controller-privileged
    # requests in the same table; leaking any column (even `namespace`) could
    # let a probe tell a real privileged target from a nonexistent one,
    # breaking the indistinguishability rule (01:281). Spec 02 §5 lists it
    # among the run-contract records. (CSR-002 decision 2 renamed
    # `_command_responses` to the spec's `_idempotency`.)
    Field("_idempotency", "namespace"): V.PRIVATE_ENGINE,
    Field("_idempotency", "request_id"): V.PRIVATE_ENGINE,
    Field("_idempotency", "response"): V.PRIVATE_ENGINE,
    Field("_idempotency", "created_at_ms"): V.PRIVATE_ENGINE,

    # -- _invocations: type, timestamp, sequence, status, prompt version,
    # attempt count (01:23, 01:228) -----------------------------------------
    # The harness records (run_id, invocation_id, kind, ...) after every
    # completed invocation for trajectory tracking (08:705) — a post-run
    # concern, not something the row itself hands the agent live.
    Field("_invocations", "invocation_id"): V.POST_RUN_ONLY,
    Field("_invocations", "type"): V.POST_RUN_ONLY,
    # The invocation's own timestamp equals the frozen simulated time by
    # construction (opening an invocation freezes the clock), but nothing reads
    # this column: `company_state.as_of_ms` reads `_clock.current_time_ms`
    # (02:477) and `meta.as_of` reads the frozen clock too — `_invocations` is
    # not in spec 02 §5's exception list (only `_clock`/`_ledger_entries`/
    # `_run` are readable by public views, 02:519). The timestamp leaves the
    # engine only post-run, in the trajectory the harness records after every
    # completed invocation (08:705), so it is post_run_only like the rest of
    # the row.
    Field("_invocations", "ts_ms"): V.POST_RUN_ONLY,
    Field("_invocations", "sequence"): V.POST_RUN_ONLY,
    Field("_invocations", "status"): V.POST_RUN_ONLY,
    # Reporting-only, same shape as `system_prompt_hash` (01:45).
    Field("_invocations", "prompt_version"): V.POST_RUN_ONLY,
    # 07:162 only establishes that reminder wording differs by attempt, not
    # that the count itself is disclosed anywhere; grouped with the other
    # `_invocations` bookkeeping columns (post_run_only) until CSR-012 settles
    # whether this maps onto the public `call_attempts` view (02:200, 02:594).
    Field("_invocations", "attempt_count"): V.POST_RUN_ONLY,

    # -- _rng_state: sequential-stream state ---------------------------------
    # "RNG stream states" is explicitly private_engine, verbatim (01:282);
    # spec 02 §5 lists `_rng_state` among the run-contract records.
    Field("_rng_state", "stream_name"): V.PRIVATE_ENGINE,
    Field("_rng_state", "seed"): V.PRIVATE_ENGINE,
    Field("_rng_state", "counter"): V.PRIVATE_ENGINE,

    # -- _random_audit: consumed-draw audit log ------------------------------
    # Spec 02 §5 lists `_random_audit` under hidden state; spec 01:275's
    # `post_run_only` definition ("full private database, random audit, and
    # trajectory, for post-run analysis") also reads as a fit, but the spec-02
    # boundary settles it as engine-internal during the run — the same
    # "hidden during, disclosed after" shape CSR-012 resolves per surface.
    # The row itself never holds the u64 result or the seed, so no value here
    # could enable prediction regardless of class.
    Field("_random_audit", "seq"): V.PRIVATE_ENGINE,
    Field("_random_audit", "stream_name"): V.PRIVATE_ENGINE,
    Field("_random_audit", "entity_type"): V.PRIVATE_ENGINE,
    Field("_random_audit", "entity_id"): V.PRIVATE_ENGINE,
    Field("_random_audit", "scope"): V.PRIVATE_ENGINE,
    Field("_random_audit", "event_kind"): V.PRIVATE_ENGINE,
    Field("_random_audit", "draw_index"): V.PRIVATE_ENGINE,
    Field("_random_audit", "outcome_id"): V.PRIVATE_ENGINE,
    Field("_random_audit", "simulated_ts_ms"): V.PRIVATE_ENGINE,

    # -- _clock: simulated time (CSR-002 decision 3; spec 02 §1.5/§4.9) ------
    # Spec 02 §5: "No public view reads any table in this section, except
    # `_clock` (used only as a filter, never projected beyond
    # `company_state.as_of_ms` and `service_date`)". The two projected fields
    # are public_sql; `singleton` is the structural pin row, never projected.
    Field("_clock", "singleton"): V.PRIVATE_ENGINE,
    Field("_clock", "current_time_ms"): V.PUBLIC_SQL,
    Field("_clock", "service_date"): V.PUBLIC_SQL,

    # -- _visibility_registry: durable copy of THIS registry (decision 5) ----
    # Spec 02 §5 lists it among the run-contract records; it is the future
    # authorizer's table (decision 5), never an agent-readable relation.
    Field("_visibility_registry", "table"): V.PRIVATE_ENGINE,
    Field("_visibility_registry", "column"): V.PRIVATE_ENGINE,
    Field("_visibility_registry", "visibility_class"): V.PRIVATE_ENGINE,

    # -- _schema_migrations: applied-migration ledger (spec 02 §9) -----------
    # Engine bookkeeping; spec 02 §5 lists it under hidden state.
    Field("_schema_migrations", "version"): V.PRIVATE_ENGINE,
    Field("_schema_migrations", "name"): V.PRIVATE_ENGINE,
    Field("_schema_migrations", "checksum"): V.PRIVATE_ENGINE,
    Field("_schema_migrations", "applied_at"): V.PRIVATE_ENGINE,

    # =====================================================================
    # B. Reference and catalog data (spec 02 §2) — the §2 text states the
    # private/public split for each table explicitly; it does not follow the
    # §4 public-default rule.
    # =====================================================================

    # -- _business_units: the three units (02 §2.1) --------------------------
    # `business_units` is on the whitelist (§3.2); all columns are projected.
    Field("_business_units", "id"): V.PUBLIC_SQL,
    Field("_business_units", "code"): V.PUBLIC_SQL,
    Field("_business_units", "name"): V.PUBLIC_SQL,
    Field("_business_units", "active"): V.PUBLIC_SQL,
    Field("_business_units", "created_at"): V.PUBLIC_SQL,

    # -- _skills: the qualification vocabulary (02 §2.2) ----------------------
    # "The four codes are not job-type codes ... `_skills` itself is private —
    # it is the qualification vocabulary and its business-unit mapping, not an
    # operational record. The code text is public wherever it already appears
    # on a public row ...; there is simply no `skills` view to enumerate or
    # join against" (02:120). Spec 02 §5 lists `_skills` under catalogs.
    Field("_skills", "id"): V.PRIVATE_ENGINE,
    Field("_skills", "code"): V.PRIVATE_ENGINE,
    Field("_skills", "name"): V.PRIVATE_ENGINE,
    Field("_skills", "business_unit_id"): V.PRIVATE_ENGINE,
    Field("_skills", "active"): V.PRIVATE_ENGINE,
    Field("_skills", "created_at"): V.PRIVATE_ENGINE,

    # -- _job_types: the five types plus the misbooking sink (02 §2.3) --------
    # "`job_types` is public" (02:141); `job_types` is on the whitelist (§3.2).
    # `required_skill_code` is public because the code text is public wherever
    # it already appears on a public row — here, on the public `job_types` view
    # itself (02:120). Supportedness is NOT resolvable here: nothing maps an
    # arbitrary requested category to a type (02 §2.5, §7 note 1).
    Field("_job_types", "id"): V.PUBLIC_SQL,
    Field("_job_types", "code"): V.PUBLIC_SQL,
    Field("_job_types", "name"): V.PUBLIC_SQL,
    Field("_job_types", "business_unit_id"): V.PUBLIC_SQL,
    Field("_job_types", "scheduled_blocks"): V.PUBLIC_SQL,
    Field("_job_types", "required_skill_code"): V.PUBLIC_SQL,
    Field("_job_types", "active"): V.PUBLIC_SQL,
    Field("_job_types", "created_at"): V.PUBLIC_SQL,

    # -- _zones: the ten zones (02 §2.4) --------------------------------------
    # `zones` is on the whitelist (§3.2); the city/postal lists ride the public
    # row so the agent can reason about coverage (02 §2.4).
    Field("_zones", "id"): V.PUBLIC_SQL,
    Field("_zones", "code"): V.PUBLIC_SQL,
    Field("_zones", "name"): V.PUBLIC_SQL,
    Field("_zones", "cities_json"): V.PUBLIC_SQL,
    Field("_zones", "postal_codes_json"): V.PUBLIC_SQL,
    Field("_zones", "active"): V.PUBLIC_SQL,
    Field("_zones", "created_at"): V.PUBLIC_SQL,

    # -- _zone_geometry: private nearest-zone assignment support (02 §2.4) ----
    # "`_zone_geometry(zone_id PK, centroid_lat, centroid_lon, boundary_ref)
    # -- private" (02:149); spec 02 §5 lists it under catalogs. It supports
    # the private `effective_zone_id` assignment, never a public surface.
    Field("_zone_geometry", "zone_id"): V.PRIVATE_ENGINE,
    Field("_zone_geometry", "centroid_lat"): V.PRIVATE_ENGINE,
    Field("_zone_geometry", "centroid_lon"): V.PRIVATE_ENGINE,
    Field("_zone_geometry", "boundary_ref"): V.PRIVATE_ENGINE,

    # -- _service_catalog: requested-service catalog (02 §2.5) ----------------
    # "The catalog and its supported-mapping are **private**" (02:156); §7:
    # "the requested-service catalog and its supported-mapping" are in the
    # private list. Public exposure is limited to the category name TEXT on the
    # call row (`calls.requested_service_name`, 02:162); a public catalog
    # column would let the agent resolve supportedness by one join (SCH-07).
    Field("_service_catalog", "id"): V.PRIVATE_ENGINE,
    Field("_service_catalog", "catalog_version"): V.PRIVATE_ENGINE,
    Field("_service_catalog", "name"): V.PRIVATE_ENGINE,
    Field("_service_catalog", "supported_job_type_id"): V.PRIVATE_ENGINE,
    Field("_service_catalog", "demand_weight_key"): V.PRIVATE_ENGINE,
    Field("_service_catalog", "active"): V.PRIVATE_ENGINE,

    # -- _address_catalog: frozen map artifact (02 §2.6) ----------------------
    # "the catalog is never exposed publicly — a public address list would let
    # the agent enumerate future callers' addresses" (02:180); spec 02 §5
    # lists it under catalogs.
    Field("_address_catalog", "id"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "dataset_version"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "house_number"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "street"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "city"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "state"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "postal_code"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "formatted_address"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "canonical_key"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "latitude"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "longitude"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "zone_id"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "effective_zone_id"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "anchor_id"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "access_distance_km"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "segment_eligibility"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "status"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "unusable_reason"): V.PRIVATE_ENGINE,
    Field("_address_catalog", "created_at"): V.PRIVATE_ENGINE,

    # -- _dataset_versions: frozen-dataset identity (02 §2.6) -----------------
    # Spec 02 §5 lists `_dataset_versions` under catalogs and frozen datasets.
    Field("_dataset_versions", "id"): V.PRIVATE_ENGINE,
    Field("_dataset_versions", "kind"): V.PRIVATE_ENGINE,
    Field("_dataset_versions", "version"): V.PRIVATE_ENGINE,
    Field("_dataset_versions", "snapshot_date"): V.PRIVATE_ENGINE,
    Field("_dataset_versions", "checksum"): V.PRIVATE_ENGINE,
    Field("_dataset_versions", "attribution"): V.PRIVATE_ENGINE,
    Field("_dataset_versions", "created_at"): V.PRIVATE_ENGINE,

    # =====================================================================
    # C. Operational tables (spec 02 §4.1–§4.8) — public-default rule
    # (02:220): every column is public_sql unless the §4 note marks it
    # private or it is `origin` (§1.3). Tables whose §4 text marks NOTHING
    # private are public throughout.
    # =====================================================================

    # -- §4.1 CRM -----------------------------------------------------------
    # _customers: the public column set is exactly
    # `(id, name, segment, status, created_at, created_from_call_id)`
    # (02:241; spec 05 §1.1). `modified_at` is NOT in that exact set and is
    # not projected by any view, so it is private_engine — the §4 preamble's
    # public-default rule yields to the exact-set note (final-branch audit F1).
    # `origin` is private (§1.3).
    Field("_customers", "id"): V.PUBLIC_SQL,
    Field("_customers", "name"): V.PUBLIC_SQL,
    Field("_customers", "segment"): V.PUBLIC_SQL,
    Field("_customers", "status"): V.PUBLIC_SQL,
    Field("_customers", "created_at"): V.PUBLIC_SQL,
    Field("_customers", "created_from_call_id"): V.PUBLIC_SQL,
    Field("_customers", "modified_at"): V.PRIVATE_ENGINE,
    # §1.3: "origin is private and Must not appear in any public view: it would
    # tell the agent which rows the engine authored" (02:58; SCH-08).
    Field("_customers", "origin"): V.PRIVATE_ENGINE,

    # _customer_contacts: public view of the same name (02 §4.1); `origin`
    # private (§1.3).
    Field("_customer_contacts", "id"): V.PUBLIC_SQL,
    Field("_customer_contacts", "customer_id"): V.PUBLIC_SQL,
    Field("_customer_contacts", "contact_type"): V.PUBLIC_SQL,
    Field("_customer_contacts", "value"): V.PUBLIC_SQL,
    Field("_customer_contacts", "do_not_text"): V.PUBLIC_SQL,
    Field("_customer_contacts", "memo"): V.PUBLIC_SQL,
    Field("_customer_contacts", "created_at"): V.PUBLIC_SQL,
    Field("_customer_contacts", "origin"): V.PRIVATE_ENGINE,

    # _locations: `effective_zone_id` and `address_catalog_id` are marked
    # private in the §4.1 column list (02:234) — the nearest-zone assignment
    # is weather/demand machinery, and the catalog link would expose the
    # private address catalog (02 §2.6). `origin` private (§1.3).
    Field("_locations", "id"): V.PUBLIC_SQL,
    Field("_locations", "customer_id"): V.PUBLIC_SQL,
    Field("_locations", "name"): V.PUBLIC_SQL,
    Field("_locations", "street"): V.PUBLIC_SQL,
    Field("_locations", "unit"): V.PUBLIC_SQL,
    Field("_locations", "city"): V.PUBLIC_SQL,
    Field("_locations", "state"): V.PUBLIC_SQL,
    Field("_locations", "postal_code"): V.PUBLIC_SQL,
    # latitude/longitude are public, matching ServiceTitan (02:244).
    Field("_locations", "latitude"): V.PUBLIC_SQL,
    Field("_locations", "longitude"): V.PUBLIC_SQL,
    Field("_locations", "zone_id"): V.PUBLIC_SQL,
    Field("_locations", "active"): V.PUBLIC_SQL,
    Field("_locations", "created_at"): V.PUBLIC_SQL,
    Field("_locations", "modified_at"): V.PUBLIC_SQL,
    Field("_locations", "effective_zone_id"): V.PRIVATE_ENGINE,
    Field("_locations", "address_catalog_id"): V.PRIVATE_ENGINE,
    Field("_locations", "origin"): V.PRIVATE_ENGINE,

    # _location_contacts: public view of the same name (02 §4.1); `origin`
    # private (§1.3).
    Field("_location_contacts", "id"): V.PUBLIC_SQL,
    Field("_location_contacts", "location_id"): V.PUBLIC_SQL,
    Field("_location_contacts", "contact_type"): V.PUBLIC_SQL,
    Field("_location_contacts", "value"): V.PUBLIC_SQL,
    Field("_location_contacts", "do_not_text"): V.PUBLIC_SQL,
    Field("_location_contacts", "memo"): V.PUBLIC_SQL,
    Field("_location_contacts", "created_at"): V.PUBLIC_SQL,
    Field("_location_contacts", "origin"): V.PRIVATE_ENGINE,

    # -- §4.2 Calls -----------------------------------------------------------
    # _calls: `address_catalog_id` is marked private in the §4.2 column list
    # (02:257); `origin` private (§1.3). `requested_service_name` is public —
    # it IS the §2.5 public exposure: "Public exposure is limited to the
    # category name text on the call row (`calls.requested_service_name`)"
    # (02:162). The acceptance decision and its draw key never ride this row
    # (02:274).
    Field("_calls", "id"): V.PUBLIC_SQL,
    Field("_calls", "arrived_at"): V.PUBLIC_SQL,
    Field("_calls", "service_date"): V.PUBLIC_SQL,
    Field("_calls", "call_kind"): V.PUBLIC_SQL,
    Field("_calls", "status"): V.PUBLIC_SQL,
    Field("_calls", "caller_name"): V.PUBLIC_SQL,
    Field("_calls", "caller_phone"): V.PUBLIC_SQL,
    Field("_calls", "segment"): V.PUBLIC_SQL,
    Field("_calls", "customer_id"): V.PUBLIC_SQL,
    Field("_calls", "location_id"): V.PUBLIC_SQL,
    Field("_calls", "service_street"): V.PUBLIC_SQL,
    Field("_calls", "service_unit"): V.PUBLIC_SQL,
    Field("_calls", "service_city"): V.PUBLIC_SQL,
    Field("_calls", "service_state"): V.PUBLIC_SQL,
    Field("_calls", "service_postal_code"): V.PUBLIC_SQL,
    Field("_calls", "requested_service_name"): V.PUBLIC_SQL,
    Field("_calls", "urgent"): V.PUBLIC_SQL,
    Field("_calls", "related_job_id"): V.PUBLIC_SQL,
    Field("_calls", "target_appointment_id"): V.PUBLIC_SQL,
    Field("_calls", "target_job_id"): V.PUBLIC_SQL,
    Field("_calls", "created_job_id"): V.PUBLIC_SQL,
    Field("_calls", "terminal_at"): V.PUBLIC_SQL,
    Field("_calls", "terminal_reason"): V.PUBLIC_SQL,
    Field("_calls", "created_at"): V.PUBLIC_SQL,
    Field("_calls", "origin"): V.PRIVATE_ENGINE,
    Field("_calls", "address_catalog_id"): V.PRIVATE_ENGINE,

    # _call_attempts: no private marks in §4.2; `call_attempts` is on the
    # whitelist (02:200, 02:594) with the full row.
    Field("_call_attempts", "id"): V.PUBLIC_SQL,
    Field("_call_attempts", "call_id"): V.PUBLIC_SQL,
    Field("_call_attempts", "attempt_number"): V.PUBLIC_SQL,
    Field("_call_attempts", "prompted_at"): V.PUBLIC_SQL,
    Field("_call_attempts", "outcome"): V.PUBLIC_SQL,
    Field("_call_attempts", "created_at"): V.PUBLIC_SQL,

    # -- §4.3 Jobs -------------------------------------------------------------
    # _jobs: no private marks in the §4.3 column list; `origin` private
    # (§1.3). Hidden per-job state lives in `_job_outcomes` (§5), never here.
    Field("_jobs", "id"): V.PUBLIC_SQL,
    Field("_jobs", "job_number"): V.PUBLIC_SQL,
    Field("_jobs", "call_id"): V.PUBLIC_SQL,
    Field("_jobs", "customer_id"): V.PUBLIC_SQL,
    Field("_jobs", "location_id"): V.PUBLIC_SQL,
    Field("_jobs", "job_type_id"): V.PUBLIC_SQL,
    Field("_jobs", "business_unit_id"): V.PUBLIC_SQL,
    Field("_jobs", "requested_service_name"): V.PUBLIC_SQL,
    Field("_jobs", "status"): V.PUBLIC_SQL,
    Field("_jobs", "outcome"): V.PUBLIC_SQL,
    Field("_jobs", "priority"): V.PUBLIC_SQL,
    Field("_jobs", "no_charge"): V.PUBLIC_SQL,
    Field("_jobs", "recall_for_job_id"): V.PUBLIC_SQL,
    Field("_jobs", "summary"): V.PUBLIC_SQL,
    Field("_jobs", "created_at"): V.PUBLIC_SQL,
    Field("_jobs", "completed_at"): V.PUBLIC_SQL,
    Field("_jobs", "modified_at"): V.PUBLIC_SQL,
    Field("_jobs", "origin"): V.PRIVATE_ENGINE,

    # -- §4.4 Appointments, blocks, assignments, events -----------------------
    # _appointments: no private marks in the §4.4 column list; `origin`
    # private (§1.3). Actual timing is public — "the agent's only signal for
    # lateness and duration patterns" (02:335).
    Field("_appointments", "id"): V.PUBLIC_SQL,
    Field("_appointments", "appointment_number"): V.PUBLIC_SQL,
    Field("_appointments", "job_id"): V.PUBLIC_SQL,
    Field("_appointments", "status"): V.PUBLIC_SQL,
    Field("_appointments", "scheduled_start"): V.PUBLIC_SQL,
    Field("_appointments", "scheduled_end"): V.PUBLIC_SQL,
    Field("_appointments", "arrival_window_start"): V.PUBLIC_SQL,
    Field("_appointments", "arrival_window_end"): V.PUBLIC_SQL,
    Field("_appointments", "actual_start"): V.PUBLIC_SQL,
    Field("_appointments", "actual_end"): V.PUBLIC_SQL,
    Field("_appointments", "cancelled_at"): V.PUBLIC_SQL,
    Field("_appointments", "created_at"): V.PUBLIC_SQL,
    Field("_appointments", "modified_at"): V.PUBLIC_SQL,
    Field("_appointments", "origin"): V.PRIVATE_ENGINE,

    # _appointment_blocks: no private marks in §4.4; `appointment_blocks` is
    # on the whitelist (02:200).
    Field("_appointment_blocks", "id"): V.PUBLIC_SQL,
    Field("_appointment_blocks", "appointment_id"): V.PUBLIC_SQL,
    Field("_appointment_blocks", "ordinal"): V.PUBLIC_SQL,
    Field("_appointment_blocks", "block_code"): V.PUBLIC_SQL,
    Field("_appointment_blocks", "block_start"): V.PUBLIC_SQL,
    Field("_appointment_blocks", "block_end"): V.PUBLIC_SQL,
    Field("_appointment_blocks", "created_at"): V.PUBLIC_SQL,

    # _appointment_assignments: no private marks in §4.4; reassignment
    # history is the point of the separate record (02:332).
    Field("_appointment_assignments", "id"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "appointment_id"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "job_id"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "technician_id"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "status"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "assigned_at"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "unassigned_at"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "assigned_by"): V.PUBLIC_SQL,
    Field("_appointment_assignments", "created_at"): V.PUBLIC_SQL,

    # _appointment_events: no private marks in §4.4; the event trail is
    # public — "Actual timing (actual_start, actual_end, and the event trail)
    # is public" (02:335).
    Field("_appointment_events", "id"): V.PUBLIC_SQL,
    Field("_appointment_events", "appointment_id"): V.PUBLIC_SQL,
    Field("_appointment_events", "technician_id"): V.PUBLIC_SQL,
    Field("_appointment_events", "event_type"): V.PUBLIC_SQL,
    Field("_appointment_events", "occurred_at"): V.PUBLIC_SQL,
    Field("_appointment_events", "service_date"): V.PUBLIC_SQL,
    Field("_appointment_events", "detail"): V.PUBLIC_SQL,
    Field("_appointment_events", "created_at"): V.PUBLIC_SQL,

    # _technician_block_holds: the structural overlap guard — "Holds are
    # private: the public schedule is already fully visible through
    # appointments, appointment_blocks, appointment_assignments,
    # non_job_appointments, and technician_shifts" (02:334); spec 02 §5 and
    # §7 list block holds under private state.
    Field("_technician_block_holds", "technician_id"): V.PRIVATE_ENGINE,
    Field("_technician_block_holds", "block_start"): V.PRIVATE_ENGINE,
    Field("_technician_block_holds", "hold_kind"): V.PRIVATE_ENGINE,
    Field("_technician_block_holds", "appointment_id"): V.PRIVATE_ENGINE,
    Field("_technician_block_holds", "non_job_appointment_id"): V.PRIVATE_ENGINE,
    Field("_technician_block_holds", "created_at"): V.PRIVATE_ENGINE,

    # -- §4.5 Workforce --------------------------------------------------------
    # _technicians: no private marks in the §4.5 column list; `origin` private
    # (§1.3). Visible technician facts are exactly the Q38 set (02:383);
    # skill/strain/performance live in the private tables below.
    Field("_technicians", "id"): V.PUBLIC_SQL,
    Field("_technicians", "name"): V.PUBLIC_SQL,
    Field("_technicians", "street"): V.PUBLIC_SQL,
    Field("_technicians", "unit"): V.PUBLIC_SQL,
    Field("_technicians", "city"): V.PUBLIC_SQL,
    Field("_technicians", "state"): V.PUBLIC_SQL,
    Field("_technicians", "postal_code"): V.PUBLIC_SQL,
    Field("_technicians", "home_latitude"): V.PUBLIC_SQL,
    Field("_technicians", "home_longitude"): V.PUBLIC_SQL,
    Field("_technicians", "home_zone_id"): V.PUBLIC_SQL,
    Field("_technicians", "joined_on"): V.PUBLIC_SQL,
    # The Q38 set includes "hourly wage" (02:383); no performance metric is
    # public (02:601) but the contracted rate is an employment term, not a
    # metric.
    Field("_technicians", "hourly_wage_cents"): V.PUBLIC_SQL,
    Field("_technicians", "employment_status"): V.PUBLIC_SQL,
    Field("_technicians", "availability_status"): V.PUBLIC_SQL,
    Field("_technicians", "available_from"): V.PUBLIC_SQL,
    Field("_technicians", "final_working_date"): V.PUBLIC_SQL,
    Field("_technicians", "created_at"): V.PUBLIC_SQL,
    Field("_technicians", "modified_at"): V.PUBLIC_SQL,
    Field("_technicians", "origin"): V.PRIVATE_ENGINE,

    # _technician_skills: no private marks in §4.5; `skill_code` is public
    # code text on a public row (02:120). `origin` private (§1.3).
    Field("_technician_skills", "id"): V.PUBLIC_SQL,
    Field("_technician_skills", "technician_id"): V.PUBLIC_SQL,
    Field("_technician_skills", "skill_code"): V.PUBLIC_SQL,
    Field("_technician_skills", "active"): V.PUBLIC_SQL,
    Field("_technician_skills", "granted_on"): V.PUBLIC_SQL,
    Field("_technician_skills", "created_at"): V.PUBLIC_SQL,
    Field("_technician_skills", "origin"): V.PRIVATE_ENGINE,

    # _technician_shifts: no private marks in §4.5; "Shifts are the
    # availability contract" (02:386). `origin` private (§1.3).
    Field("_technician_shifts", "id"): V.PUBLIC_SQL,
    Field("_technician_shifts", "technician_id"): V.PUBLIC_SQL,
    Field("_technician_shifts", "shift_type"): V.PUBLIC_SQL,
    Field("_technician_shifts", "start_at"): V.PUBLIC_SQL,
    Field("_technician_shifts", "end_at"): V.PUBLIC_SQL,
    Field("_technician_shifts", "service_date"): V.PUBLIC_SQL,
    Field("_technician_shifts", "active"): V.PUBLIC_SQL,
    Field("_technician_shifts", "on_call_rotation_id"): V.PUBLIC_SQL,
    Field("_technician_shifts", "created_at"): V.PUBLIC_SQL,
    Field("_technician_shifts", "origin"): V.PRIVATE_ENGINE,

    # _technician_locations: the public live-GPS row is exactly the non-trip
    # columns; "The six `trip_*` columns are `private_engine` and are non-null
    # only while `location_state = 'traveling'`; the public
    # `technician_locations` view is a read-time projection ... never exposes
    # them" (02:388). Spec 02 §7 lists the six trip_* columns in the private
    # set.
    Field("_technician_locations", "technician_id"): V.PUBLIC_SQL,
    Field("_technician_locations", "latitude"): V.PUBLIC_SQL,
    Field("_technician_locations", "longitude"): V.PUBLIC_SQL,
    Field("_technician_locations", "coordinates_updated_at"): V.PUBLIC_SQL,
    Field("_technician_locations", "location_state"): V.PUBLIC_SQL,
    Field("_technician_locations", "trip_origin_latitude"): V.PRIVATE_ENGINE,
    Field("_technician_locations", "trip_origin_longitude"): V.PRIVATE_ENGINE,
    Field("_technician_locations", "trip_destination_latitude"): V.PRIVATE_ENGINE,
    Field("_technician_locations", "trip_destination_longitude"): V.PRIVATE_ENGINE,
    Field("_technician_locations", "trip_departed_at"): V.PRIVATE_ENGINE,
    Field("_technician_locations", "trip_travel_minutes"): V.PRIVATE_ENGINE,

    # _technician_movements: "It is `private_engine`; the leg surfaces publicly
    # only through the live-location projection" (02:389); spec 02 §5 lists it
    # under hidden state.
    Field("_technician_movements", "id"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "technician_id"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "service_date"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "movement_kind"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "origin_latitude"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "origin_longitude"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "destination_latitude"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "destination_longitude"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "departed_at"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "arrives_at"): V.PRIVATE_ENGINE,
    Field("_technician_movements", "created_at"): V.PRIVATE_ENGINE,

    # _technician_absences: no private marks in §4.5; `technician_absences` is
    # on the whitelist (02:202) — sickness is visible exactly as
    # `availability_status = 'sick'` plus an open absence row (02:384).
    Field("_technician_absences", "id"): V.PUBLIC_SQL,
    Field("_technician_absences", "technician_id"): V.PUBLIC_SQL,
    Field("_technician_absences", "absence_kind"): V.PUBLIC_SQL,
    Field("_technician_absences", "start_date"): V.PUBLIC_SQL,
    Field("_technician_absences", "expected_return_date"): V.PUBLIC_SQL,
    Field("_technician_absences", "actual_return_date"): V.PUBLIC_SQL,
    Field("_technician_absences", "status"): V.PUBLIC_SQL,
    Field("_technician_absences", "created_at"): V.PUBLIC_SQL,

    # _non_job_appointments: no private marks in §4.5; `non_job_appointments`
    # is on the whitelist (02:202).
    Field("_non_job_appointments", "id"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "technician_id"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "kind"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "name"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "start_at"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "end_at"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "block_code"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "status"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "training_reservation_id"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "technician_absence_id"): V.PUBLIC_SQL,
    Field("_non_job_appointments", "created_at"): V.PUBLIC_SQL,

    # _on_call_rotations: no private marks in the §4.5 column list; the
    # rotation roster is on the whitelist (02:202). `origin` private (§1.3).
    Field("_on_call_rotations", "id"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "business_unit_id"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "technician_id"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "effective_start_date"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "effective_end_date"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "status"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "vacated_at"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "created_at"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "modified_at"): V.PUBLIC_SQL,
    Field("_on_call_rotations", "origin"): V.PRIVATE_ENGINE,

    # -- Hidden performance state (spec 04 §1.5; spec 02 §5) ------------------
    # _technician_true_skill / _technician_strain: spec 02 §5 lists both under
    # hidden state; §7: "No performance metric is public. Not skill, not
    # strain, not a rate, not a band, not a lag indicator. Only raw history"
    # (02:601).
    Field("_technician_true_skill", "technician_id"): V.PRIVATE_ENGINE,
    Field("_technician_true_skill", "skill_code"): V.PRIVATE_ENGINE,
    Field("_technician_true_skill", "base_skill"): V.PRIVATE_ENGINE,
    Field("_technician_true_skill", "training_gain"): V.PRIVATE_ENGINE,
    Field("_technician_true_skill", "updated_at"): V.PRIVATE_ENGINE,
    Field("_technician_strain", "technician_id"): V.PRIVATE_ENGINE,
    Field("_technician_strain", "strain_value"): V.PRIVATE_ENGINE,
    Field("_technician_strain", "last_updated_date"): V.PRIVATE_ENGINE,

    # -- §4.6 Hiring and training ---------------------------------------------
    # _hiring_requests: no private marks in the §4.6 column list; "Hiring is
    # one committed action; the response reveals wage, recruiting cost, and
    # start date, and true quality stays hidden" (02:411) — the hidden part
    # lives in the labor-economy state of §5, not in a column here. The §7
    # public list names "hiring requests with committed terms" (02:594).
    Field("_hiring_requests", "id"): V.PUBLIC_SQL,
    Field("_hiring_requests", "requested_at"): V.PUBLIC_SQL,
    Field("_hiring_requests", "requested_skill_codes_json"): V.PUBLIC_SQL,
    Field("_hiring_requests", "minimum_experience_years"): V.PUBLIC_SQL,
    Field("_hiring_requests", "preferred_home_zone_id"): V.PUBLIC_SQL,
    Field("_hiring_requests", "status"): V.PUBLIC_SQL,
    Field("_hiring_requests", "recruiting_cost_cents"): V.PUBLIC_SQL,
    Field("_hiring_requests", "committed_hourly_wage_cents"): V.PUBLIC_SQL,
    Field("_hiring_requests", "committed_start_date"): V.PUBLIC_SQL,
    Field("_hiring_requests", "technician_id"): V.PUBLIC_SQL,
    Field("_hiring_requests", "created_at"): V.PUBLIC_SQL,
    Field("_hiring_requests", "modified_at"): V.PUBLIC_SQL,

    # _training_rates: no private marks in §4.6; the §7 public list names
    # "past and current training rates". `rate_date` is public — the
    # "tomorrow's rates are unknowable" rule (02:412) is the view-time filter
    # `rate_date <= k.service_date` (02:89), not a classification.
    Field("_training_rates", "id"): V.PUBLIC_SQL,
    Field("_training_rates", "skill_code"): V.PUBLIC_SQL,
    Field("_training_rates", "rate_date"): V.PUBLIC_SQL,
    Field("_training_rates", "hourly_rate_cents"): V.PUBLIC_SQL,
    Field("_training_rates", "created_at"): V.PUBLIC_SQL,

    # _training_reservations: no private marks in §4.6; the §7 public list
    # names "training reservations". The locked rate and prepaid cost are the
    # committed terms of the reservation (02:413), not a hidden market rate.
    Field("_training_reservations", "id"): V.PUBLIC_SQL,
    Field("_training_reservations", "technician_id"): V.PUBLIC_SQL,
    Field("_training_reservations", "skill_code"): V.PUBLIC_SQL,
    Field("_training_reservations", "scheduled_at"): V.PUBLIC_SQL,
    Field("_training_reservations", "total_hours"): V.PUBLIC_SQL,
    Field("_training_reservations", "locked_hourly_rate_cents"): V.PUBLIC_SQL,
    Field("_training_reservations", "prepaid_cost_cents"): V.PUBLIC_SQL,
    Field("_training_reservations", "status"): V.PUBLIC_SQL,
    Field("_training_reservations", "completed_hours"): V.PUBLIC_SQL,
    Field("_training_reservations", "created_at"): V.PUBLIC_SQL,
    Field("_training_reservations", "modified_at"): V.PUBLIC_SQL,

    # -- §4.7 Money ------------------------------------------------------------
    # _invoices: no private marks in the §4.7 column list; `origin` private
    # (§1.3). `material_percent_basis_points` is public after completion only
    # — but it exists on no row before the completion transaction (02:444), so
    # the temporal rule is a row-existence fact, not a classification.
    Field("_invoices", "id"): V.PUBLIC_SQL,
    Field("_invoices", "invoice_number"): V.PUBLIC_SQL,
    Field("_invoices", "job_id"): V.PUBLIC_SQL,
    Field("_invoices", "customer_id"): V.PUBLIC_SQL,
    Field("_invoices", "location_id"): V.PUBLIC_SQL,
    Field("_invoices", "business_unit_id"): V.PUBLIC_SQL,
    Field("_invoices", "invoice_date"): V.PUBLIC_SQL,
    Field("_invoices", "issued_at"): V.PUBLIC_SQL,
    Field("_invoices", "status"): V.PUBLIC_SQL,
    Field("_invoices", "no_charge"): V.PUBLIC_SQL,
    Field("_invoices", "subtotal_cents"): V.PUBLIC_SQL,
    Field("_invoices", "total_cents"): V.PUBLIC_SQL,
    Field("_invoices", "material_cost_cents"): V.PUBLIC_SQL,
    Field("_invoices", "material_percent_basis_points"): V.PUBLIC_SQL,
    Field("_invoices", "created_at"): V.PUBLIC_SQL,
    Field("_invoices", "origin"): V.PRIVATE_ENGINE,

    # _invoice_items: no private marks in §4.7; `invoice_items` is on the
    # whitelist (02:204).
    Field("_invoice_items", "id"): V.PUBLIC_SQL,
    Field("_invoice_items", "invoice_id"): V.PUBLIC_SQL,
    Field("_invoice_items", "item_type"): V.PUBLIC_SQL,
    Field("_invoice_items", "description"): V.PUBLIC_SQL,
    Field("_invoice_items", "quantity"): V.PUBLIC_SQL,
    Field("_invoice_items", "amount_cents"): V.PUBLIC_SQL,
    Field("_invoice_items", "created_at"): V.PUBLIC_SQL,

    # _payments: no private marks in the §4.7 column list; `origin` private
    # (§1.3).
    Field("_payments", "id"): V.PUBLIC_SQL,
    Field("_payments", "payment_number"): V.PUBLIC_SQL,
    Field("_payments", "invoice_id"): V.PUBLIC_SQL,
    Field("_payments", "customer_id"): V.PUBLIC_SQL,
    Field("_payments", "paid_at"): V.PUBLIC_SQL,
    Field("_payments", "amount_cents"): V.PUBLIC_SQL,
    Field("_payments", "payment_type"): V.PUBLIC_SQL,
    Field("_payments", "status"): V.PUBLIC_SQL,
    Field("_payments", "created_at"): V.PUBLIC_SQL,
    Field("_payments", "origin"): V.PRIVATE_ENGINE,

    # _ledger_entries: "Cash is the ledger" (02:445) and the §7 public list
    # names "the full ledger" (02:594) — every column is public except
    # `origin` (§1.3). `balance_after_cents` is part of the public record; the
    # distribution that produced an amount is not stored here.
    Field("_ledger_entries", "id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "occurred_at"): V.PUBLIC_SQL,
    Field("_ledger_entries", "service_date"): V.PUBLIC_SQL,
    Field("_ledger_entries", "entry_type"): V.PUBLIC_SQL,
    Field("_ledger_entries", "amount_cents"): V.PUBLIC_SQL,
    Field("_ledger_entries", "balance_after_cents"): V.PUBLIC_SQL,
    Field("_ledger_entries", "job_id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "invoice_id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "payment_id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "technician_id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "hiring_request_id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "training_reservation_id"): V.PUBLIC_SQL,
    Field("_ledger_entries", "description"): V.PUBLIC_SQL,
    Field("_ledger_entries", "created_at"): V.PUBLIC_SQL,
    Field("_ledger_entries", "origin"): V.PRIVATE_ENGINE,

    # -- §4.8 Reputation and alerts --------------------------------------------
    # _reviews: "Only sampled, surfaced reviews live here" (02:464) and the
    # §7 public list names "sampled reviews"; no private marks in the §4.8
    # column list besides `origin` (§1.3). The hidden full rating stream lives
    # in `_job_outcomes` (§5), never here.
    Field("_reviews", "id"): V.PUBLIC_SQL,
    Field("_reviews", "job_id"): V.PUBLIC_SQL,
    Field("_reviews", "appointment_id"): V.PUBLIC_SQL,
    Field("_reviews", "customer_id"): V.PUBLIC_SQL,
    Field("_reviews", "technician_id"): V.PUBLIC_SQL,
    Field("_reviews", "published_at"): V.PUBLIC_SQL,
    Field("_reviews", "service_date"): V.PUBLIC_SQL,
    Field("_reviews", "stars"): V.PUBLIC_SQL,
    Field("_reviews", "source"): V.PUBLIC_SQL,
    Field("_reviews", "created_at"): V.PUBLIC_SQL,
    Field("_reviews", "origin"): V.PRIVATE_ENGINE,

    # _notifications: "an alert layer over authoritative operational tables,
    # never the source of truth" (02:467); the §7 public list names
    # "notifications" — every column rides the public `notifications` view.
    Field("_notifications", "id"): V.PUBLIC_SQL,
    Field("_notifications", "created_at"): V.PUBLIC_SQL,
    Field("_notifications", "service_date"): V.PUBLIC_SQL,
    Field("_notifications", "notification_type"): V.PUBLIC_SQL,
    Field("_notifications", "entity_type"): V.PUBLIC_SQL,
    Field("_notifications", "entity_id"): V.PUBLIC_SQL,
    Field("_notifications", "message"): V.PUBLIC_SQL,
}

#: Read-only view of the registry (table, column) -> class, for callers that
#: need to enumerate it (e.g. the completeness test). A genuine proxy, not a
#: copy, so it cannot drift from what `classify` reads (matches the
#: `MappingProxyType` convention in config.py's `RunConfig.resolved`).
FIELD_REGISTRY: Mapping[Field, VisibilityClass] = MappingProxyType(_REGISTRY)


def classify(field: Field | tuple[str, str]) -> VisibilityClass:
    """Look up the registered :class:`VisibilityClass` for ``(table, column)``.

    Accepts a :class:`Field` or any ``(table, column)`` pair. Raises
    :class:`EngineError` with :data:`ErrorCode.UNCLASSIFIED_FIELD` if the field
    has no registry entry — spec 01:283: "an unclassified field fails the
    build," not a silent default. This is a build/registry-completeness
    failure (like :data:`ErrorCode.UNKNOWN_CONFIG_KEY` in ``config.py``), never
    a value that reaches a public surface, so the message may name the field.
    """
    key = field if isinstance(field, Field) else Field(*field)
    try:
        return _REGISTRY[key]
    except KeyError:
        raise EngineError(
            ErrorCode.UNCLASSIFIED_FIELD,
            f"no visibility classification registered for {key}",
        ) from None


def assert_public(view: str, columns: Iterable[str]) -> None:
    """Reject any of ``view``'s ``columns`` that do not classify as public
    (spec 01 §Interfaces: ``Visibility ... assert_public(view, columns)``).

    A column with no registry entry propagates :func:`classify`'s
    :data:`ErrorCode.UNCLASSIFIED_FIELD` (build failure). A column that *is*
    classified but resolves to a private class (``private_engine`` /
    ``post_run_only``) raises the uniform :data:`ErrorCode.UNAUTHORIZED` used
    for every other privileged/hidden probe — the caller cannot tell from the
    error which column or table failed (01:281: "Errors for a hidden table and
    for a nonexistent table Must be indistinguishable").

    CSR-001 approximation: any class in :meth:`VisibilityClass.public` passes,
    not just ``public_sql`` for a view or ``public_sdk`` for an SDK response,
    because this ticket ships no per-surface view/SDK registry to check
    ``view`` against — the parameter is really a base-table name here. A
    tighter per-surface acceptance set, and keying ``view`` on real view names
    once CSR-002's view registry exists, land with CSR-012's full VIS corpus
    (docs/CSR-001.md:88).
    """
    for column in columns:
        if classify(Field(view, column)) not in VisibilityClass.public():
            raise EngineError.unauthorized()


__all__ = ["FIELD_REGISTRY", "Field", "assert_public", "classify"]
