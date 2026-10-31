from __future__ import annotations

import http.client
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

CONTROL_PREFIX = "/v1/control/"
HEALTH_PATH = "/v1/health"
REPLAY_HEADER = "X-CSR-Idempotent-Replay"

NEXT_INVOCATION = "next_invocation"
COMPLETE_INVOCATION = "complete_invocation"
RECORD_CALL_ATTEMPT = "record_call_attempt"
RECORD_FINAL_UNANSWERED_ATTEMPT = "record_final_unanswered_attempt"
MARK_CALL_UNANSWERED = "mark_call_unanswered"
FAIL_RUN = "fail_run"
CALL_STATUS = "call_status"

CONTROL_OPERATIONS = (
    NEXT_INVOCATION,
    COMPLETE_INVOCATION,
    RECORD_CALL_ATTEMPT,
    RECORD_FINAL_UNANSWERED_ATTEMPT,
    MARK_CALL_UNANSWERED,
    FAIL_RUN,
    CALL_STATUS,
)

DEFAULT_TIMEOUT_SECONDS = 60.0


class ControlTransportError(RuntimeError):
    pass


def next_invocation_request_id(ordinal: int) -> str:
    return f"next-invocation-{int(ordinal)}"


def complete_invocation_request_id(invocation_id: int) -> str:
    return f"complete-invocation-{int(invocation_id)}"


def call_attempt_request_id(call_id: int, attempt_number: int) -> str:
    return f"call-attempt-{int(call_id)}-{int(attempt_number)}"


def call_final_request_id(call_id: int) -> str:
    return f"call-final-{int(call_id)}"


def call_unanswered_request_id(call_id: int) -> str:
    return f"call-unanswered-{int(call_id)}"


def fail_run_request_id(ordinal: int) -> str:
    return f"fail-run-{int(ordinal)}"


def call_status_request_id(call_id: int) -> str:
    return f"call-status-{int(call_id)}"


@dataclass(frozen=True)
class ControlResult:

    operation: str
    request: Mapping[str, Any]
    status: int
    body: Mapping[str, Any] = field(default_factory=dict)
    replayed: bool = False

    @property
    def request_id(self) -> str:
        return str(self.request.get("request_id", ""))

    @property
    def ok(self) -> bool:
        return self.status == 200 and isinstance(self.body.get("data"), Mapping)

    @property
    def data(self) -> Mapping[str, Any]:
        value = self.body.get("data")
        return value if isinstance(value, Mapping) else {}

    @property
    def error(self) -> Mapping[str, Any]:
        value = self.body.get("error")
        return value if isinstance(value, Mapping) else {}

    @property
    def error_code(self) -> str | None:
        code = self.error.get("code")
        return None if code is None else str(code)

    @property
    def error_details(self) -> Mapping[str, Any]:
        details = self.error.get("details")
        return details if isinstance(details, Mapping) else {}

    @property
    def error_rule(self) -> str | None:
        rule = self.error_details.get("rule")
        return None if rule is None else str(rule)

    def describe_failure(self) -> str:
        rule = self.error_rule
        code = self.error_code or "unknown"
        return f"{self.operation}: {code}" if rule is None else f"{self.operation}: {code}/{rule}"


class ControlClient:

    def __init__(
        self,
        *,
        port: int,
        token: str,
        host: str = "127.0.0.1",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._token = token
        self.timeout = float(timeout)
        self.on_request: Callable[[str, Mapping[str, Any]], None] | None = None

    def health(self) -> bool:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("GET", HEALTH_PATH)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()
        if status != 200:
            return False
        try:
            return bool(json.loads(raw).get("ok"))
        except (json.JSONDecodeError, AttributeError):
            return False

    def next_invocation(self, ordinal: int) -> ControlResult:
        return self.post(
            NEXT_INVOCATION, {"request_id": next_invocation_request_id(ordinal)}
        )

    def complete_invocation(self, invocation_id: int) -> ControlResult:
        return self.post(
            COMPLETE_INVOCATION,
            {
                "request_id": complete_invocation_request_id(invocation_id),
                "invocation_id": int(invocation_id),
            },
        )

    def record_call_attempt(self, call_id: int, attempt_number: int) -> ControlResult:
        return self.post(
            RECORD_CALL_ATTEMPT,
            {
                "request_id": call_attempt_request_id(call_id, attempt_number),
                "call_id": int(call_id),
                "attempt_number": int(attempt_number),
            },
        )

    def record_final_unanswered_attempt(
        self, call_id: int, attempt_number: int
    ) -> ControlResult:
        return self.post(
            RECORD_FINAL_UNANSWERED_ATTEMPT,
            {
                "request_id": call_final_request_id(call_id),
                "call_id": int(call_id),
                "attempt_number": int(attempt_number),
            },
        )

    def mark_call_unanswered(self, call_id: int) -> ControlResult:
        return self.post(
            MARK_CALL_UNANSWERED,
            {
                "request_id": call_unanswered_request_id(call_id),
                "call_id": int(call_id),
            },
        )

    def fail_run(self, reason: str, ordinal: int) -> ControlResult:
        return self.post(
            FAIL_RUN,
            {"request_id": fail_run_request_id(ordinal), "reason": str(reason)},
        )

    def call_status(self, call_id: int) -> ControlResult:
        return self.post(
            CALL_STATUS,
            {"request_id": call_status_request_id(call_id), "call_id": int(call_id)},
        )

    def post(self, operation: str, body: Mapping[str, Any]) -> ControlResult:
        if operation not in CONTROL_OPERATIONS:
            raise ValueError(f"unknown control operation {operation!r}")
        if self.on_request is not None:
            self.on_request(operation, dict(body))
        payload = json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request("POST", CONTROL_PREFIX + operation, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            replayed = response.getheader(REPLAY_HEADER) == "1"
        except (OSError, http.client.HTTPException) as exc:
            raise ControlTransportError(f"control request {operation} failed") from exc
        finally:
            connection.close()
        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ControlTransportError(
                f"control request {operation} returned a non-JSON body"
            ) from exc
        if not isinstance(decoded, dict):
            raise ControlTransportError(
                f"control request {operation} returned a non-object body"
            )
        return ControlResult(
            operation=operation,
            request=dict(body),
            status=status,
            body=decoded,
            replayed=replayed,
        )


__all__ = [
    "CALL_STATUS",
    "CONTROL_OPERATIONS",
    "CONTROL_PREFIX",
    "COMPLETE_INVOCATION",
    "ControlClient",
    "ControlResult",
    "ControlTransportError",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAIL_RUN",
    "HEALTH_PATH",
    "MARK_CALL_UNANSWERED",
    "NEXT_INVOCATION",
    "RECORD_CALL_ATTEMPT",
    "RECORD_FINAL_UNANSWERED_ATTEMPT",
    "REPLAY_HEADER",
    "call_attempt_request_id",
    "call_status_request_id",
    "call_final_request_id",
    "call_unanswered_request_id",
    "complete_invocation_request_id",
    "fail_run_request_id",
    "next_invocation_request_id",
]
