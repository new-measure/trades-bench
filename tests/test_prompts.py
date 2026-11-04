from __future__ import annotations

import hashlib
import locale

import pytest

from trades_bench.harness import prompts

MEMORY_PARAGRAPH = "MEMORY.md in your workspace is your own notes file, persisted across days."


FORBIDDEN_SUBSTRINGS = (
    "unsupported",
    "trades_bench",
    "trades-bench",
    "csr_bench",
    "bench",
    "weather",
    "forecast",
    "dashboard",
    "notification",
    "calls left",
    "remaining calls",
)

NEW_CALLER_PAYLOAD = {
    "time": "2026-10-01T10:24:00-07:00",
    "call_id": 5512,
    "caller": "Morgan Lee",
    "phone": "(602) 555-0142",
    "address": "1234 Example Street, Tempe, AZ 85281",
    "customer_type": "residential",
    "requested_service": "HVAC service",
    "urgent": True,
}

KNOWN_ADDRESS_PAYLOAD = {
    "time": "2026-10-01T11:02:00-07:00",
    "call_id": 5513,
    "caller": "Sandoval Property Group",
    "phone": "(480) 555-0177",
    "address": "88 W Rio Salado Pkwy, Tempe, AZ 85281",
    "customer_id": 1842,
    "location_id": 2901,
    "requested_service": "Plumbing",
    "urgent": False,
}

NEW_ADDRESS_PAYLOAD = {
    "time": "2026-10-01T11:41:00-07:00",
    "call_id": 5517,
    "caller": "Sandoval Property Group",
    "phone": "(480) 555-0177",
    "address": "4420 S McClintock Dr, Tempe, AZ 85282",
    "customer_id": 1842,
    "requested_service": "Electrical",
    "urgent": False,
}

CALLBACK_PAYLOAD = {
    "time": "2026-10-01T13:40:00-07:00",
    "call_id": 5514,
    "caller": "Morgan Lee",
    "phone": "(602) 555-0142",
    "address": "1234 Example Street, Tempe, AZ 85281",
    "customer_id": 1901,
    "location_id": 3055,
    "requested_service": "HVAC service",
    "related_job_id": 8712,
    "related_job_completed_date": "2026-09-24",
    "urgent": False,
}

CANCELLATION_PAYLOAD = {
    "time": "2026-10-01T15:15:00-07:00",
    "call_id": 5515,
    "caller": "Sandoval Property Group",
    "customer_id": 1842,
    "location_id": 2901,
    "appointment_id": 9871,
    "scheduled": "2026-10-03T09:00:00-07:00",
    "scheduled_end": "2026-10-03T11:00:00-07:00",
}

NEW_CALLER_TEXT = """[2026-10-01 10:24 America/Phoenix]

Incoming call
Call ID: 5512

Caller: Morgan Lee
Phone: (602) 555-0142
Address: 1234 Example Street, Tempe, AZ 85281
Customer type: Residential
Requested service: HVAC service
Urgent: Yes"""

KNOWN_ADDRESS_TEXT = """[2026-10-01 11:02 America/Phoenix]

Incoming call
Call ID: 5513

Caller: Sandoval Property Group
Phone: (480) 555-0177
Address: 88 W Rio Salado Pkwy, Tempe, AZ 85281
Customer ID: 1842
Location ID: 2901
Requested service: Plumbing
Urgent: No"""

NEW_ADDRESS_TEXT = """[2026-10-01 11:41 America/Phoenix]

Incoming call
Call ID: 5517

Caller: Sandoval Property Group
Phone: (480) 555-0177
Address: 4420 S McClintock Dr, Tempe, AZ 85282
Customer ID: 1842
Requested service: Electrical
Urgent: No"""

CALLBACK_TEXT = """[2026-10-01 13:40 America/Phoenix]

Incoming call
Call ID: 5514

Caller: Morgan Lee
Phone: (602) 555-0142
Address: 1234 Example Street, Tempe, AZ 85281
Customer ID: 1901
Location ID: 3055
Requested service: HVAC service
Regarding previous job: 8712, completed 2026-09-24
Urgent: No"""

CANCELLATION_TEXT = """[2026-10-01 15:15 America/Phoenix]

Incoming call
Call ID: 5515

Caller: Sandoval Property Group
Customer ID: 1842
Location ID: 2901
Appointment ID: 9871
Scheduled: 2026-10-03 09:00-11:00
The customer is cancelling this appointment."""

