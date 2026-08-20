from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from trades_bench.adapter import AgentTurn, HarnessPromptKind, RunMetadata
from trades_bench.http_engine import SUNRIDGE_API_PORT_ENV, SUNRIDGE_API_TOKEN_ENV

SQL_PATH = "/v1/sql"
OPS_PREFIX = "/v1/ops/"

CANCELLATION_FIELD = "Appointment ID"

CALL_KIND: HarnessPromptKind = "call"

CONTEXT_POLICY = "policy_runner_stateless"
ATTEMPT_DEFINITION = (
    "one policy decision in reply to the active call prompt in which no terminal "
    "action for that call committed"
)

DEFAULT_HORIZON_DAYS = 14
DEFAULT_TIMEOUT_SECONDS = 60.0

SEGMENT_BY_DISPLAY: Mapping[str, str] = {
    "Residential": "residential",
    "Commercial": "commercial",
    "Apartment/property management": "apartment",
}

_STAMP_TIMESTAMP_LENGTH = len("YYYY-MM-DD HH:MM")


class PolicyApiError(RuntimeError):
    pass


class PolicyPromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentEndpoint:

    port: int
    token: str
    host: str = "127.0.0.1"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AgentEndpoint:
        try:
            port = int(env[SUNRIDGE_API_PORT_ENV])
            token = str(env[SUNRIDGE_API_TOKEN_ENV])
        except (KeyError, ValueError) as exc:
            raise PolicyApiError("the agent environment carries no usable endpoint") from exc
        return cls(port=port, token=token)


@dataclass(frozen=True)
class ApiResult:

    operation: str
    status: int
    body: Mapping[str, Any] = field(default_factory=dict)

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
    def error_code(self) -> str:
        return str(self.error.get("code", ""))

    @property
    def error_rule(self) -> str:
        details = self.error.get("details")
        if isinstance(details, Mapping):
            return str(details.get("rule", ""))
        return ""

    @property
    def outcome(self) -> str:
        return str(self.data.get("outcome", ""))

    def describe(self) -> str:
        if self.ok:
            return f"{self.operation}: ok"
        if self.error_rule:
            return f"{self.operation}: {self.error_code}/{self.error_rule}"
        return f"{self.operation}: {self.error_code or self.status}"


class AgentApi:

    def __init__(
        self, endpoint: AgentEndpoint, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.request_count = 0

    def op(self, operation: str, body: Mapping[str, Any]) -> ApiResult:
        status, decoded = self._post(OPS_PREFIX + operation, body)
        return ApiResult(operation=operation, status=status, body=decoded)

    def sql(self, statement: str) -> list[dict[str, Any]]:
        status, decoded = self._post(SQL_PATH, {"sql": statement})
        result = ApiResult(operation="query.sql", status=status, body=decoded)
        if not result.ok:
            raise PolicyApiError(f"read query refused: {result.describe()}")
        columns = [str(name) for name in result.data.get("columns", ())]
        rows = result.data.get("rows", ())
        if not isinstance(rows, list):
            raise PolicyApiError("read query returned no row list")
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def _post(self, path: str, body: Mapping[str, Any]) -> tuple[int, Mapping[str, Any]]:
        payload = json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.endpoint.token}",
            "Content-Type": "application/json",
        }
        connection = http.client.HTTPConnection(
            self.endpoint.host, self.endpoint.port, timeout=self.timeout
        )
        self.request_count += 1
        try:
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise PolicyApiError(f"request to {path} failed") from exc
        finally:
            connection.close()
        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise PolicyApiError(f"request to {path} returned a non-JSON body") from exc
        if not isinstance(decoded, dict):
            raise PolicyApiError(f"request to {path} returned a non-object body")
        return status, decoded


@dataclass(frozen=True)
class ParsedCall:

    call_id: int
    local_time: str
    caller: str
    is_cancellation: bool
    urgent: bool = False
    phone: str | None = None
    address: str | None = None
    segment: str | None = None
    customer_id: int | None = None
    location_id: int | None = None
    appointment_id: int | None = None
    requested_service: str | None = None

    @property
    def moment(self) -> datetime:
        return datetime.strptime(self.local_time, "%Y-%m-%d %H:%M")


def _stamp_timestamp(line: str) -> str:
    inner = line.strip().strip("[]")
    _, _, tail = inner.partition(", ")
    stamped = tail or inner
    return stamped[:_STAMP_TIMESTAMP_LENGTH]


def _labelled_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        label, separator, value = line.partition(": ")
        if separator and label and label not in fields:
            fields[label] = value
    return fields


def _optional_int(fields: Mapping[str, str], label: str) -> int | None:
    value = fields.get(label)
    return None if value is None else int(value)


