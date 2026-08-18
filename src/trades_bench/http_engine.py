from __future__ import annotations

import hmac
import json
import secrets
import select
import socket
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import appointments, booking, calls, clock, crm, db
from .config import RunConfig
from .enums import InvocationType, RunStatus
from .errors import EngineError, ErrorCode, ok_response
from .geo_reader import GeoArtifact
from .ops_read import (
    execute_maps,
    execute_view_get,
    execute_view_list,
    execute_view_singleton,
    execute_weather,
    validate_operation_args,
)
from .registry import OPERATIONS_BY_NAME, OperationSpec
from .sql_gate import open_agent_session
from .sql_static import SqlRejected, check_static, open_shadow_connection, validate_relations
from .txn import Services, command_with_replay, is_reserved
from .txn import run_status as _txn_run_status
from .wire import (
    WireCode,
    build_failure_envelope,
    build_success_envelope,
    native_failure,
    translate_error,
    wire_response_for,
)

_HEALTH_PATH = "/v1/health"
_SQL_PATH = "/v1/sql"
_OPS_PREFIX = "/v1/ops/"
_CONTROL_PREFIX = "/v1/control/"
_HEALTH_BODY = json.dumps({"ok": True}, sort_keys=True, separators=(",", ":")).encode("utf-8")

SUNRIDGE_API_PORT_ENV = "SUNRIDGE_API_PORT"
SUNRIDGE_API_TOKEN_ENV = "SUNRIDGE_API_TOKEN"
SUNRIDGE_WORKSPACE_ENV = "SUNRIDGE_WORKSPACE"

_MAX_BODY_BYTES = 1_000_000
_WATCHDOG_OPCODE_INTERVAL = 2000
_HANDLER_SOCKET_TIMEOUT_SECONDS = 3.0
_STOP_LOCK_TIMEOUT_SECONDS = 5.0
_STOP_DRAIN_TIMEOUT_SECONDS = 20.0
_STOP_DRAIN_POLL_SECONDS = 0.02
_LOOPBACK_HOST = "127.0.0.1"

_CONTROL_OPERATIONS = frozenset({"next_invocation", "complete_invocation"})
_INVOCATION_TYPES = {member.value: member for member in InvocationType}


def _mutation_fingerprint_args(args: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in args.items() if key != "request_id"}


def _dispatch_mutation(
    engine: Engine,
    request_id: str,
    args: dict[str, object],
    *,
    spec: OperationSpec,
    run: object,
) -> tuple[bytes, bool]:
    return command_with_replay(
        engine.services,
        request_id,
        lambda ctx: run(ctx, engine, args),
        operation=spec.name,
        args=_mutation_fingerprint_args(args),
    )


_CRM_CREATE_CUSTOMER_SPEC = OPERATIONS_BY_NAME["crm.create_customer"]
_CRM_CREATE_LOCATION_SPEC = OPERATIONS_BY_NAME["crm.create_location"]
_CALLS_REJECT_SPEC = OPERATIONS_BY_NAME["calls.reject"]
_JOBS_BOOK_SPEC = OPERATIONS_BY_NAME["jobs.book"]
_DISPATCH_ASSIGN_SPEC = OPERATIONS_BY_NAME["dispatch.assign"]
_APPOINTMENTS_ACKNOWLEDGE_CANCELLATION_SPEC = OPERATIONS_BY_NAME[
    "appointments.acknowledge_cancellation"
]


def _dispatch_crm_create_customer(
    engine: Engine, request_id: str, args: dict[str, object]
) -> tuple[bytes, bool]:
    return _dispatch_mutation(
        engine,
        request_id,
        args,
        spec=_CRM_CREATE_CUSTOMER_SPEC,
        run=lambda ctx, engine, args: crm.create_customer(
            ctx,
            engine.config,
            call_id=int(args["call_id"]),
            name=str(args["name"]),
            segment=str(args["segment"]),
            contacts=args["contacts"],
        ),
    )


def _dispatch_crm_create_location(
    engine: Engine, request_id: str, args: dict[str, object]
) -> tuple[bytes, bool]:
    return _dispatch_mutation(
        engine,
        request_id,
        args,
        spec=_CRM_CREATE_LOCATION_SPEC,
        run=lambda ctx, engine, args: crm.create_location(
            ctx,
            engine.config,
            call_id=int(args["call_id"]),
            customer_id=int(args["customer_id"]),
            address=str(args["address"]),
            contacts=args.get("contacts"),
            artifact=engine.artifact,
        ),
    )


