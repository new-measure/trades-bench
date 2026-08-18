from __future__ import annotations

import json

import pytest

from trades_bench.errors import EngineError, ErrorCode, ok_response
from trades_bench.wire import (
    ENGINE_ERROR_TRANSLATION,
    SPEC_VERSION,
    WIRE_MESSAGE_TEMPLATES,
    WIRE_STATUS,
    WireCode,
    build_failure_envelope,
    build_success_envelope,
    incident_id_for,
    native_failure,
    parse_engine_response,
    translate_error,
    wire_response_for,
)

_SPEC_STATUS_TABLE = {
    WireCode.UNAUTHORIZED: 401,
    WireCode.UNKNOWN_OPERATION: 404,
    WireCode.NO_ACTIVE_SESSION: 409,
    WireCode.ENGINE_BUSY: 503,
    WireCode.DEADLINE_EXCEEDED: 504,
    WireCode.INVALID_REQUEST: 400,
    WireCode.VALIDATION_ERROR: 422,
    WireCode.NOT_FOUND: 404,
    WireCode.CONFLICT: 409,
    WireCode.INSUFFICIENT_CASH: 422,
    WireCode.REQUEST_ID_CONFLICT: 409,
    WireCode.SQL_NOT_PERMITTED: 422,
    WireCode.UNKNOWN_RELATION: 422,
    WireCode.UNKNOWN_COLUMN: 422,
    WireCode.SQL_TIMEOUT: 504,
    WireCode.RUN_NOT_ACTIVE: 409,
    WireCode.INTERNAL_ERROR: 500,
}


def test_wire_code_enum_is_the_closed_seventeen_code_set():
    assert len(list(WireCode)) == 17
    assert set(WireCode) == set(_SPEC_STATUS_TABLE)


def test_every_wire_code_has_a_status_matching_the_spec_table():
    assert dict(WIRE_STATUS) == _SPEC_STATUS_TABLE


def test_every_wire_code_has_a_message_template():
    for code in WireCode:
        assert code in WIRE_MESSAGE_TEMPLATES
        assert isinstance(WIRE_MESSAGE_TEMPLATES[code], str)
        assert WIRE_MESSAGE_TEMPLATES[code]


def test_every_engine_error_code_maps_to_a_wire_code():
    assert set(ENGINE_ERROR_TRANSLATION) == set(ErrorCode)
    for wire_code in ENGINE_ERROR_TRANSLATION.values():
        assert wire_code in WIRE_STATUS
        assert wire_code in WIRE_MESSAGE_TEMPLATES


def test_direct_one_to_one_mappings():
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.UNAUTHORIZED] is WireCode.UNAUTHORIZED
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.INSUFFICIENT_CASH] is WireCode.INSUFFICIENT_CASH
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.REQUEST_ID_CONFLICT] is WireCode.REQUEST_ID_CONFLICT
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.INVALID_RUN_STATUS] is WireCode.RUN_NOT_ACTIVE
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.INVOCATION_CONFLICT] is WireCode.NO_ACTIVE_SESSION
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.ENTITY_NOT_FOUND] is WireCode.NOT_FOUND
    assert ENGINE_ERROR_TRANSLATION[ErrorCode.INVALID_ARGUMENT] is WireCode.INVALID_REQUEST


def test_invocation_conflict_with_already_active_reason_maps_to_conflict_with_that_rule():
    translated = translate_error(
        ErrorCode.INVOCATION_CONFLICT,
        {"reason": "invocation_already_active"},
        request_id="req.1",
    )
    assert translated.wire_code is WireCode.CONFLICT
    assert translated.status == 409
    assert translated.details == {"rule": "invocation_already_active"}


@pytest.mark.parametrize("engine_details", [{"reason": "no_active_invocation"}, {}, None])
def test_invocation_conflict_without_already_active_reason_stays_no_active_session(engine_details):
    translated = translate_error(ErrorCode.INVOCATION_CONFLICT, engine_details, request_id="req.1")
    assert translated.wire_code is WireCode.NO_ACTIVE_SESSION
    assert translated.status == 409


