from __future__ import annotations

import json

import pytest

from trades_bench.errors import EngineError, ErrorCode, ok_response


def test_codes_are_stable_strings():
    for code in ErrorCode:
        assert isinstance(code.value, str) and code.value
        assert " " not in code.value


def test_unauthorized_is_uniform_and_secret_free():
    e_real = EngineError(ErrorCode.UNAUTHORIZED, details={"target": "control.settle"})
    e_probe = EngineError(ErrorCode.UNAUTHORIZED, "no such table 007")
    assert e_real.code is e_probe.code is ErrorCode.UNAUTHORIZED
    assert e_real.message == e_probe.message == "unauthorized"
    assert e_real.details == {}
    assert e_probe.details == {}


def test_unauthorized_factory_is_uniform():
    e = EngineError.unauthorized()
    assert e.code is ErrorCode.UNAUTHORIZED
    assert e.message == "unauthorized"
    assert e.details == {}


def test_error_response_bytes_are_stable_and_sorted():
    a = EngineError(ErrorCode.INSUFFICIENT_CASH, "not enough", details={"b": 1, "a": 2})
    b = EngineError(ErrorCode.INSUFFICIENT_CASH, "not enough", details={"a": 2, "b": 1})
    assert a.to_response() == b.to_response()
    payload = json.loads(a.to_response())
    assert payload == {
        "ok": False,
        "error": {
            "code": "insufficient_cash",
            "message": "not enough",
            "details": {"a": 2, "b": 1},
        },
    }
    assert EngineError.unauthorized().to_response() == (
        b'{"error":{"code":"unauthorized","message":"unauthorized"},"ok":false}'
    )


def test_ok_response_canonical_and_float_free():
    from decimal import Decimal

    payload = {"z": 1, "a": Decimal("0.5"), "b": [3, 2, 1]}
    raw = ok_response(payload)
    dec = json.loads(raw)
    assert dec == {"ok": True, "result": {"a": "0.5", "b": [3, 2, 1], "z": 1}}
    assert "e" not in dec["result"]["a"].lower()


def test_error_inherits_exception():
    with pytest.raises(EngineError) as ei:
        raise EngineError(ErrorCode.STATE_ILLEGAL)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    assert ei.value.message == "state_illegal"
