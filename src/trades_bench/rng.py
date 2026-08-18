from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from decimal import Decimal, localcontext

from . import clock
from .constants import TIMING_VARIATION_HIGH, TIMING_VARIATION_LOW
from .enums import KeyedStreamName, SequentialStreamName
from .errors import EngineError, ErrorCode

U64_MOD = 1 << 64

_UNIFORM01_PREC = 64

AuditRecorder = Callable[..., None]


def sqlite_audit_recorder(conn: sqlite3.Connection) -> AuditRecorder:

    def record(**kw: object) -> None:
        conn.execute(
            "INSERT INTO _random_audit(stream_name, entity_type, entity_id, "
            "scope, event_kind, draw_index, outcome_id, simulated_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                kw["stream_name"],
                kw["entity_type"],
                kw["entity_id"],
                kw["scope"],
                kw["event_kind"],
                kw["draw_index"],
                kw.get("outcome_id"),
                clock.now(conn),
            ),
        )

    return record


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
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        return value.value  # type: ignore[union-attr]
    raise EngineError(ErrorCode.INVALID_ARGUMENT, f"{name} must be a string, got {value!r}")


def _require_master_seed(master_seed: object) -> int:
    if isinstance(master_seed, bool) or not isinstance(master_seed, int):
        raise ValueError(f"master_seed must be an int, got {type(master_seed).__name__}")
    if not (0 <= master_seed < U64_MOD):
        raise ValueError(f"master_seed must fit u64, got {master_seed}")
    return master_seed


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


def stream_seed(master_seed: int, spec_version: str, stream_name: str) -> bytes:
    body = _u8(master_seed) + _lp_str(spec_version) + _lp_str(stream_name)
    return hashlib.sha256(body).digest()


def sequential_draw(stream_seed_bytes: bytes, counter: int) -> int:
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


def chance(u: int, threshold: int) -> bool:
    return u < threshold


def choice(u: int, n: int) -> int:
    if not isinstance(n, int) or n <= 0:
        raise EngineError(ErrorCode.STATE_ILLEGAL, f"choice(n) needs n>0, got {n}")
    return u % n


def randint(u: int, a: int, b: int) -> int:
    if not isinstance(a, int) or not isinstance(b, int) or a > b:
        raise EngineError(ErrorCode.STATE_ILLEGAL, f"randint needs a<=b, got [{a},{b}]")
    return a + (u % (b - a + 1))


def bounded_int(u: int, lo: int, hi: int) -> int:
    return randint(u, lo, hi)


def triangular(
    u1: int, u2: int, *, low: int = TIMING_VARIATION_LOW, high: int = TIMING_VARIATION_HIGH
) -> int:
    if (low + high) % 2 != 0:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL, f"triangular needs symmetric [low,high], got [{low},{high}]"
        )
    mid = (low + high) // 2
    n = mid - low + 1
    s = (u1 % n) + (u2 % n)
    return s - (n - 1) + mid


def inverse_cdf_variation(
    u: int, *, low: int = TIMING_VARIATION_LOW, high: int = TIMING_VARIATION_HIGH
) -> int:
    if isinstance(u, bool) or not isinstance(u, int) or not (0 <= u < U64_MOD):
        raise EngineError(ErrorCode.INVALID_ARGUMENT, f"u must be a u64, got {u!r}")
    if low != -high or high <= 0:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"inverse_cdf_variation needs a symmetric [low,high] about zero, got [{low},{high}]",
        )
    branch_one = 2 * u < U64_MOD
    scaled = u if branch_one else U64_MOD - u
    eight_h_sq = 8 * high * high
    k = high
    for candidate in range(high + 1):
        if eight_h_sq * scaled <= U64_MOD * (2 * candidate + 1) ** 2:
            k = candidate
            break
    base = k - high
    result = base if branch_one else -base
    return max(low, min(high, result))


def uniform01(u: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _UNIFORM01_PREC
        return Decimal(u) / Decimal(U64_MOD)


class SequentialStream:

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
        self._rng._resync(self)
        u = sequential_draw(self.seed, self.counter)
        self._rng._audit_draw(self.name, "run", 1, self.name, "sequential", self.counter)
        self.counter += 1
        self._rng._persist(self)
        return u

    def state(self) -> tuple[str, int]:
        self._rng._resync(self)
        return self.seed.hex(), self.counter

    def restore(self, seed_hex: str, counter: int) -> None:
        self.seed = bytes.fromhex(seed_hex)
        self.counter = counter


class Rng:

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
        self._seq: dict[str, tuple[str, int]] = {}
        self._streams: dict[str, SequentialStream] = {}
        self._audit: AuditRecorder | None = audit_recorder
        self._txn_open = False
        self._conn = conn
        if conn is not None:
            self._adopt_persisted()

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
        self._require_txn(what)
        if self._conn is not None and not self._conn.in_transaction:
            raise EngineError(
                ErrorCode.DRAW_OUTSIDE_TXN,
                f"{what} is callable only inside the database transaction "
                "that persists _rng_state",
            )

    def _require_advance_txn(self, name: str) -> None:
        self._require_state_write_txn(f"sequential draw [{name}]")

    def stream_seed(self, stream_name: str) -> bytes:
        seed = stream_seed(self.master_seed, self.spec_version, stream_name)
        if stream_name not in {s.value for s in KeyedStreamName} and stream_name not in {
            s.value for s in SequentialStreamName
        }:
            raise EngineError(ErrorCode.STATE_ILLEGAL, f"unknown stream name {stream_name!r}")
        return seed

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

    def inverse_cdf_variation(
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
        u = self.draw(stream_name, entity_type, entity_id, scope, event_kind, draw_index)
        return inverse_cdf_variation(u, low=low, high=high)

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
            self._seq[name] = st.state()
        return self._streams[name]

    def seq_state(self) -> dict[str, tuple[str, int]]:
        for name, st in self._streams.items():
            self._seq[name] = st.state()
        return {name: self._seq[name] for name in sorted(self._seq)}

    def restore_seq_state(self, state: dict[str, tuple[str, int]]) -> None:
        self._require_state_write_txn("restore_seq_state")
        for name in sorted(state):
            seed_hex, counter = state[name]
            st = self.stream(name)
            expected = stream_seed(self.master_seed, self.spec_version, name).hex()
            if seed_hex != expected:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"restored seed for stream {name!r} does not match this run's master seed",
                )
            st.restore(seed_hex, counter)
            self._persist(st)

    def _adopt_persisted(self) -> None:
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
        if self._conn is not None:
            st.counter = self._load_counter(st.name, st.seed)

    def _persist(self, st: SequentialStream) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO _rng_state(stream_name, seed, counter) VALUES (?,?,?) "
            "ON CONFLICT(stream_name) DO UPDATE SET "
            "seed=excluded.seed, counter=excluded.counter",
            (st.name, st.seed.hex(), st.counter),
        )
        self._seq[st.name] = st.state()

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
    "inverse_cdf_variation",
    "keyed_draw",
    "randint",
    "sequential_draw",
    "sqlite_audit_recorder",
    "stream_seed",
    "triangular",
    "uniform01",
]