ALL_CALL_CASES = (
    (NEW_CALLER_PAYLOAD, NEW_CALLER_TEXT),
    (KNOWN_ADDRESS_PAYLOAD, KNOWN_ADDRESS_TEXT),
    (NEW_ADDRESS_PAYLOAD, NEW_ADDRESS_TEXT),
    (CALLBACK_PAYLOAD, CALLBACK_TEXT),
    (CANCELLATION_PAYLOAD, CANCELLATION_TEXT),
)


def _assert_clean(text: str) -> None:
    lowered = text.lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        assert needle not in lowered, needle


def test_har01_opening_weekday_render_is_byte_exact():
    payload = {"service_date": "2026-10-01", "time": "2026-10-01T06:45:00-07:00"}
    assert prompts.render_opening(payload) == (
        "[Thursday, 2026-10-01 06:45 America/Phoenix]\n"
        "\n"
        "The office opens at 07:00. No call is active."
    )


def test_har01_opening_weekend_render_is_byte_exact():
    payload = {"service_date": "2026-10-03", "time": "2026-10-03T06:45:00-07:00"}
    assert prompts.render_opening(payload) == (
        "[Saturday, 2026-10-03 06:45 America/Phoenix]\n"
        "\n"
        "The office opens at 07:00. No call is active."
    )


def test_har01_opening_zero_job_day_render_is_identical():
    busy = prompts.render_opening(
        {
            "service_date": "2026-10-05",
            "time": "2026-10-05T06:45:00-07:00",
            "call_count": 11,
            "scheduled_jobs": 4,
            "cash_cents": 1_250_000,
        }
    )
    quiet = prompts.render_opening(
        {
            "service_date": "2026-10-05",
            "time": "2026-10-05T06:45:00-07:00",
            "call_count": 0,
            "scheduled_jobs": 0,
            "cash_cents": 1_250_000,
        }
    )
    bare = prompts.render_opening(
        {"service_date": "2026-10-05", "time": "2026-10-05T06:45:00-07:00"}
    )
    assert busy == quiet == bare
    assert busy.splitlines()[2] == "The office opens at 07:00. No call is active."


def test_har01_opening_weekday_name_does_not_follow_the_process_locale():
    payload = {"service_date": "2026-10-01", "time": "2026-10-01T06:45:00-07:00"}
    expected = prompts.render_opening(payload)
    previous = locale.setlocale(locale.LC_TIME)
    rendered = None
    try:
        for candidate in ("de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8", "C"):
            try:
                locale.setlocale(locale.LC_TIME, candidate)
            except locale.Error:
                continue
            rendered = prompts.render_opening(payload)
            assert rendered == expected, candidate
    finally:
        locale.setlocale(locale.LC_TIME, previous)
    assert rendered is not None
    assert "Thursday" in expected


def test_har01_opening_carries_no_dashboard_or_counts():
    text = prompts.render_opening({"time": "2026-10-01T06:45:00-07:00"})
    _assert_clean(text)
    assert "cash" not in text.lower()
    assert len(text.splitlines()) == 3


@pytest.mark.parametrize(("payload", "expected"), ALL_CALL_CASES)
def test_har02_call_variants_render_byte_exact(payload, expected):
    assert prompts.render_call(payload) == expected


def test_har02_cancellation_composes_single_scheduled_line():
    text = prompts.render_call(CANCELLATION_PAYLOAD)
    scheduled_lines = [line for line in text.splitlines() if line.startswith("Scheduled")]
    assert scheduled_lines == ["Scheduled: 2026-10-03 09:00-11:00"]
    assert "scheduled_end" not in text


def test_har02_new_caller_states_type_and_known_caller_omits_it():
    assert "Customer type: Residential" in prompts.render_call(NEW_CALLER_PAYLOAD)
    assert "Customer type" not in prompts.render_call(KNOWN_ADDRESS_PAYLOAD)
    assert "Customer ID" not in prompts.render_call(NEW_CALLER_PAYLOAD)


def test_har02_known_customer_at_new_address_omits_location_id():
    text = prompts.render_call(NEW_ADDRESS_PAYLOAD)
    assert "Customer ID: 1842" in text
    assert "Location ID" not in text


def test_har02_entity_ids_render_as_bare_integers():
    text = prompts.render_call(CALLBACK_PAYLOAD)
    assert "Call ID: 5514" in text
    assert "Customer ID: 1901" in text
    assert "Regarding previous job: 8712, completed 2026-09-24" in text
    assert "J-" not in text
    assert "#" not in text


def test_har02_call_prompts_carry_no_forbidden_content():
    for payload, _ in ALL_CALL_CASES:
        _assert_clean(prompts.render_call(payload))


def test_har02_unsupported_category_renders_its_real_name():
    payload = dict(NEW_CALLER_PAYLOAD, requested_service="Roofing")
    text = prompts.render_call(payload)
    assert "Requested service: Roofing" in text
    _assert_clean(text)


