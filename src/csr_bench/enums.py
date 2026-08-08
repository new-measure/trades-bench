"""Closed enumerations known for spec v1.

Every closed list the engine knows today lives here (statuses, visibility
classes, invocation lifecycle, RNG stream-name set, the event-kind registry,
ledger categories, and the entity-type discriminator used by keyed draws). The
run-status, event-status, invocation-type/status, visibility-class, and RNG
stream-name sets are fully declared now; the event-kind registry ships only the
clearly-marked ``contract_test_*`` kinds (retired by CSR-005+).

Adding any member is a :data:`csr_bench.SPEC_VERSION` change.

Members are string enums so they round-trip through SQLite and JSON without a
custom serialization layer, while still being a closed set at the API boundary.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    #: Configuration resolved and warm-start loading; no invocation runs.
    INITIALIZING = "initializing"
    #: Normal scored state.
    ACTIVE = "active"
    #: Final settlement after the last service day committed; score is the cash.
    COMPLETED = "completed"
    #: Cash went below zero in a committed transaction; score is zero.
    BANKRUPT = "bankrupt"
    #: Unrecoverable engine/adapter failure; the run is invalid and unscored.
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> frozenset[RunStatus]:
        return frozenset({cls.COMPLETED, cls.BANKRUPT, cls.FAILED})


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class InvocationType(StrEnum):
    OPENING = "opening"
    CALL = "call"
    CLOSING = "closing"


class InvocationStatus(StrEnum):
    #: Opened and active; world time frozen at the invocation timestamp.
    ACTIVE = "active"
    #: Completed through a terminal transition (controller side).
    COMPLETED = "completed"


class VisibilityClass(StrEnum):
    #: Readable through whitelisted public views (agent SQL).
    PUBLIC_SQL = "public_sql"
    #: Returned in SDK / CLI responses.
    PUBLIC_SDK = "public_sdk"
    #: Appears only in rendered prompts.
    PROMPT_ONLY = "prompt_only"
    #: Appears in notification rows.
    NOTIFICATION = "notification"
    #: Never leaves the engine during a run.
    PRIVATE_ENGINE = "private_engine"
    #: Full private database / audit / trajectory, post-run analysis only.
    POST_RUN_ONLY = "post_run_only"

    @classmethod
    def public(cls) -> frozenset[VisibilityClass]:
        return frozenset(
            {
                cls.PUBLIC_SQL,
                cls.PUBLIC_SDK,
                cls.PROMPT_ONLY,
                cls.NOTIFICATION,
            }
        )


class LedgerCategory(StrEnum):
    #: The calibrated opening-balance adjustment that sets scored starting cash
    #: while preserving historical financial records (00-decisions §Warm start).
    OPENING_BALANCE_ADJUSTMENT = "opening_balance_adjustment"
    # -- contract-test categories (retired by CSR-010+; clearly marked) ---------
    #: synthetic debit used by the determinism / integrity driver.
    CONTRACT_TEST_DEBIT = "contract_test_debit"
    #: synthetic credit used by the determinism / integrity driver.
    CONTRACT_TEST_CREDIT = "contract_test_credit"


class EntityType(StrEnum):
    """Discriminator that disambiguates colliding numeric IDs across tables in
    keyed draws and the event envelope (spec 01 §Entity-keyed draws).

    Domain tickets (CSR-002+) register their entity types here; for CSR-001 only
    the contract-test discriminator exists.
    """

    # -- contract-test (retired by CSR-005+) -----------------------------------
    CONTRACT_TEST_ENTITY = "contract_test_entity"
    CONTRACT_TEST_ENTITY_ALT = "contract_test_entity_alt"


class EventKind(StrEnum):
    """Central event-kind registry (spec 01 §Event envelope and ordering).

    CSR-001 ships only the clearly-marked ``contract_test_*`` kinds and the
    machinery that applies them. Domain kinds are added here by their owning
    tickets; nothing in this ticket branches on a domain kind.
    """

    # -- contract-test (retired by CSR-005+; clearly marked) --------------------
    #: A deterministic toy world event used by the scripted determinism driver.
    CONTRACT_TEST_EVENT = "contract_test_event"
    #: Synthetic unavoidable settlement debit (exercises money/bankruptcy).
    CONTRACT_TEST_SETTLE_DEBIT = "contract_test_settle_debit"
    #: Synthetic unavoidable settlement credit (exercises money/bankruptcy).
    CONTRACT_TEST_SETTLE_CREDIT = "contract_test_settle_credit"


# ---------------------------------------------------------------------------
# Deterministic randomness — named streams (spec 01 §Deterministic randomness)
# ---------------------------------------------------------------------------


class SequentialStreamName(StrEnum):
    """Stateful streams that evolve in simulated-time order and Must not depend
    on agent behavior. Stream states are persisted in ``_rng_state`` inside the
    advancing transaction.
    """

    WEATHER_PROCESS = "weather_process"
    LABOR_ECONOMY = "labor_economy"
    COMPETITOR_BACKGROUND = "competitor_background"
    WARM_START = "warm_start"


class KeyedStreamName(StrEnum):
    """Keyed (order-independent) streams: a seed, nothing consumed. Registered
    centrally here; every domain spec registers its names in this table and Must
    not invent one locally. Adding a name is a spec-version change.
    """

    WEATHER_FORECAST = "weather_forecast"  # 05 §11
    DEMAND = "demand"  # 05 §11
    ACCEPTANCE = "acceptance"  # 05 §11
    CANCELLATION = "cancellation"  # 05 §11
    COMPETITOR_LEAK = "competitor_leak"  # 05 §11 / 06 §10 (shared)
    TRAVEL_NOISE = "travel_noise"  # 03 §5.2
    WORKFORCE_HEALTH = "workforce_health"  # 04 §10
    WORKFORCE_ATTRITION = "workforce_attrition"  # 04 §10
    HIRING = "hiring"  # 04 §10
    TRAINING = "training"  # 04 §10
    TECHNICIAN_QUALITY = "technician_quality"  # 04 §10
    JOB_EXECUTION = "job_execution"  # 06 §10
    JOB_QUALITY = "job_quality"  # 06 §10
    JOB_FINANCE = "job_finance"  # 06 §10
    CALLBACKS = "callbacks"  # 06 §10
    REVIEWS = "reviews"  # 06 §10


#: The complete stream-name registry: 4 sequential + 16 keyed (spec 01 tables).
ALL_STREAM_NAMES: frozenset[str] = frozenset(
    str(s) for s in (*SequentialStreamName, *KeyedStreamName)
)

#: Count assertions kept near the tables so a future edit that adds a stream
#: also trips a cheap invariant test here (it must also bump SPEC_VERSION).
SEQUENTIAL_STREAM_COUNT = 4
KEYED_STREAM_COUNT = 16