def parse_call(text: str) -> ParsedCall:
    lines = text.splitlines()
    if not lines:
        raise PolicyPromptError("call prompt is empty")
    fields = _labelled_fields(text)
    if "Call ID" not in fields or "Caller" not in fields:
        raise PolicyPromptError("call prompt carries no call id or caller")
    segment_display = fields.get("Customer type")
    if segment_display is not None and segment_display not in SEGMENT_BY_DISPLAY:
        raise PolicyPromptError(f"unknown customer type {segment_display!r}")
    return ParsedCall(
        call_id=int(fields["Call ID"]),
        local_time=_stamp_timestamp(lines[0]),
        caller=fields["Caller"],
        is_cancellation=CANCELLATION_FIELD in fields,
        urgent=fields.get("Urgent") == "Yes",
        phone=fields.get("Phone"),
        address=fields.get("Address"),
        segment=None if segment_display is None else SEGMENT_BY_DISPLAY[segment_display],
        customer_id=_optional_int(fields, "Customer ID"),
        location_id=_optional_int(fields, "Location ID"),
        appointment_id=_optional_int(fields, "Appointment ID"),
        requested_service=fields.get("Requested service"),
    )


@dataclass(frozen=True)
class PolicyOutcome:

    terminal: bool = False
    notes: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(self.notes)


@dataclass(frozen=True)
class PolicyRequest:

    policy_name: str
    call: ParsedCall
    api: AgentApi
    service_date: str
    horizon_days: int

    def request_id(self, *parts: object) -> str:
        tail = "-".join(str(part) for part in parts)
        return f"{self.policy_name}-{self.call.call_id}-{tail}"


@runtime_checkable
class Policy(Protocol):

    name: str
    version: str

    def decide(self, request: PolicyRequest) -> PolicyOutcome: ...


@dataclass(frozen=True)
class PolicyDecision:

    service_date: str
    call_id: int
    is_cancellation: bool
    terminal: bool
    notes: tuple[str, ...]


class PolicyAdapter:

    def __init__(
        self,
        policy: Policy,
        *,
        endpoint: AgentEndpoint | None = None,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        model_id: str | None = None,
    ) -> None:
        self.policy = policy
        self.name = policy.name
        self.version = policy.version
        self.context_policy = CONTEXT_POLICY
        self.attempt_definition = ATTEMPT_DEFINITION
        self.model_id = model_id
        self.horizon_days = int(horizon_days)
        self.timeout = float(timeout)
        self.decisions: list[PolicyDecision] = []
        self.workspace: Path | None = None
        self.system_prompt: str | None = None
        self.end_status: str | None = None
        self._endpoint = endpoint
        self._api: AgentApi | None = None
        self._service_date: str | None = None
        self._decided: set[int] = set()

    def start_run(self, workspace: Path, system_prompt: str, env: Mapping[str, str]) -> None:
        self.workspace = Path(workspace)
        self.system_prompt = system_prompt
        endpoint = self._endpoint or AgentEndpoint.from_env(env)
        self._endpoint = endpoint
        self._api = AgentApi(endpoint, timeout=self.timeout)

    def start_day(self, service_date: date) -> None:
        self._service_date = service_date.isoformat()

    def end_day(self, service_date: date) -> None:
        self._service_date = None

    def end_run(self, status: str) -> None:
        self.end_status = status

    def describe(self) -> RunMetadata:
        return RunMetadata(
            name=self.name,
            version=self.version,
            context_policy=self.context_policy,
            attempt_definition=self.attempt_definition,
            model_id=self.model_id,
        )

    def send(self, prompt: str, kind: HarnessPromptKind, deadline_s: float) -> AgentTurn:
        if kind != CALL_KIND:
            return self._turn(PolicyOutcome())
        call = parse_call(prompt)
        if call.call_id in self._decided:
            return self._turn(PolicyOutcome())
        if self._api is None:
            raise PolicyApiError("the adapter was sent a call before start_run")
        outcome = self.policy.decide(
            PolicyRequest(
                policy_name=self.policy.name,
                call=call,
                api=self._api,
                service_date=self._service_date or call.local_time[:10],
                horizon_days=self.horizon_days,
            )
        )
        self._decided.add(call.call_id)
        self.decisions.append(
            PolicyDecision(
                service_date=self._service_date or call.local_time[:10],
                call_id=call.call_id,
                is_cancellation=call.is_cancellation,
                terminal=outcome.terminal,
                notes=outcome.notes,
            )
        )
        return self._turn(outcome)

    def _turn(self, outcome: PolicyOutcome) -> AgentTurn:
        return AgentTurn(
            text=outcome.text, ended_normally=True, input_tokens=0, output_tokens=0
        )


__all__ = [
    "ATTEMPT_DEFINITION",
    "CANCELLATION_FIELD",
    "CONTEXT_POLICY",
    "DEFAULT_HORIZON_DAYS",
    "DEFAULT_TIMEOUT_SECONDS",
    "OPS_PREFIX",
    "SEGMENT_BY_DISPLAY",
    "SQL_PATH",
    "AgentApi",
    "AgentEndpoint",
    "ApiResult",
    "ParsedCall",
    "Policy",
    "PolicyAdapter",
    "PolicyApiError",
    "PolicyDecision",
    "PolicyOutcome",
    "PolicyPromptError",
    "PolicyRequest",
    "parse_call",
]
