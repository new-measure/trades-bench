"""Deterministic randomness (spec 01 §Deterministic randomness; decision 1).

No numpy, no Python ``hash()``, no SQLite ``RANDOM()``, no wall clock, no UUID
randomness, no container-iteration-order dependence, no shared global generator.

Stream seeds are SHA-256 over a canonical length-prefixed serialization of
``(master_seed, spec_version, stream_name)``:

    stream_seed = SHA-256( u64(master_seed)
                           || len(spec_version) || spec_version
                           || len(stream_name) || stream_name )

Sequential draws: first 8 bytes of ``SHA-256(stream_seed || u64(counter))`` as a
big-endian uint64. State is exactly ``(seed, counter)``, persisted in
``_rng_state`` inside the same transaction that advances it — so a rollback
discards the advance and a fresh process resumes from the last commit (DET-02).

Keyed draws: ``SHA-256`` over ``stream_seed || entity_type || entity_id || scope
|| event_kind || draw_index`` (all length-prefixed / fixed-width). A pure
function — order-independent, nothing consumed from a stream, identical keys
always yield identical outcomes within a run.

Probability comparisons: ``u < floor(p × 2^64)`` with integer math only. The
threshold is computed once at config resolution (:mod:`csr_bench.config`); this
module only performs the comparison.

Helpers built on the u64 draw: ``chance(threshold)``, ``choice(n) = u % n``,
``randint(a, b) = a + u % (b−a+1)``, ``bounded_int``, ``triangular`` via summed
remainders mapped to ``[-15, 0, +15]``, and ``uniform01``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from decimal import Decimal, localcontext

from .constants import TIMING_VARIATION_HIGH, TIMING_VARIATION_LOW
from .enums import KeyedStreamName, SequentialStreamName
from .errors import EngineError, ErrorCode

U64_MOD = 1 << 64

#: Precision for ``Decimal(u) / Decimal(2**64)`` in :func:`uniform01`, applied
#: via a local context so the result depends only on ``u`` — never on the
#: ambient thread's Decimal context (which importing this module, or any other
#: code, may have changed; see docs/CSR-001.md chunk-6 audit item 1). 64
#: significant digits makes the division exact for every u in [0, 2**64): since
#: 10**64 == 2**64 * 5**64, u / 2**64 == u * 5**64 / 10**64 terminates, and
#: u * 5**64 < 10**64 has at most 64 digits.
_UNIFORM01_PREC = 64

#: A recorder for a consumed draw key: ``(stream_name, entity_type, entity_id,
#: scope, event_kind, draw_index, outcome_id)``. Records the *key* only — never
#: the u64 result or the seed, never the secrets needed to predict future draws
#: (spec 01 §Audit).
AuditRecorder = Callable[..., None]


def _require_pos_entity_id(entity_id: object) -> None:
    if isinstance(entity_id, bool) or not isinstance(entity_id, int):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"entity_id must be a positive int, got ({type(entity_id).__name__}) {entity_id!r}",
        )
    if not (0 < entity_id < (1 << 63)):
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            f"entity_id must be a positive signed i64, got {entity_id}",
        )


def _require_str(name: str, value: object) -> str:
    if isinstance(value, str):
        return value
    # accept StrEnum members (they are str subclasses)
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        return value.value  # type: ignore[union-attr]
    raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be a string, got {value!r}")


def _require_master_seed(master_seed: object) -> int:
    if isinstance(master_seed, bool) or not isinstance(master_seed, int):
        raise ValueError(f"master_seed must be an int, got {type(master_seed).__name__}")
    if not (0 <= master_seed < U64_MOD):
        raise ValueError(f"master_seed must fit u64, got {master_seed}")
    return master_seed


# ---------------------------------------------------------------------------
# Canonical encoders (length-prefixed, unambiguous)
# ---------------------------------------------------------------------------


def _lp_str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        raise EngineError(ErrorCode.INVALID_ARGUMENT, "string too long for RNG key")
    return len(b).to_bytes(2, "big") + b


def _u8(n: int) -> bytes:
    return n.to_bytes(8, "big")


def _u8_counter(counter: int) -> bytes:
    if not isinstance(counter, int) or not (0 <= counter < U64_MOD):
        raise EngineError(ErrorCode.INVALID_ARGUMENT, f"counter must fit u64, got {counter}")
    return counter.to_bytes(8, "big")


# ---------------------------------------------------------------------------
# Pure primitives — no DB, no audit, fully testable for determinism
# ---------------------------------------------------------------------------


def stream_seed(master_seed: int, spec_version: str, stream_name: str) -> bytes:
    """The 32-byte SHA-256 stream seed from ``(master_seed, spec_version, name)``."""
    body = _u8(master_seed) + _lp_str(spec_version) + _lp_str(stream_name)
    return hashlib.sha256(body).digest()


def sequential_draw(stream_seed_bytes: bytes, counter: int) -> int:
    """Sequential-stream u64: first 8 bytes of ``SHA-256(seed || counter)``."""
    h = hashlib.sha256(stream_seed_bytes + _u8_counter(counter)).digest()
    return int.from_bytes(h[:8], "big")


def keyed_draw(
    stream_seed_bytes: bytes,
    entity_type: str,
    entity_id: int,
    scope: str,
    event_kind: str,
    draw_index: int,
) -> int:
    """Keyed (order-independent) u64: SHA-256 over the canonical key tuple.

    Pure function — same key always yields the same uint64 within a run,
    regardless of how many other draws happened in between.
    """
    _require_pos_entity_id(entity_id)
    if not isinstance(draw_index, int) or draw_index < 0 or draw_index >= U64_MOD:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT, f"draw_index must be a u64 >= 0, got {draw_index}"
        )
    body = (
        stream_seed_bytes
        + _lp_str(entity_type)
        + _u8(entity_id)
        + _lp_str(scope)
        + _lp_str(event_kind)
        + _u8_counter(draw_index)
    )
    h = hashlib.sha256(body).digest()
    return int.from_bytes(h[:8], "big")


# ---------------------------------------------------------------------------
# Helpers built on a u64 draw (decision 1)
# ---------------------------------------------------------------------------


def chance(u: int, threshold: int) -> bool:
    """Probability comparison: ``u < floor(p × 2^64)`` as integer math only."""
    return u < threshold


def choice(u: int, n: int) -> int:
    """``u % n`` exactly as specified (decision 1)."""
    if not isinstance(n, int) or n <= 0:
        raise EngineError(ErrorCode.STATE_ILLEGAL, f"choice(n) needs n>0, got {n}")
    return u % n


def randint(u: int, a: int, b: int) -> int:
    """``a + u % (b − a + 1)`` (inclusive ``[a, b]``)."""
    if not isinstance(a, int) or not isinstance(b, int) or a > b:
        raise EngineError(ErrorCode.STATE_ILLEGAL, f"randint needs a<=b, got [{a},{b}]")
    return a + (u % (b - a + 1))


def bounded_int(u: int, lo: int, hi: int) -> int:
    """Inclusive ``[lo, hi]`` integer draw."""
    return randint(u, lo, hi)


def triangular(
    u1: int, u2: int, *, low: int = TIMING_VARIATION_LOW, high: int = TIMING_VARIATION_HIGH
) -> int:
    """Symmetric triangular over ``[low, high]`` with mode at the midpoint.

    Implemented as the sum of two uniform remainders (one per draw), the classic
    discrete triangular: sum of two ``U[0, n-1]`` is triangular over
    ``[0, 2n-2]`` with mode ``n-1``; shift and recenter to ``[low, high]``.
    For the user-decided constant ``triangular(-15, 0, +15)`` this yields a
    value in ``[-15, +15]`` peaking at ``0``.
    """
    if (low + high) % 2 != 0:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL, f"triangular needs symmetric [low,high], got [{low},{high}]"
        )
    mid = (low + high) // 2
    n = mid - low + 1  # values per uniform half; symmetric => high - mid + 1
    s = (u1 % n) + (u2 % n)  # support [0, 2n-2], mode n-1
    return s - (n - 1) + mid


def uniform01(u: int) -> Decimal:
    """``u / 2^64`` as an exact Decimal in ``[0, 1)`` (no float).

    Computed in a ``localcontext`` pinned to :data:`_UNIFORM01_PREC`, so the
    result is a pure function of ``u`` alone, independent of the caller's
    ambient Decimal context.
    """
    with localcontext() as ctx:
        ctx.prec = _UNIFORM01_PREC
        return Decimal(u) / Decimal(U64_MOD)


# ---------------------------------------------------------------------------
# Rng — orchestrator-facing façade (txn-guarded, audit-on-draw)
# ---------------------------------------------------------------------------


class SequentialStream:
    """An orchestrator-scoped sequential stream: ``(stream_seed, counter)``.

    Each :meth:`next` advances the counter and — when the owning :class:`Rng` is
    bound to a run database — upserts ``(seed, counter)`` into ``_rng_state``
    inside the same open transaction that pulls the draw, so a rollback discards
    the advance and a resume reproduces the stream (spec 01 §Audit).
    """

    __slots__ = ("name", "seed", "counter", "_rng")

    def __init__(self, name: str, seed: bytes, rng: Rng) -> None:
        self.name = name
        self.seed = seed
        self.counter = 0
        self._rng = rng

    def __iter__(self) -> Iterator[int]:
        while True:
            yield self.next()

    def next(self) -> int:
        self._rng._require_advance_txn(self.name)
        # The persisted row, not the in-memory counter, is the authority for a
        # bound Rng: after a rollback the in-memory counter is one ahead of what
        # any fresh reader sees, and this draw must repeat the discarded one.
        self._rng._resync(self)
        u = sequential_draw(self.seed, self.counter)
        self._rng._audit_draw(self.name, "run", 1, self.name, "sequential", self.counter)
        self.counter += 1
        self._rng._persist(self)
        return u

    def state(self) -> tuple[str, int]:
        # The persisted row, not the in-memory counter, is authoritative for a
        # bound Rng — resync first so a post-rollback read never reports the
        # phantom (unpersisted) counter next() leaves behind (spec 01 §Audit).
        self._rng._resync(self)
        return self.seed.hex(), self.counter

    def restore(self, seed_hex: str, counter: int) -> None:
        self.seed = bytes.fromhex(seed_hex)  # raises ValueError on bad hex
        self.counter = counter


class Rng:
    """Engine RNG service (spec 01 §Interfaces).

    Keyed draws are callable **only inside an open transaction** so a rollback
    discards the audit row (and any scheduled event that used it). Sequential
    streams are orchestrator-scoped. Nothing about a keyed draw depends on draw
    order or on agent behavior.

    Passing ``conn`` binds the sequential streams to the run database's
    ``_rng_state`` rows: counters are adopted from the last commit at
    construction and written back inside the advancing transaction (CSR-002
    decision 4: one run per database, so there is no run_id filter — the
    ``stream_name`` primary key addresses the row). Unbound (``conn``
    omitted), sequential state lives in memory only — useful for the pure
    primitives, never for a real run.
    """

    __slots__ = (
        "master_seed",
        "spec_version",
        "_seq",
        "_streams",
        "_audit",
        "_txn_open",
        "_conn",
    )

    def __init__(
        self,
        master_seed: int,
        spec_version: str,
        *,
        audit_recorder: AuditRecorder | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.master_seed = _require_master_seed(master_seed)
        self.spec_version = str(spec_version)
        self._seq: dict[str, tuple[str, int]] = {}  # name -> (seed_hex, counter)
        self._streams: dict[str, SequentialStream] = {}
        self._audit: AuditRecorder | None = audit_recorder
        self._txn_open = False
        self._conn = conn
        if conn is not None:
            self._adopt_persisted()

    # --- transaction binding ------------------------------------------------
    def enter_txn(self) -> None:
        if self._txn_open:
            raise EngineError(ErrorCode.INVARIANT_VIOLATED, "Rng already bound to a txn")
        self._txn_open = True

    def exit_txn(self) -> None:
        if not self._txn_open:
            raise EngineError(ErrorCode.INVARIANT_VIOLATED, "Rng not bound to a txn")
        self._txn_open = False

    def is_txn_open(self) -> bool:
        return self._txn_open

    def _require_txn(self, what: str = "draw") -> None:
        if not self._txn_open:
            raise EngineError(
                ErrorCode.DRAW_OUTSIDE_TXN,
                f"{what} is callable only inside an open transaction",
            )

    def _require_state_write_txn(self, what: str) -> None:
        """Guard for any write to ``_rng_state``.

        Requires the engine-level txn flag (``enter_txn``/``exit_txn``) plus,
        when bound to a database, the *database* transaction itself — otherwise
        the ``_rng_state`` write would autocommit and survive a rollback of the
        operation that produced it. Shared by the sequential-advance path and
        ``restore_seq_state``: both write ``_rng_state``.
        """
        self._require_txn(what)
        if self._conn is not None and not self._conn.in_transaction:
            raise EngineError(
                ErrorCode.DRAW_OUTSIDE_TXN,
                f"{what} is callable only inside the database transaction "
                "that persists _rng_state",
            )

    def _require_advance_txn(self, name: str) -> None:
        """Guard for advancing a sequential stream — see ``_require_state_write_txn``."""
        self._require_state_write_txn(f"sequential draw [{name}]")

    # --- seeds -------------------------------------------------------------
    def stream_seed(self, stream_name: str) -> bytes:
        seed = stream_seed(self.master_seed, self.spec_version, stream_name)
        if stream_name not in {s.value for s in KeyedStreamName} and stream_name not in {
            s.value for s in SequentialStreamName
        }:
            raise EngineError(ErrorCode.STATE_ILLEGAL, f"unknown stream name {stream_name!r}")
        return seed

    # --- keyed draws -------------------------------------------------------
    def draw(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        draw_index: int,
        *,
        outcome_id: str | None = None,
    ) -> int:
        self._require_txn(f"keyed draw [{stream_name}]")
        if stream_name not in {s.value for s in KeyedStreamName}:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"keyed draw requires a keyed stream name, got {stream_name!r}",
            )
        stream_seed_bytes = stream_seed(self.master_seed, self.spec_version, stream_name)
        u = keyed_draw(
            stream_seed_bytes,
            _require_str("entity_type", entity_type),
            entity_id,
            _require_str("scope", scope),
            _require_str("event_kind", event_kind),
            draw_index,
        )
        self._audit_draw(
            stream_name, entity_type, entity_id, scope, event_kind, draw_index, outcome_id
        )
        return u

    # --- helper draws (built on draw) ------------------------------------
    def chance(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        threshold: int,
        draw_index: int = 0,
    ) -> bool:
        u = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index)
        return chance(u, threshold)

    def choice(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        n: int,
        draw_index: int = 0,
    ) -> int:
        u = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index)
        return choice(u, n)

    def randint(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        a: int,
        b: int,
        draw_index: int = 0,
    ) -> int:
        u = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index)
        return randint(u, a, b)

    def bounded_int(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        scope_event_kind: str,
        lo: int,
        hi: int,
        draw_index: int = 0,
    ) -> int:
        u = self.draw(stream_name, entity_type, entity_id, scope, scope_event_kind, draw_index)
        return bounded_int(u, lo, hi)

    def triangular(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        draw_index: int = 0,
        *,
        low: int = TIMING_VARIATION_LOW,
        high: int = TIMING_VARIATION_HIGH,
    ) -> int:
        u1 = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index)
        u2 = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index + 1)
        return triangular(u1, u2, low=low, high=high)

    def uniform01(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        draw_index: int = 0,
    ):
        u = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index)
        return uniform01(u)

    # --- sequential streams ------------------------------------------------
    def stream(self, name: str) -> SequentialStream:
        if name not in {s.value for s in SequentialStreamName}:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"sequential stream requires a sequential name, got {name!r}",
            )
        if name not in self._streams:
            seed = stream_seed(self.master_seed, self.spec_version, name)
            st = SequentialStream(name, seed, self)
            self._streams[name] = st
            self._seq[name] = st.state()  # resyncs the counter from _rng_state when bound
        return self._streams[name]

    def seq_state(self) -> dict[str, tuple[str, int]]:
        """Snapshot of sequential-stream counters as ``_rng_state`` holds them."""
        for name, st in self._streams.items():
            self._seq[name] = st.state()  # resyncs before reporting
        return {name: self._seq[name] for name in sorted(self._seq)}

    def restore_seq_state(self, state: dict[str, tuple[str, int]]) -> None:
        """Adopt a snapshot; a bound Rng writes it through to ``_rng_state``
        (the persisted row is what later draws read).

        Guarded exactly like advancing a stream: this writes ``_rng_state`` and
        must not autocommit outside the caller's transaction, or a rollback of
        the restoring operation could not undo it (spec 01 §Audit).
        """
        self._require_state_write_txn("restore_seq_state")
        for name in sorted(state):  # sorted: never depend on dict order
            seed_hex, counter = state[name]
            st = self.stream(name)
            expected = stream_seed(self.master_seed, self.spec_version, name).hex()
            if seed_hex != expected:
                # Mirrors the check _load_counter makes on read: a restored
                # seed from a different master_seed must not be adopted, or a
                # later fresh Rng over this run becomes unresumable.
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"restored seed for stream {name!r} does not match this run's master seed",
                )
            st.restore(seed_hex, counter)
            self._persist(st)  # upserts _rng_state and refreshes self._seq[name]

    # --- _rng_state persistence (spec 01 §Audit) ----------------------------
    def _adopt_persisted(self) -> None:
        """Instantiate the streams the run has already committed state for.

        One query total (``stream_name, seed, counter``) rather than one query
        per row plus one per counter load. No run_id: the run is one file and
        ``_rng_state`` holds exactly that run's rows (CSR-002 decision 4).
        """
        conn = self._conn
        if conn is None:
            return
        rows = conn.execute(
            "SELECT stream_name, seed, counter FROM _rng_state ORDER BY stream_name"
        ).fetchall()
        for name, seed_hex, counter in rows:
            if name not in {s.value for s in SequentialStreamName}:
                raise EngineError(
                    ErrorCode.STATE_ILLEGAL,
                    f"sequential stream requires a sequential name, got {name!r}",
                )
            seed = stream_seed(self.master_seed, self.spec_version, name)
            if seed_hex != seed.hex():
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"persisted seed for stream {name!r} does not match this run's master seed",
                )
            st = SequentialStream(name, seed, self)
            st.counter = counter
            self._streams[name] = st
            self._seq[name] = (seed_hex, counter)

    def _load_counter(self, name: str, seed: bytes) -> int:
        if self._conn is None:
            return 0
        row = self._conn.execute(
            "SELECT seed, counter FROM _rng_state WHERE stream_name=?", (name,)
        ).fetchone()
        if row is None:
            return 0
        if row[0] != seed.hex():
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"persisted seed for stream {name!r} does not match this run's master seed",
            )
        return int(row[1])

    def _resync(self, st: SequentialStream) -> None:
        """Reload ``st.counter`` from ``_rng_state`` when bound (rollback-safe)."""
        if self._conn is not None:
            st.counter = self._load_counter(st.name, st.seed)

    def _persist(self, st: SequentialStream) -> None:
        """Upsert ``(seed, counter)`` — the caller's transaction owns the write."""
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO _rng_state(stream_name, seed, counter) VALUES (?,?,?) "
            "ON CONFLICT(stream_name) DO UPDATE SET "
            "seed=excluded.seed, counter=excluded.counter",
            (st.name, st.seed.hex(), st.counter),
        )
        self._seq[st.name] = st.state()

    # --- audit -------------------------------------------------------------
    def _audit_draw(
        self,
        stream_name: str,
        entity_type: str,
        entity_id: int,
        scope: str,
        event_kind: str,
        draw_index: int,
        outcome_id: str | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit(
                stream_name=stream_name,
                entity_type=entity_type,
                entity_id=entity_id,
                scope=scope,
                event_kind=event_kind,
                draw_index=draw_index,
                outcome_id=outcome_id,
            )


__all__ = [
    "AuditRecorder",
    "Rng",
    "SequentialStream",
    "U64_MOD",
    "chance",
    "choice",
    "bounded_int",
    "keyed_draw",
    "randint",
    "sequential_draw",
    "stream_seed",
    "triangular",
    "uniform01",
]