def test_invocation_conflict_full_pipeline_distinguishes_already_active_from_no_active():
    already_active = EngineError(
        ErrorCode.INVOCATION_CONFLICT,
        "an invocation is already active; cannot open a second",
        details={"reason": "invocation_already_active"},
    ).to_response()
    envelope, status = wire_response_for(
        already_active, as_of="2026-10-01T10:24:00-07:00", invocation_id=412, request_id="req.dbl"
    )
    body = json.loads(envelope)
    assert status == 409
    assert body["error"]["code"] == "conflict"
    assert body["error"]["details"] == {"rule": "invocation_already_active"}

    no_active = EngineError(
        ErrorCode.INVOCATION_CONFLICT,
        "command requires an active invocation",
        details={"reason": "no_active_invocation"},
    ).to_response()
    envelope2, status2 = wire_response_for(
        no_active, as_of="2026-10-01T10:24:00-07:00", invocation_id=None, request_id="req.noact"
    )
    body2 = json.loads(envelope2)
    assert status2 == 409
    assert body2["error"]["code"] == "no_active_session"


def test_state_illegal_maps_to_conflict_with_rule_from_reason_detail():
    translated = translate_error(
        ErrorCode.STATE_ILLEGAL, {"reason": "locked_appointment"}, request_id="req.1"
    )
    assert translated.wire_code is WireCode.CONFLICT
    assert translated.status == 409
    assert translated.details == {"rule": "locked_appointment"}
    assert translated.message == WIRE_MESSAGE_TEMPLATES[WireCode.CONFLICT]


def test_closing_restricted_maps_to_conflict_with_fallback_rule_when_no_reason():
    translated = translate_error(ErrorCode.CLOSING_RESTRICTED, {}, request_id="req.1")
    assert translated.wire_code is WireCode.CONFLICT
    assert translated.details == {"rule": ErrorCode.CLOSING_RESTRICTED.value}


def test_address_unresolvable_maps_to_validation_error_with_a_closed_rule():
    translated = translate_error(
        ErrorCode.ADDRESS_UNRESOLVABLE,
        {"canonical_key": "1600 pennsylvania ave|az|85001"},
        request_id="req.1",
    )
    assert translated.wire_code is WireCode.VALIDATION_ERROR
    assert translated.details == {"rule": "address_not_resolvable"}
    assert "canonical_key" not in translated.details
    assert "1600 pennsylvania" not in json.dumps(translated.details)


def test_insufficient_cash_never_carries_priced_detail():
    translated = translate_error(ErrorCode.INSUFFICIENT_CASH, {}, request_id="req.1")
    assert translated.details == {}
    assert translated.message == "insufficient cash"


def test_insufficient_cash_carries_operation_only_when_supplied():
    with_operation = translate_error(
        ErrorCode.INSUFFICIENT_CASH, {}, request_id="req.1", operation="jobs.book"
    )
    assert with_operation.details == {"operation": "jobs.book"}

    without_operation = translate_error(ErrorCode.INSUFFICIENT_CASH, {}, request_id="req.1")
    assert without_operation.details == {}


def test_unexpected_failure_produces_opaque_incident_id_and_drops_exception_type():
    translated = translate_error(
        ErrorCode.UNEXPECTED_FAILURE,
        {"exception_type": "RuntimeError"},
        request_id="req.boom",
    )
    assert translated.wire_code is WireCode.INTERNAL_ERROR
    assert translated.status == 500
    assert set(translated.details) == {"incident_id"}
    assert "RuntimeError" not in json.dumps(translated.details)
    assert "RuntimeError" not in translated.message


def test_incident_id_is_deterministic_per_request_id_and_varies_across_request_ids():
    a1 = incident_id_for("req.boom")
    a2 = incident_id_for("req.boom")
    b = incident_id_for("req.other")
    assert a1 == a2
    assert a1 != b
    assert isinstance(a1, str) and a1


