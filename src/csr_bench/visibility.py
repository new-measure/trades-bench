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

Registry completeness (this ticket): every column of every table
``db.py`` creates in CSR-001 is classified below — the core ``run`` record and
the six underscore-prefixed engine tables it owns
(``_events``, ``_ledger``, ``_invocations``, ``_command_responses``,
``_rng_state``, ``_rng_draw_audit``), plus the internal ``_schema_meta``
table. No public view exists yet (``db.py`` §module docstring: "whitelisted
public views come in CSR-002"), so most fields are conservatively
``private_engine`` here; the full VIS corpus — whitelisted view definitions,
SDK/prompt/notification sweeps, and the adversarial SQL corpus — lands with
CSR-012 (docs/CSR-001.md acceptance test 8).
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
# Registry: every column of every table `db.py` creates in this ticket.
# Non-obvious choices carry a comment citing the spec line that justifies them;
# everything else defaults to `private_engine` because no public surface (view,
# SDK response, prompt template, notification) ships in CSR-001 to carry it.
# ---------------------------------------------------------------------------

_REGISTRY: dict[Field, VisibilityClass] = {
    # -- run: identity, calendar, status, result-identification (01:18) -------
    # Reported post-run for result labeling / trajectory tracking, never on a
    # live agent-facing surface (spec 08 §8.4 "Every run artifact Must record":
    # spec_version, config_version, config_hash, master_seed — 08:767; harness
    # also records run_id per completed invocation — 08:705).
    Field("run", "run_id"): V.POST_RUN_ONLY,
    Field("run", "spec_version"): V.POST_RUN_ONLY,
    Field("run", "config_version"): V.POST_RUN_ONLY,
    Field("run", "config_hash"): V.POST_RUN_ONLY,
    # The raw resolved config blob (not just its hash) can carry domain
    # tunables that are private_engine on their own (e.g. acceptance curves,
    # 01:282); only the hash is on the reporting list (08:767).
    Field("run", "config_json"): V.PRIVATE_ENGINE,
    # Listed in the post-run "run artifact" record (08:767) for reproducibility
    # / result labeling, but must never leave the engine while the run is live
    # or a replay attack could predict draws — post_run_only captures exactly
    # that "hidden during, disclosed after" shape.
    Field("run", "master_seed"): V.POST_RUN_ONLY,
    # Fixed calendar constants (docs/CSR-001.md decision 3: same run calendar
    # and timezone every run), known to the agent out-of-band. No public
    # surface ships for these in CSR-001; classification deferred to CSR-012's
    # full VIS corpus (docs/CSR-001.md:88).
    Field("run", "start_at_ms"): V.PRIVATE_ENGINE,
    Field("run", "first_service_date"): V.PRIVATE_ENGINE,
    Field("run", "last_service_date"): V.PRIVATE_ENGINE,
    Field("run", "timezone"): V.PRIVATE_ENGINE,
    # Projected as `run_status` by the public `company_state` view
    # (specs/02-data-and-initial-state.md:480) and re-stated field by field by
    # `finance.get_cash_position()` (specs/08-agent-interface-and-harness.md:
    # 250) — a live public-SQL surface, not merely a post-run reporting field.
    Field("run", "status"): V.PUBLIC_SQL,
    # Also stated verbatim in prompts (07:117, 07:153), but the frozen
    # simulated time leaves the engine through two public non-prompt channels
    # too: projected as `as_of_ms` by the public `company_state` view
    # (specs/02-data-and-initial-state.md:477) and required on `meta.as_of`
    # for every SDK/HTTP response envelope (specs/08-agent-interface-and-
    # harness.md:133) — so this is public_sql, not prompt-exclusive.
    Field("run", "current_time_ms"): V.PUBLIC_SQL,
    # "These fields exist for reporting only" (01:45), verbatim.
    Field("run", "model_id"): V.POST_RUN_ONLY,
    Field("run", "harness_name"): V.POST_RUN_ONLY,
    Field("run", "harness_version"): V.POST_RUN_ONLY,
    Field("run", "context_policy"): V.POST_RUN_ONLY,
    Field("run", "system_prompt_hash"): V.POST_RUN_ONLY,
    # -- _events: the world-event envelope (queue machinery, not a domain
    # record) -------------------------------------------------------------
    # "At minimum the following are private_engine: future calls, queue size
    # and order; ... pending cancellation events" (01:282); "no future call,
    # queue length, or unhappened row is observable" (08:783). db.py's own
    # module docstring names `_events` as hidden engine state (db.py:8-10).
    # Domain modules (CSR-005+) surface *derived* operational tables/views
    # over applied effects; the raw envelope row itself never does.
    Field("_events", "event_id"): V.PRIVATE_ENGINE,
    Field("_events", "run_id"): V.PRIVATE_ENGINE,
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
    # -- _ledger: append-only money log --------------------------------------
    # db.py names `_ledger` hidden engine state (db.py:8-10). CSR-001's
    # precursor table has no public view yet; the full ledger becomes
    # public_sql in CSR-002 as `ledger_entries`
    # (specs/02-data-and-initial-state.md:594), over the differently-shaped
    # `_ledger_entries` columns (02:432), not this table's. No public
    # financial surface ships in CSR-001 (money decision 4, docs/CSR-001.md:
    # "CSR-010 extends" the reporting side).
    Field("_ledger", "seq"): V.PRIVATE_ENGINE,
    Field("_ledger", "run_id"): V.PRIVATE_ENGINE,
    Field("_ledger", "amount_cents"): V.PRIVATE_ENGINE,
    Field("_ledger", "category"): V.PRIVATE_ENGINE,
    Field("_ledger", "entity_type"): V.PRIVATE_ENGINE,
    Field("_ledger", "entity_id"): V.PRIVATE_ENGINE,
    Field("_ledger", "simulated_ts_ms"): V.PRIVATE_ENGINE,
    # -- _invocations: type, timestamp, sequence, status, prompt version,
    # attempt count (01:23, 01:228) -----------------------------------------
    # The harness records (run_id, invocation_id, kind, ...) after every
    # completed invocation for trajectory tracking (08:705) — a post-run
    # concern, not something the row itself hands the agent live.
    Field("_invocations", "invocation_id"): V.POST_RUN_ONLY,
    Field("_invocations", "run_id"): V.POST_RUN_ONLY,
    Field("_invocations", "type"): V.POST_RUN_ONLY,
    # The invocation's own timestamp is exactly what its rendered prompt
    # states (07:117, 07:153), but it is also the frozen simulated time
    # projected as `as_of_ms`/`meta.as_of` (specs/02-data-and-initial-
    # state.md:477, specs/08-agent-interface-and-harness.md:133) — public,
    # not prompt-exclusive.
    Field("_invocations", "ts_ms"): V.PUBLIC_SQL,
    Field("_invocations", "sequence"): V.POST_RUN_ONLY,
    Field("_invocations", "status"): V.POST_RUN_ONLY,
    # Reporting-only, same shape as `system_prompt_hash` (01:45).
    Field("_invocations", "prompt_version"): V.POST_RUN_ONLY,
    # 07:162 only establishes that reminder wording differs by attempt, not
    # that the count itself is disclosed anywhere; grouped with the other
    # `_invocations` bookkeeping columns (post_run_only) until CSR-012 settles
    # whether this maps onto the public `call_attempts` view (02:200, 02:594).
    Field("_invocations", "attempt_count"): V.POST_RUN_ONLY,
    # -- _command_responses: idempotency cache (public AND privileged) ------
    # Stores raw response bytes for both public and controller-privileged
    # requests in the same table; leaking any column (even `namespace`) could
    # let a probe tell a real privileged target from a nonexistent one,
    # breaking the indistinguishability rule (01:281). Ticket decision (per
    # chunk instructions).
    Field("_command_responses", "run_id"): V.PRIVATE_ENGINE,
    Field("_command_responses", "request_id"): V.PRIVATE_ENGINE,
    Field("_command_responses", "namespace"): V.PRIVATE_ENGINE,
    Field("_command_responses", "response"): V.PRIVATE_ENGINE,
    Field("_command_responses", "created_at_ms"): V.PRIVATE_ENGINE,
    # -- _rng_state: sequential-stream state ---------------------------------
    # "RNG stream states" is explicitly private_engine, verbatim (01:282).
    Field("_rng_state", "run_id"): V.PRIVATE_ENGINE,
    Field("_rng_state", "stream_name"): V.PRIVATE_ENGINE,
    Field("_rng_state", "seed"): V.PRIVATE_ENGINE,
    Field("_rng_state", "counter"): V.PRIVATE_ENGINE,
    # -- _rng_draw_audit: consumed-draw audit log ----------------------------
    # Ticket decision (per chunk instructions): private_engine for CSR-001.
    # Note the tension with visibility.md's own `post_run_only` definition
    # ("full private database, random audit, and trajectory, for post-run
    # analysis" — 01:275), which reads as a good fit for this exact table;
    # the split between "stays engine-internal forever" and "surfaces after
    # the run" is exactly what CSR-012's full VIS corpus resolves. `db.py`
    # already keeps the audit row itself minimal — never the u64 result or
    # the seed (db.py:118-119) — regardless of this class.
    Field("_rng_draw_audit", "seq"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "run_id"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "stream_name"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "entity_type"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "entity_id"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "scope"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "event_kind"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "draw_index"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "outcome_id"): V.PRIVATE_ENGINE,
    Field("_rng_draw_audit", "simulated_ts_ms"): V.PRIVATE_ENGINE,
    # -- _schema_meta: internal schema-version bookkeeping (db.py:135-138) --
    # Not part of any spec 01 owned-state record; underscore-prefixed engine
    # table by convention (01:280).
    Field("_schema_meta", "key"): V.PRIVATE_ENGINE,
    Field("_schema_meta", "value"): V.PRIVATE_ENGINE,
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
