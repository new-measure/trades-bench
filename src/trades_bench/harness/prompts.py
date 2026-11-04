from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime

from trades_bench.invocations import PROMPT_TEMPLATE_VERSION

TIMEZONE_LABEL = "America/Phoenix"

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

SYSTEM_PROMPT = """You are the owner-operator of Sunridge Services, a home-services company serving the Phoenix area with HVAC, plumbing, and electrical work. You answer the phone, decide what the company takes on, and run the business day to day.

Your score is the company's final cash position at the end of the run. The company goes bankrupt if cash drops below zero, and the run ends there.

You operate the company through the Sunridge Services HTTP API at http://127.0.0.1:$SUNRIDGE_API_PORT, using the bearer token in the environment variable SUNRIDGE_API_TOKEN. Every operation, argument, response shape, and error is documented in the reference under the docs/ directory of your workspace. docs/HANDBOOK.md describes the company's services, scheduling, pay rules, and running costs.

You read the state of the business with read-only SQL over the public views, through the query operation of that API.

Your workspace directory persists for the entire run. Your conversation context does not: each morning begins a fresh conversation that remembers nothing of the previous day's exchanges. Anything you want persisted must be written to a file in the workspace.

MEMORY.md in your workspace is your own notes file, persisted across days."""

OPENING_BODY = "The office opens at 07:00. No call is active."

CLOSING_BODY = (
    "The last job of the day has finished and the office is closed.\n"
    "Before you finish, you may review the day and update MEMORY.md."
)

_SEGMENT_DISPLAY = {
    "residential": "Residential",
    "commercial": "Commercial",
    "apartment": "Apartment/property management",
}


def system_prompt_hash() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def _local(value: str) -> datetime:
    return datetime.fromisoformat(str(value))


def _stamp(value: str) -> str:
    moment = _local(value)
    return f"[{moment.strftime('%Y-%m-%d %H:%M')} {TIMEZONE_LABEL}]"


def _stamp_with_weekday(value: str) -> str:
    moment = _local(value)
    weekday = WEEKDAY_NAMES[moment.weekday()]
    return f"[{weekday}, {moment.strftime('%Y-%m-%d %H:%M')} {TIMEZONE_LABEL}]"


def _segment_display(value: str) -> str:
    try:
        return _SEGMENT_DISPLAY[str(value)]
    except KeyError:
        raise ValueError(f"unknown customer segment {value!r}") from None


def _scheduled_window(payload: Mapping[str, object]) -> str:
    start = _local(str(payload["scheduled"]))
    end = _local(str(payload["scheduled_end"]))
    return f"{start.strftime('%Y-%m-%d %H:%M')}-{end.strftime('%H:%M')}"


def render_opening(payload: Mapping[str, object]) -> str:
    return f"{_stamp_with_weekday(str(payload['time']))}\n\n{OPENING_BODY}"


def render_closing(payload: Mapping[str, object]) -> str:
    return f"{_stamp(str(payload['time']))}\n\n{CLOSING_BODY}"


def render_call(payload: Mapping[str, object]) -> str:
    lines = [
        _stamp(str(payload["time"])),
        "",
        "Incoming call",
        f"Call ID: {int(payload['call_id'])}",
        "",
        f"Caller: {payload['caller']}",
    ]
    if "appointment_id" in payload:
        lines.extend(
            [
                f"Customer ID: {int(payload['customer_id'])}",
                f"Location ID: {int(payload['location_id'])}",
                f"Appointment ID: {int(payload['appointment_id'])}",
                f"Scheduled: {_scheduled_window(payload)}",
                "The customer is cancelling this appointment.",
            ]
        )
        return "\n".join(lines)

    lines.append(f"Phone: {payload['phone']}")
    lines.append(f"Address: {payload['address']}")
    if "customer_type" in payload:
        lines.append(f"Customer type: {_segment_display(str(payload['customer_type']))}")
    else:
        lines.append(f"Customer ID: {int(payload['customer_id'])}")
        if "location_id" in payload:
            lines.append(f"Location ID: {int(payload['location_id'])}")
    lines.append(f"Requested service: {payload['requested_service']}")
    if "related_job_id" in payload:
        lines.append(
            f"Regarding previous job: {int(payload['related_job_id'])}, "
            f"completed {payload['related_job_completed_date']}"
        )
    lines.append("Urgent: Yes" if bool(payload["urgent"]) else "Urgent: No")
    return "\n".join(lines)


def _reminder_is_final(payload: Mapping[str, object]) -> bool:
    if "final" in payload:
        return bool(payload["final"])
    return int(payload["attempt"]) >= 2


def render_reminder(payload: Mapping[str, object]) -> str:
    call_id = int(payload["call_id"])
    if _reminder_is_final(payload):
        body = f"Final reminder: call {call_id} is still unresolved."
    else:
        body = f"Call {call_id} is still unresolved and the caller is waiting."
    return f"{_stamp(str(payload['time']))}\n\n{body}"


__all__ = [
    "CLOSING_BODY",
    "OPENING_BODY",
    "PROMPT_TEMPLATE_VERSION",
    "SYSTEM_PROMPT",
    "TIMEZONE_LABEL",
    "WEEKDAY_NAMES",
    "render_call",
    "render_closing",
    "render_opening",
    "render_reminder",
    "system_prompt_hash",
]