def test_engine_config_and_registry_faults_map_to_internal_error():
    for code in (
        ErrorCode.UNKNOWN_CONFIG_KEY,
        ErrorCode.CONFIG_VALUE_INVALID,
        ErrorCode.CONSTANT_OVERRIDE,
        ErrorCode.DRAW_OUTSIDE_TXN,
        ErrorCode.EVENT_ALREADY_APPLIED,
        ErrorCode.INVARIANT_VIOLATED,
        ErrorCode.DEFERRED_UNRESOLVED,
        ErrorCode.UNCLASSIFIED_FIELD,
        ErrorCode.MIGRATION_FAILED,
        ErrorCode.MIGRATION_CHECKSUM_MISMATCH,
        ErrorCode.VIEW_REGISTRY_MISMATCH,
        ErrorCode.ADDRESS_NORMALIZATION_AMBIGUOUS,
        ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
    ):
        translated = translate_error(code, {"path": "some/internal/path.py"}, request_id="req.x")
        assert translated.wire_code is WireCode.INTERNAL_ERROR
        assert set(translated.details) == {"incident_id"}


def test_build_success_envelope_omits_request_id_when_not_provided():
    envelope = build_success_envelope(
        {"job_id": 8801}, as_of="2026-10-01T10:24:00-07:00", invocation_id=412
    )
    body = json.loads(envelope)
    assert "request_id" not in body["meta"]


def test_build_success_envelope_shape_and_mutual_exclusivity():
    envelope = build_success_envelope(
        {"job_id": 8801}, as_of="2026-10-01T10:24:00-07:00", invocation_id=412, request_id="req.1"
    )
    body = json.loads(envelope)
    assert body.keys() == {"data", "meta"}
    assert body["data"] == {"job_id": 8801}
    assert body["meta"] == {
        "as_of": "2026-10-01T10:24:00-07:00",
        "invocation_id": 412,
        "request_id": "req.1",
        "spec_version": SPEC_VERSION,
    }


def test_build_success_envelope_allows_null_invocation_id():
    envelope = build_success_envelope(
        {}, as_of="2026-10-01T10:24:00-07:00", invocation_id=None, request_id="req.1"
    )
    body = json.loads(envelope)
    assert body["meta"]["invocation_id"] is None


def _reject_non_finite_constant(token):
    raise ValueError(f"non-finite JSON constant in raw bytes: {token}")


def test_build_success_envelope_coerces_non_finite_floats_to_null_recursively():
    envelope = build_success_envelope(
        {
            "total": float("inf"),
            "rows": [[float("-inf"), 1.5], [float("nan"), None]],
        },
        as_of="2026-10-01T10:24:00-07:00",
        invocation_id=412,
        request_id="req.1",
    )
    body = json.loads(envelope, parse_constant=_reject_non_finite_constant)
    assert body["data"] == {"total": None, "rows": [[None, 1.5], [None, None]]}


def test_build_failure_envelope_coerces_non_finite_floats_to_null_recursively():
    translated = native_failure(WireCode.VALIDATION_ERROR, details={"observed": float("nan")})
    envelope = build_failure_envelope(translated, as_of="2026-10-01T10:24:00-07:00")
    body = json.loads(envelope, parse_constant=_reject_non_finite_constant)
    assert body["error"]["details"] == {"observed": None}


def test_build_failure_envelope_shape_and_mutual_exclusivity():
    translated = translate_error(
        ErrorCode.STATE_ILLEGAL, {"reason": "locked_appointment"}, request_id="req.1"
    )
    envelope = build_failure_envelope(translated, as_of="2026-10-01T10:24:00-07:00")
    body = json.loads(envelope)
    assert body.keys() == {"error", "meta"}
    assert body["meta"] == {"as_of": "2026-10-01T10:24:00-07:00"}
    assert body["error"] == {
        "code": "conflict",
        "message": "conflict",
        "details": {"rule": "locked_appointment"},
    }


def test_build_failure_envelope_omits_details_key_when_empty():
    translated = native_failure(WireCode.UNAUTHORIZED)
    envelope = build_failure_envelope(translated, as_of="2026-10-01T10:24:00-07:00")
    body = json.loads(envelope)
    assert "details" not in body["error"]
    assert body["error"] == {"code": "unauthorized", "message": "unauthorized"}