def _dispatch_calls_reject(
    engine: Engine, request_id: str, args: dict[str, object]
) -> tuple[bytes, bool]:
    return _dispatch_mutation(
        engine,
        request_id,
        args,
        spec=_CALLS_REJECT_SPEC,
        run=lambda ctx, engine, args: calls.reject(
            ctx, engine.config, call_id=int(args["call_id"])
        ),
    )


def _dispatch_jobs_book(
    engine: Engine, request_id: str, args: dict[str, object]
) -> tuple[bytes, bool]:
    return _dispatch_mutation(
        engine,
        request_id,
        args,
        spec=_JOBS_BOOK_SPEC,
        run=lambda ctx, engine, args: booking.book(
            ctx,
            engine.config,
            call_id=int(args["call_id"]),
            customer_id=int(args["customer_id"]),
            location_id=int(args["location_id"]),
            job_type_id=int(args["job_type_id"]),
            service_date=str(args["service_date"]),
            block_start=str(args["block_start"]),
            technician_id=int(args["technician_id"]),
            artifact=engine.artifact,
        ),
    )


def _dispatch_dispatch_assign(
    engine: Engine, request_id: str, args: dict[str, object]
) -> tuple[bytes, bool]:
    return _dispatch_mutation(
        engine,
        request_id,
        args,
        spec=_DISPATCH_ASSIGN_SPEC,
        run=lambda ctx, engine, args: appointments.assign(
            ctx,
            engine.config,
            appointment_id=int(args["appointment_id"]),
            technician_id=int(args["technician_id"]),
            artifact=engine.artifact,
        ),
    )


def _dispatch_appointments_acknowledge_cancellation(
    engine: Engine, request_id: str, args: dict[str, object]
) -> tuple[bytes, bool]:
    return _dispatch_mutation(
        engine,
        request_id,
        args,
        spec=_APPOINTMENTS_ACKNOWLEDGE_CANCELLATION_SPEC,
        run=lambda ctx, engine, args: appointments.acknowledge_cancellation(
            ctx,
            engine.config,
            call_id=int(args["call_id"]),
            artifact=engine.artifact,
        ),
    )


_MUTATION_DISPATCH: dict[str, object] = {
    _CRM_CREATE_CUSTOMER_SPEC.name: _dispatch_crm_create_customer,
    _CRM_CREATE_LOCATION_SPEC.name: _dispatch_crm_create_location,
    _CALLS_REJECT_SPEC.name: _dispatch_calls_reject,
    _JOBS_BOOK_SPEC.name: _dispatch_jobs_book,
    _DISPATCH_ASSIGN_SPEC.name: _dispatch_dispatch_assign,
    _APPOINTMENTS_ACKNOWLEDGE_CANCELLATION_SPEC.name: (
        _dispatch_appointments_acknowledge_cancellation
    ),
}

_REGISTRY_MUTATION_NAMES = frozenset(
    spec.name for spec in OPERATIONS_BY_NAME.values() if spec.mutation
)
if set(_MUTATION_DISPATCH) != _REGISTRY_MUTATION_NAMES:
    raise AssertionError(
        "mutation dispatch table does not match the registry's mutation operations: "
        f"missing={sorted(_REGISTRY_MUTATION_NAMES - set(_MUTATION_DISPATCH))} "
        f"extra={sorted(set(_MUTATION_DISPATCH) - _REGISTRY_MUTATION_NAMES)}"
    )