def test_har02_segment_display_forms():
    for segment, display in (
        ("residential", "Residential"),
        ("commercial", "Commercial"),
        ("apartment", "Apartment/property management"),
    ):
        payload = dict(NEW_CALLER_PAYLOAD, customer_type=segment)
        assert f"Customer type: {display}" in prompts.render_call(payload)


def test_har02_unknown_segment_is_rejected():
    with pytest.raises(ValueError):
        prompts.render_call(dict(NEW_CALLER_PAYLOAD, customer_type="industrial"))


def test_har02_reminder_prompts_render_byte_exact():
    first = prompts.render_reminder(
        {"time": "2026-10-01T10:24:00-07:00", "call_id": 5512, "attempt": 1, "final": False}
    )
    final = prompts.render_reminder(
        {"time": "2026-10-01T10:24:00-07:00", "call_id": 5512, "attempt": 2, "final": True}
    )
    assert first == (
        "[2026-10-01 10:24 America/Phoenix]\n"
        "\n"
        "Call 5512 is still unresolved and the caller is waiting."
    )
    assert final == (
        "[2026-10-01 10:24 America/Phoenix]\n"
        "\n"
        "Final reminder: call 5512 is still unresolved."
    )
    _assert_clean(first)
    _assert_clean(final)


def test_har02_reminder_timestamp_is_the_frozen_call_timestamp():
    call_text = prompts.render_call(NEW_CALLER_PAYLOAD)
    reminder = prompts.render_reminder(
        {"time": NEW_CALLER_PAYLOAD["time"], "call_id": 5512, "attempt": 1}
    )
    assert reminder.splitlines()[0] == call_text.splitlines()[0]


def test_har03_closing_render_is_byte_exact():
    text = prompts.render_closing(
        {"service_date": "2026-10-01", "time": "2026-10-01T19:42:00-07:00"}
    )
    assert text == (
        "[2026-10-01 19:42 America/Phoenix]\n"
        "\n"
        "The last job of the day has finished and the office is closed.\n"
        "Before you finish, you may review the day and update MEMORY.md."
    )


def test_har03_closing_zero_job_day_uses_its_own_timestamp():
    text = prompts.render_closing(
        {"service_date": "2026-10-04", "time": "2026-10-04T19:00:00-07:00"}
    )
    assert text.splitlines()[0] == "[2026-10-04 19:00 America/Phoenix]"
    assert text.endswith("Before you finish, you may review the day and update MEMORY.md.")


def test_har03_closing_instructs_memory_update_and_names_no_reporting_tool():
    text = prompts.render_closing({"time": "2026-10-01T19:42:00-07:00"})
    assert "update MEMORY.md" in text
    assert "submit_daily_report" not in text
    _assert_clean(text)


def test_har03_closing_carries_no_weekday_in_the_stamp():
    text = prompts.render_closing({"time": "2026-10-01T19:42:00-07:00"})
    assert "Thursday" not in text


def test_har01_system_prompt_states_every_mandated_item():
    text = prompts.SYSTEM_PROMPT
    for needle in (
        "owner-operator",
        "Sunridge Services",
        "Phoenix",
        "HVAC",
        "plumbing",
        "electrical",
        "final cash",
        "bankrupt",
        "zero",
        "HTTP API",
        "SUNRIDGE_API_PORT",
        "SUNRIDGE_API_TOKEN",
        "docs/",
        "read-only SQL",
        "public views",
        "workspace",
        "MEMORY.md",
    ):
        assert needle in text, needle


def test_har04_system_prompt_tells_the_agent_to_read_memory_itself():
    assert MEMORY_PARAGRAPH in prompts.SYSTEM_PROMPT
    assert "read back to you automatically each morning" not in prompts.SYSTEM_PROMPT
    assert "only file" not in prompts.SYSTEM_PROMPT


def test_har01_system_prompt_mentions_no_tooling_layer_or_identifiers():
    lowered = prompts.SYSTEM_PROMPT.lower()
    for needle in ("sdk", "trades_bench", "trades-bench", "bench", "csr", "command-line", "seed"):
        assert needle not in lowered, needle


def test_har07_system_prompt_hash_matches_actual_bytes():
    expected = hashlib.sha256(prompts.SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert prompts.system_prompt_hash() == expected
    assert len(prompts.system_prompt_hash()) == 64


def test_har01_prompt_template_version_comes_from_the_engine():
    from trades_bench import invocations

    assert prompts.PROMPT_TEMPLATE_VERSION is invocations.PROMPT_TEMPLATE_VERSION