def test_unauthorized_reveals_no_other_information():
    translated = native_failure(WireCode.UNAUTHORIZED, details={"leaked": "nope"})
    assert translated.status == 401
    envelope = build_failure_envelope(translated, as_of="2026-10-01T10:24:00-07:00")
    body = json.loads(envelope)
    assert body["error"] == {"code": "unauthorized", "message": "unauthorized"}


def test_parse_engine_response_round_trips_success_bytes():
    response = ok_response({"booked": True, "entity_id": 7})
    ok, payload = parse_engine_response(response)
    assert ok is True
    assert payload == {"booked": True, "entity_id": 7}


def test_parse_engine_response_round_trips_error_bytes():
    response = EngineError(
        ErrorCode.STATE_ILLEGAL, "call already terminal", details={"reason": "cancellation_call"}
    ).to_response()
    ok, payload = parse_engine_response(response)
    assert ok is False
    engine_code, engine_details = payload
    assert engine_code is ErrorCode.STATE_ILLEGAL
    assert engine_details == {"reason": "cancellation_call"}


def test_wire_response_for_success_full_round_trip():
    response = ok_response({"booked": True})
    envelope, status = wire_response_for(
        response,
        as_of="2026-10-01T10:24:00-07:00",
        invocation_id=412,
        request_id="req.42",
    )
    assert status == 200
    body = json.loads(envelope)
    assert body["data"] == {"booked": True}
    assert body["meta"]["request_id"] == "req.42"
    assert body["meta"]["invocation_id"] == 412
    assert body["meta"]["spec_version"] == SPEC_VERSION


def test_wire_response_for_failure_full_round_trip_translates_status_and_code():
    response = EngineError(ErrorCode.INSUFFICIENT_CASH, "not enough").to_response()
    envelope, status = wire_response_for(
        response,
        as_of="2026-10-01T10:24:00-07:00",
        invocation_id=412,
        request_id="req.43",
    )
    assert status == 422
    body = json.loads(envelope)
    assert body["error"]["code"] == "insufficient_cash"
    assert "details" not in body["error"]
    assert body["meta"] == {"as_of": "2026-10-01T10:24:00-07:00"}


def test_wire_response_for_unexpected_failure_never_leaks_exception_type():
    response = EngineError(
        ErrorCode.UNEXPECTED_FAILURE,
        "an unexpected internal error occurred",
        details={"exception_type": "RuntimeError"},
    ).to_response()
    envelope, status = wire_response_for(
        response,
        as_of="2026-10-01T10:24:00-07:00",
        invocation_id=412,
        request_id="req.44",
    )
    assert status == 500
    assert b"RuntimeError" not in envelope
    body = json.loads(envelope)
    assert set(body["error"]["details"]) == {"incident_id"}


def test_wire_response_for_replayed_bytes_are_deterministic_across_calls():
    response = EngineError(
        ErrorCode.UNEXPECTED_FAILURE,
        "an unexpected internal error occurred",
        details={"exception_type": "RuntimeError"},
    ).to_response()
    envelope1, status1 = wire_response_for(
        response, as_of="2026-10-01T10:24:00-07:00", invocation_id=412, request_id="req.replay"
    )
    envelope2, status2 = wire_response_for(
        response, as_of="2026-10-01T11:00:00-07:00", invocation_id=413, request_id="req.replay"
    )
    body1 = json.loads(envelope1)
    body2 = json.loads(envelope2)
    assert status1 == status2 == 500
    assert body1["error"]["details"]["incident_id"] == body2["error"]["details"]["incident_id"]


@pytest.mark.parametrize("engine_code", list(ErrorCode))
def test_translate_error_never_raises_for_any_known_engine_code(engine_code):
    translated = translate_error(engine_code, {"anything": "goes"}, request_id="req.fuzz")
    assert translated.wire_code in WIRE_STATUS
    assert translated.status == WIRE_STATUS[translated.wire_code]
    assert translated.message == WIRE_MESSAGE_TEMPLATES[translated.wire_code]