def open_engine_connection(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


@dataclass(frozen=True)
class Tokens:

    agent: str
    control: str

    @classmethod
    def generate(cls) -> Tokens:
        return cls(agent=secrets.token_hex(32), control=secrets.token_hex(32))


class _DeadlineHit(Exception):
    pass


class _SqlTimeout(Exception):
    pass


class _ClientGone(Exception):
    pass


def _client_disconnected(sock: socket.socket | None) -> bool:
    if sock is None:
        return False
    try:
        ready, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    if not ready:
        return False
    try:
        peek = sock.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return False
    except OSError:
        return True
    return peek == b""


class _Watchdog:

    __slots__ = ("_deadline_at", "_sql_timeout_at", "_sock", "fired")

    def __init__(
        self,
        *,
        deadline_at: float,
        sql_timeout_at: float | None,
        sock: socket.socket | None,
    ) -> None:
        self._deadline_at = deadline_at
        self._sql_timeout_at = sql_timeout_at
        self._sock = sock
        self.fired: str | None = None

    def progress_handler(self) -> int:
        if self.fired is not None:
            return 0
        now = time.monotonic()
        if self._sql_timeout_at is not None and now >= self._sql_timeout_at:
            self.fired = "sql_timeout"
            return 1
        if now >= self._deadline_at:
            self.fired = "deadline"
            return 1
        if _client_disconnected(self._sock):
            self.fired = "disconnected"
            return 1
        return 0


def _raise_for_watchdog(watchdog: _Watchdog) -> None:
    if watchdog.fired == "sql_timeout":
        raise _SqlTimeout() from None
    if watchdog.fired == "deadline":
        raise _DeadlineHit() from None
    if watchdog.fired == "disconnected":
        raise _ClientGone() from None


class _FairLock:

    __slots__ = ("_guard", "_waiters", "_locked")

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._waiters: deque[threading.Event] = deque()
        self._locked = False

    def acquire(self, timeout: float) -> bool:
        event = threading.Event()
        with self._guard:
            if not self._locked and not self._waiters:
                self._locked = True
                return True
            self._waiters.append(event)
        event.wait(timeout if timeout > 0 else 0)
        with self._guard:
            if event.is_set():
                return True
            try:
                self._waiters.remove(event)
            except ValueError:
                pass
            return False

    def release(self) -> None:
        with self._guard:
            if self._waiters:
                self._waiters.popleft().set()
            else:
                self._locked = False


class _InFlightGate:

    __slots__ = ("_guard", "_count", "_limit")

    def __init__(self, limit: int) -> None:
        self._guard = threading.Lock()
        self._count = 0
        self._limit = limit

    def try_enter(self) -> bool:
        with self._guard:
            if self._count >= self._limit:
                return False
            self._count += 1
            return True

    def leave(self) -> None:
        with self._guard:
            self._count -= 1

    def count(self) -> int:
        with self._guard:
            return self._count


def _fit_row_count(columns: list[str], rows: list[list], max_bytes: int) -> int:
    def encoded_len(n: int) -> int:
        payload = {"columns": columns, "rows": rows[:n], "row_count": n, "truncated": True}
        return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    lo, hi = 0, len(rows)
    if encoded_len(hi) <= max_bytes:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if encoded_len(mid) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return lo


class Engine:

    def __init__(
        self,
        *,
        services: Services,
        config: RunConfig,
        db_path: str | Path,
        host: str = "127.0.0.1",
        artifact: GeoArtifact | None = None,
    ) -> None:
        if host != _LOOPBACK_HOST:
            raise ValueError(f"engine must bind to {_LOOPBACK_HOST} only, got {host!r}")
        self.services = services
        self.config = config
        self.db_path = str(db_path)
        self.host = host
        self.artifact = artifact
        self.tokens = Tokens.generate()
        self.request_deadline_seconds = config.get_int("interface.request_deadline_seconds")
        self.lock_acquire_timeout_seconds = config.get_int("interface.lock_acquire_timeout_seconds")
        self.max_inflight_requests = config.get_int("interface.max_inflight_requests")
        self.sql_statement_timeout_seconds = config.get_int("interface.sql_statement_timeout_seconds")
        self._lock = _FairLock()
        self._gate = _InFlightGate(self.max_inflight_requests)
        self.sql_conn = open_agent_session(self.db_path)
        self.shadow_conn = open_shadow_connection(self.db_path)
        self._as_of_fallback = clock.to_iso8601(clock.now(services.conn))
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._shutting_down = False
        self._stopped = False
        self._stop_guard = threading.Lock()

    def current_as_of(self) -> str:
        try:
            value = clock.to_iso8601(clock.now(self.services.conn))
        except (sqlite3.Error, EngineError):
            return self._as_of_fallback
        self._as_of_fallback = value
        return value

    def committed_as_of(self) -> str:
        return self._as_of_fallback

    def _native_response(self, wire_code: WireCode, *, details: dict | None = None) -> tuple[int, bytes]:
        translated = native_failure(wire_code, details=details)
        return translated.status, build_failure_envelope(translated, as_of=self.committed_as_of())

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("engine is not started")
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("engine has been stopped and cannot be restarted")
        if self._httpd is not None:
            raise RuntimeError("engine already started")
        httpd = ThreadingHTTPServer((self.host, 0), _Handler)
        httpd.engine = self  # type: ignore[attr-defined]
        self._httpd = httpd
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._stop_guard:
            if self._stopped:
                return
            self._shutting_down = True
            httpd = self._httpd
            thread = self._thread
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
            if thread is not None:
                thread.join(timeout=5)
            self._httpd = None
            self._thread = None

            lock_acquired = self._lock.acquire(_STOP_LOCK_TIMEOUT_SECONDS)
            drain_deadline = time.monotonic() + _STOP_DRAIN_TIMEOUT_SECONDS
            while self._gate.count() > 0 and time.monotonic() < drain_deadline:
                time.sleep(_STOP_DRAIN_POLL_SECONDS)
            if lock_acquired:
                self._lock.release()

            try:
                self.sql_conn.close()
            except Exception:
                pass
            try:
                self.shadow_conn.close()
            except Exception:
                pass
            self._stopped = True


class _Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "sunridge"
    timeout = _HANDLER_SOCKET_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: object) -> None:
        pass

    def version_string(self) -> str:
        return self.server_version

    @property
    def engine(self) -> Engine:
        return self.server.engine  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except Exception:
            self._respond_internal_error()

    def _do_GET(self) -> None:
        if self.path == _HEALTH_PATH:
            self._write(200, _HEALTH_BODY)
            return
        self._respond_unknown_route()

    def do_HEAD(self) -> None:
        self._respond_unknown_route()

    def do_PUT(self) -> None:
        self._respond_unknown_route()

    def do_DELETE(self) -> None:
        self._respond_unknown_route()

    def do_OPTIONS(self) -> None:
        self._respond_unknown_route()

    def do_PATCH(self) -> None:
        self._respond_unknown_route()

    def do_POST(self) -> None:
        try:
            self._do_POST()
        except Exception:
            self._respond_internal_error()

    def _do_POST(self) -> None:
        engine = self.engine
        deadline_at = time.monotonic() + engine.request_deadline_seconds

        if self._is_chunked_request():
            self.close_connection = True
            self._respond_native(WireCode.INVALID_REQUEST, details={"field": "body"})
            return

        body_bytes = self._read_body()
        if body_bytes is None:
            self.close_connection = True
            self._respond_native(WireCode.INVALID_REQUEST, details={"field": "body"})
            return

        path = self.path
        if path == _SQL_PATH:
            route_kind: str | None = "sql"
            operation = None
        elif path.startswith(_OPS_PREFIX) and len(path) > len(_OPS_PREFIX):
            route_kind = "ops"
            operation = path[len(_OPS_PREFIX):]
        elif path.startswith(_CONTROL_PREFIX) and len(path) > len(_CONTROL_PREFIX):
            route_kind = "control"
            operation = path[len(_CONTROL_PREFIX):]
        else:
            route_kind = None
            operation = None

        if route_kind is None:
            self._respond_unknown_route()
            return

        token = self._authorization_token()
        principal = self._principal(token)

        if route_kind == "control":
            if principal == "agent":
                self._respond_unknown_route()
                return
            if principal != "control":
                self._respond_unauthorized()
                return
        else:
            if principal != "agent":
                self._respond_unauthorized()
                return

        if body_bytes:
            try:
                body = json.loads(body_bytes)
            except json.JSONDecodeError:
                self._respond_native(WireCode.INVALID_REQUEST, details={"field": "body"})
                return
        else:
            body = {}
        if not isinstance(body, dict):
            self._respond_native(WireCode.INVALID_REQUEST, details={"field": "body"})
            return

        if route_kind == "control":
            self._handle_control(operation, body, deadline_at)
        elif route_kind == "ops":
            self._handle_ops(operation, body, deadline_at)
        else:
            self._handle_sql(body, deadline_at)

    def _is_chunked_request(self) -> bool:
        encoding = self.headers.get("Transfer-Encoding", "")
        return "chunked" in encoding.lower()

    def _authorization_token(self) -> str | None:
        header = self.headers.get("Authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme != "Bearer" or not token:
            return None
        return token

    def _principal(self, token: str | None) -> str | None:
        if token is None:
            return None
        engine = self.engine
        if hmac.compare_digest(token, engine.tokens.agent):
            return "agent"
        if hmac.compare_digest(token, engine.tokens.control):
            return "control"
        return None

    def _read_body(self) -> bytes | None:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return b""
        try:
            length = int(length_header)
        except ValueError:
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            return None
        return self.rfile.read(length)

    def _handle_control(self, operation: str, body: dict, deadline_at: float) -> None:
        engine = self.engine
        if operation not in _CONTROL_OPERATIONS:
            self._respond_unknown_route()
            return
        if engine._shutting_down:
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        if not engine._gate.try_enter():
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        if not engine._lock.acquire(engine.lock_acquire_timeout_seconds):
            engine._gate.leave()
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        try:
            status, payload, replayed = self._compute_control_result(operation, body, deadline_at)
        finally:
            engine._lock.release()
            engine._gate.leave()
        self._write(status, payload, replay=replayed)

    def _extract_request_id(
        self, body: dict, *, mint_if_absent: bool = True
    ) -> tuple[str | None, bool]:
        raw = body.get("request_id")
        if raw is None:
            if mint_if_absent:
                return uuid.uuid4().hex, True
            return None, True
        if not isinstance(raw, str):
            return None, False
        max_chars = self.engine.config.get_int("interface.error_echo_max_chars")
        if len(raw) > max_chars or is_reserved(raw):
            return None, False
        return raw, True

    def _compute_control_result(
        self, operation: str, body: dict, deadline_at: float
    ) -> tuple[int, bytes, bool]:
        engine = self.engine
        if time.monotonic() >= deadline_at:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of()), False

        request_id, request_id_ok = self._extract_request_id(body)
        if not request_id_ok:
            translated = native_failure(WireCode.INVALID_REQUEST, details={"field": "request_id"})
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of()), False

        if operation == "next_invocation":
            engine_bytes, invocation_id, fired, replayed = self._open_invocation(
                body, request_id, deadline_at
            )
        else:
            engine_bytes, invocation_id, fired, replayed = self._complete_invocation(
                body, request_id, deadline_at
            )

        as_of = engine.current_as_of()
        if fired in ("deadline", "disconnected"):
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=as_of), False

        envelope, status = wire_response_for(
            engine_bytes, as_of=as_of, invocation_id=invocation_id, request_id=request_id
        )
        return status, envelope, replayed

    def _open_invocation(
        self, body: dict, request_id: str, deadline_at: float
    ) -> tuple[bytes, int | None, str | None, bool]:
        engine = self.engine
        conn = engine.services.conn
        type_raw = body.get("type")
        if not isinstance(type_raw, str) or type_raw not in _INVOCATION_TYPES:
            error = EngineError(ErrorCode.INVALID_ARGUMENT, "invalid invocation type")
            return error.to_response(), None, None, False
        ts_raw = body.get("ts_ms")
        if ts_raw is None:
            ts_ms = clock.now(conn)
        else:
            try:
                ts_ms = int(ts_raw)
            except (TypeError, ValueError):
                error = EngineError(ErrorCode.INVALID_ARGUMENT, "ts_ms must be an integer")
                return error.to_response(), None, None, False
        prompt_version = body.get("prompt_version")
        watchdog = _Watchdog(deadline_at=deadline_at, sql_timeout_at=None, sock=self.connection)
        conn.set_progress_handler(watchdog.progress_handler, _WATCHDOG_OPCODE_INTERVAL)
        try:
            opened, replayed = engine.services.invocations.open_with_replay(
                _INVOCATION_TYPES[type_raw],
                ts_ms,
                request_id=request_id,
                prompt_version=prompt_version,
                services=engine.services,
            )
        finally:
            conn.set_progress_handler(None, 0)
        if isinstance(opened, bytes):
            return opened, None, watchdog.fired, replayed
        return ok_response({"invocation_id": opened}), int(opened), watchdog.fired, replayed

    def _complete_invocation(
        self, body: dict, request_id: str, deadline_at: float
    ) -> tuple[bytes, int | None, str | None, bool]:
        engine = self.engine
        conn = engine.services.conn
        raw_id = body.get("invocation_id")
        try:
            invocation_id = int(raw_id)
        except (TypeError, ValueError):
            error = EngineError(ErrorCode.INVALID_ARGUMENT, "invocation_id must be an integer")
            return error.to_response(), None, None, False
        watchdog = _Watchdog(deadline_at=deadline_at, sql_timeout_at=None, sock=self.connection)
        conn.set_progress_handler(watchdog.progress_handler, _WATCHDOG_OPCODE_INTERVAL)
        try:
            response, replayed = engine.services.invocations.complete_with_replay(
                invocation_id, request_id=request_id, services=engine.services
            )
        finally:
            conn.set_progress_handler(None, 0)
        return response, invocation_id, watchdog.fired, replayed

    def _resolve_operation(self, operation_path: str) -> OperationSpec | None:
        if "." not in operation_path:
            return None
        namespace, _, operation = operation_path.partition(".")
        if not namespace or not operation:
            return None
        spec = OPERATIONS_BY_NAME.get(operation_path)
        if spec is None:
            return None
        if spec.namespace != namespace or spec.operation != operation:
            return None
        return spec

    def _handle_ops(self, operation_path: str, body: dict, deadline_at: float) -> None:
        engine = self.engine
        spec = self._resolve_operation(operation_path)
        if spec is None:
            self._respond_unknown_route()
            return
        if engine._shutting_down:
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        if not engine._gate.try_enter():
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        if not engine._lock.acquire(engine.lock_acquire_timeout_seconds):
            engine._gate.leave()
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        try:
            status, payload, replay = self._compute_ops_result(spec, body, deadline_at)
        finally:
            engine._lock.release()
            engine._gate.leave()
        self._write(status, payload, replay=replay)

    def _compute_ops_result(
        self, spec: OperationSpec, body: dict, deadline_at: float
    ) -> tuple[int, bytes, bool]:
        engine = self.engine
        if time.monotonic() >= deadline_at:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of()), False

        if _txn_run_status(engine.services.conn) is not RunStatus.ACTIVE:
            translated = native_failure(WireCode.RUN_NOT_ACTIVE)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of()), False

        active = engine.services.invocations.active()
        if active is None:
            translated = native_failure(WireCode.NO_ACTIVE_SESSION)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of()), False
        invocation_id = int(active["invocation_id"])

        if spec.kind == "mutation":
            request_id, request_id_ok = self._extract_request_id(body, mint_if_absent=True)
        else:
            request_id, request_id_ok = self._extract_request_id(body, mint_if_absent=False)
        if not request_id_ok:
            translated = native_failure(WireCode.INVALID_REQUEST, details={"field": "request_id"})
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of()), False

        as_of = engine.current_as_of()
        arg_body = body
        if spec.kind == "read":
            arg_body = {key: value for key, value in body.items() if key != "request_id"}
        try:
            if spec.kind == "mutation":
                engine_bytes, replayed = self._execute_mutation(
                    spec, arg_body, request_id, deadline_at
                )
                envelope, status = wire_response_for(
                    engine_bytes,
                    as_of=as_of,
                    invocation_id=invocation_id,
                    request_id=request_id,
                    operation=spec.name,
                )
                return status, envelope, replayed
            data, next_cursor = self._execute_read(spec, arg_body, deadline_at)
        except EngineError as exc:
            translated = translate_error(
                exc.code,
                exc.details,
                request_id=request_id or "",
                operation=spec.name,
            )
            return translated.status, build_failure_envelope(translated, as_of=as_of), False
        except SqlRejected as exc:
            translated = native_failure(exc.wire_code)
            return translated.status, build_failure_envelope(translated, as_of=as_of), False
        except _DeadlineHit:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=as_of), False
        except _SqlTimeout:
            translated = native_failure(WireCode.SQL_TIMEOUT)
            return translated.status, build_failure_envelope(translated, as_of=as_of), False
        except _ClientGone:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=as_of), False

        envelope = build_success_envelope(
            data,
            as_of=as_of,
            invocation_id=invocation_id,
            request_id=request_id,
            next_cursor=next_cursor,
        )
        return 200, envelope, False

    def _execute_read(
        self, spec: OperationSpec, body: dict, deadline_at: float
    ) -> tuple[object, str | None]:
        engine = self.engine
        args = validate_operation_args(spec, body)
        read = spec.read
        assert read is not None
        if read.kind == "sql":
            statement = args["statement"]
            if not isinstance(statement, str) or not statement.strip():
                raise EngineError(ErrorCode.INVALID_ARGUMENT, "statement must be a non-empty string")
            max_rows_param = args.get("max_rows")
            if max_rows_param is not None:
                max_rows, ok = self._validate_max_rows(max_rows_param)
                if not ok:
                    raise EngineError(
                        ErrorCode.INVALID_ARGUMENT,
                        "max_rows must be a non-negative integer",
                        details={"field": "max_rows"},
                    )
            else:
                max_rows = None
            return self._run_sql(str(statement), max_rows, deadline_at), None
        if read.kind == "weather":
            conn = engine.services.conn

            def _do_weather() -> dict:
                db.begin(conn)
                engine.services.rng.enter_txn()
                try:
                    data = execute_weather(
                        conn, engine.config, engine.services.rng, args
                    )
                except BaseException:
                    engine.services.rng.exit_txn()
                    db.rollback(conn)
                    raise
                engine.services.rng.exit_txn()
                db.rollback(conn)
                return data

            return self._run_with_watchdog(conn, deadline_at, _do_weather), None
        if read.kind == "maps":
            conn = engine.services.conn
            return self._run_with_watchdog(
                conn,
                deadline_at,
                lambda: execute_maps(conn, engine.config, engine.artifact, args),
            ), None
        if read.kind == "view_get":
            return self._run_agent_read(
                deadline_at,
                lambda conn: execute_view_get(conn, spec, args),
            ), None
        if read.kind == "view_singleton":
            return self._run_agent_read(
                deadline_at,
                lambda conn: execute_view_singleton(conn, spec),
            ), None
        if read.kind == "view_list":
            data, next_cursor = self._run_agent_read(
                deadline_at,
                lambda conn: execute_view_list(conn, engine.config, spec, args),
            )
            return data, next_cursor
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"unsupported read kind {read.kind!r}")

    def _run_with_watchdog(
        self, conn: sqlite3.Connection, deadline_at: float, fn: object
    ) -> object:
        watchdog = _Watchdog(deadline_at=deadline_at, sql_timeout_at=None, sock=self.connection)
        conn.set_progress_handler(watchdog.progress_handler, _WATCHDOG_OPCODE_INTERVAL)
        try:
            result = fn()
        except sqlite3.Error:
            _raise_for_watchdog(watchdog)
            raise
        finally:
            conn.set_progress_handler(None, 0)
        if watchdog.fired in ("deadline", "disconnected"):
            raise _DeadlineHit() from None
        return result

    def _run_agent_read(self, deadline_at: float, fn: object) -> object:
        engine = self.engine
        return self._run_with_watchdog(engine.sql_conn, deadline_at, lambda: fn(engine.sql_conn))

    def _execute_mutation(
        self, spec: OperationSpec, body: dict, request_id: str, deadline_at: float
    ) -> tuple[bytes, bool]:
        engine = self.engine
        args = validate_operation_args(spec, body)
        conn = engine.services.conn
        watchdog = _Watchdog(deadline_at=deadline_at, sql_timeout_at=None, sock=self.connection)
        conn.set_progress_handler(watchdog.progress_handler, _WATCHDOG_OPCODE_INTERVAL)
        try:
            dispatch = _MUTATION_DISPATCH.get(spec.name)
            if dispatch is None:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"no mutation dispatch for {spec.name!r}",
                )
            response, replayed = dispatch(engine, request_id, args)
        finally:
            conn.set_progress_handler(None, 0)
        if watchdog.fired in ("deadline", "disconnected"):
            raise _DeadlineHit() from None
        return response, replayed

    def _handle_sql(self, body: dict, deadline_at: float) -> None:
        engine = self.engine
        if engine._shutting_down:
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        if not engine._gate.try_enter():
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        if not engine._lock.acquire(engine.lock_acquire_timeout_seconds):
            engine._gate.leave()
            self._respond_native(WireCode.ENGINE_BUSY)
            return
        try:
            status, payload = self._compute_sql_result(body, deadline_at)
        finally:
            engine._lock.release()
            engine._gate.leave()
        self._write(status, payload)

    def _validate_max_rows(self, max_rows_param: object) -> tuple[int | None, bool]:
        if max_rows_param is None:
            return None, True
        if not isinstance(max_rows_param, int) or isinstance(max_rows_param, bool):
            return None, False
        if max_rows_param < 0:
            return None, False
        return max_rows_param, True

    def _compute_sql_result(self, body: dict, deadline_at: float) -> tuple[int, bytes]:
        engine = self.engine
        if time.monotonic() >= deadline_at:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of())

        if _txn_run_status(engine.services.conn) is not RunStatus.ACTIVE:
            translated = native_failure(WireCode.RUN_NOT_ACTIVE)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of())

        active = engine.services.invocations.active()
        if active is None:
            translated = native_failure(WireCode.NO_ACTIVE_SESSION)
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of())
        invocation_id = int(active["invocation_id"])

        request_id, request_id_ok = self._extract_request_id(body, mint_if_absent=False)
        if not request_id_ok:
            translated = native_failure(WireCode.INVALID_REQUEST, details={"field": "request_id"})
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of())

        sql_text = body.get("sql")
        if not isinstance(sql_text, str) or not sql_text.strip():
            translated = native_failure(WireCode.INVALID_REQUEST, details={"field": "sql"})
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of())

        requested_max_rows, max_rows_ok = self._validate_max_rows(body.get("max_rows"))
        if not max_rows_ok:
            translated = native_failure(WireCode.INVALID_REQUEST, details={"field": "max_rows"})
            return translated.status, build_failure_envelope(translated, as_of=engine.current_as_of())

        as_of = engine.current_as_of()
        try:
            data = self._run_sql(sql_text, requested_max_rows, deadline_at)
        except EngineError as exc:
            translated = translate_error(exc.code, exc.details, request_id=request_id)
            return translated.status, build_failure_envelope(translated, as_of=as_of)
        except SqlRejected as exc:
            translated = native_failure(exc.wire_code)
            return translated.status, build_failure_envelope(translated, as_of=as_of)
        except _DeadlineHit:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=as_of)
        except _SqlTimeout:
            translated = native_failure(WireCode.SQL_TIMEOUT)
            return translated.status, build_failure_envelope(translated, as_of=as_of)
        except _ClientGone:
            translated = native_failure(WireCode.DEADLINE_EXCEEDED)
            return translated.status, build_failure_envelope(translated, as_of=as_of)

        envelope = build_success_envelope(
            data, as_of=as_of, invocation_id=invocation_id, request_id=request_id
        )
        return 200, envelope

    def _run_sql(self, sql_text: str, requested_max_rows: int | None, deadline_at: float) -> dict:
        engine = self.engine
        max_rows = engine.config.get_int("interface.sql_max_rows")
        if requested_max_rows is not None:
            max_rows = min(max_rows, requested_max_rows)
        max_bytes = engine.config.get_int("interface.sql_max_response_bytes")

        check_static(sql_text)

        sql_timeout_at = time.monotonic() + engine.sql_statement_timeout_seconds
        watchdog = _Watchdog(deadline_at=deadline_at, sql_timeout_at=sql_timeout_at, sock=self.connection)

        engine.shadow_conn.set_progress_handler(watchdog.progress_handler, _WATCHDOG_OPCODE_INTERVAL)
        try:
            validate_relations(engine.shadow_conn, sql_text)
        except SqlRejected:
            _raise_for_watchdog(watchdog)
            raise
        finally:
            engine.shadow_conn.set_progress_handler(None, 0)
        _raise_for_watchdog(watchdog)

        engine.sql_conn.set_progress_handler(watchdog.progress_handler, _WATCHDOG_OPCODE_INTERVAL)
        try:
            cursor = engine.sql_conn.execute(sql_text)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            fetched = cursor.fetchmany(max_rows + 1)
        except sqlite3.Error:
            _raise_for_watchdog(watchdog)
            raise SqlRejected(WireCode.SQL_NOT_PERMITTED) from None
        finally:
            engine.sql_conn.set_progress_handler(None, 0)

        truncated = len(fetched) > max_rows
        rows = [
            [cell.hex() if isinstance(cell, bytes) else cell for cell in row]
            for row in fetched[:max_rows]
        ]
        data = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
        encoded_len = len(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        if encoded_len > max_bytes:
            fit = _fit_row_count(columns, rows, max_bytes)
            rows = rows[:fit]
            data = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": True}
        return data

    def _write(self, status: int, body: bytes, *, replay: bool = False) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if replay:
                self.send_header("X-CSR-Idempotent-Replay", "1")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _respond_native(self, wire_code: WireCode, *, details: dict | None = None) -> None:
        status, body = self.engine._native_response(wire_code, details=details)
        self._write(status, body)

    def _respond_unauthorized(self) -> None:
        self._respond_native(WireCode.UNAUTHORIZED)

    def _respond_unknown_route(self) -> None:
        self._respond_native(WireCode.UNKNOWN_OPERATION)

    def _respond_internal_error(self) -> None:
        request_id = uuid.uuid4().hex
        translated = translate_error(ErrorCode.UNEXPECTED_FAILURE, None, request_id=request_id)
        try:
            as_of = self.engine.committed_as_of()
        except Exception:
            as_of = ""
        body = build_failure_envelope(translated, as_of=as_of)
        self._write(translated.status, body)


__all__ = [
    "Engine",
    "SUNRIDGE_API_PORT_ENV",
    "SUNRIDGE_API_TOKEN_ENV",
    "SUNRIDGE_WORKSPACE_ENV",
    "Tokens",
    "open_engine_connection",
]
