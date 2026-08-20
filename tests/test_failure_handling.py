from __future__ import annotations

import pytest

from test_har_equivalence import (
    ONE_DAY,
    call_attempt_rows,
    call_statuses,
    controlled_run,
    harness_paths,
    open_world,
    scripted_config,
)
from trades_bench.harness.runner import CALL_ATTEMPT_LIMIT
from trades_bench.harness.scripted import (
    COMPLETED_EMPTY_REPLY,
    INCOMPLETE_REPLY,
    PROVIDER_FAILURE_REPLY,
    TIMED_OUT_REPLY,
    TRUNCATED_REPLY,
    ScriptedAdapter,
    ScriptedProviderError,
    ScriptedReply,
    SentPrompt,
)
from trades_bench.harness.trajectory import read_all

TIMEOUT_CONFIG_KEYS = {
    "harness.model_retry_limit": 0,
    "harness.model_retry_backoff_seconds": 0,
}


def _run_status(db_path) -> str:
    conn = open_world(db_path)
    status = str(conn.execute("SELECT status FROM _run").fetchone()["status"])
    conn.close()
    return status


def _invocation_rows(db_path) -> list[tuple[int, str, str]]:
    conn = open_world(db_path)
    rows = [
        (int(row["invocation_id"]), str(row["type"]), str(row["status"]))
        for row in conn.execute(
            "SELECT invocation_id, type, status FROM _invocations ORDER BY invocation_id"
        ).fetchall()
    ]
    conn.close()
    return rows


def test_scripted_adapter_raises_provider_failures_without_recording_a_turn():
    adapter = ScriptedAdapter([PROVIDER_FAILURE_REPLY, COMPLETED_EMPTY_REPLY])
    with pytest.raises(ScriptedProviderError):
        adapter.send("prompt", "call", 5.0)
    assert adapter.record.turns == []
    assert len(adapter.prompts) == 1
    turn = adapter.send("prompt", "call", 5.0)
    assert turn.ended_normally
    assert len(adapter.record.turns) == 1


def test_scripted_adapter_reports_a_turn_that_did_not_end_normally():
    adapter = ScriptedAdapter([TRUNCATED_REPLY])
    turn = adapter.send("prompt", "call", 5.0)
    assert turn.ended_normally is False
    assert turn.text == "partial"


def test_scripted_adapter_times_out_when_a_turn_outlives_its_deadline():
    adapter = ScriptedAdapter([ScriptedReply(hold_s=0.05)])
    with pytest.raises(TimeoutError):
        adapter.send("prompt", "call", 0.0)
    assert adapter.record.turns == []


def test_acceptance_37_provider_retries_are_not_attempts(tmp_path):
    failures: dict[tuple[str, str], int] = {}

    def responder(sent: SentPrompt) -> ScriptedReply:
        if sent.kind in ("call", "reminder"):
            key = (sent.kind, sent.text)
            seen = failures.get(key, 0)
            failures[key] = seen + 1
            if seen == 0:
                return PROVIDER_FAILURE_REPLY
            return INCOMPLETE_REPLY
        return COMPLETED_EMPTY_REPLY

    adapter = ScriptedAdapter(responder=responder)
    paths = harness_paths(tmp_path)
    config = scripted_config(ONE_DAY, **{"harness.model_retry_limit": 3})
    controlled_run(adapter, paths, config)

    attempts = call_attempt_rows(paths["db_path"])
    by_call: dict[int, int] = {}
    for call_id, _number, _outcome in attempts:
        by_call[call_id] = by_call.get(call_id, 0) + 1
    assert by_call
    assert set(by_call.values()) == {3}
    assert _run_status(paths["db_path"]) in ("completed", "bankrupt")


def test_acceptance_37_exhausted_retries_fail_the_run_without_skipping_an_invocation(tmp_path):
    def responder(sent: SentPrompt) -> ScriptedReply:
        if sent.kind == "opening":
            return COMPLETED_EMPTY_REPLY
        return PROVIDER_FAILURE_REPLY

    adapter = ScriptedAdapter(responder=responder)
    paths = harness_paths(tmp_path)
    config = scripted_config(
        ONE_DAY,
        **{"harness.model_retry_limit": 1, "harness.model_retry_backoff_seconds": 0},
    )
    failure = None
    try:
        controlled_run(adapter, paths, config)
    except Exception as exc:
        failure = exc
    assert failure is not None

    assert _run_status(paths["db_path"]) == "failed"
    rows = _invocation_rows(paths["db_path"])
    assert rows
    assert [kind for _id, kind, _status in rows][0] == "opening"
    assert sum(1 for _id, _kind, status in rows if status != "completed") <= 1
    assert adapter.record.end_status in (None, "failed")


def test_acceptance_37_a_timed_out_call_turn_counts_as_one_incomplete_attempt(tmp_path):
    def responder(sent: SentPrompt) -> ScriptedReply:
        if sent.kind in ("call", "reminder"):
            return TIMED_OUT_REPLY
        return COMPLETED_EMPTY_REPLY

    adapter = ScriptedAdapter(responder=responder)
    paths = harness_paths(tmp_path)
    controlled_run(adapter, paths, scripted_config(ONE_DAY, **TIMEOUT_CONFIG_KEYS))

    attempts = call_attempt_rows(paths["db_path"])
    assert attempts
    per_call: dict[int, list[tuple[int, str]]] = {}
    for call_id, number, outcome in attempts:
        per_call.setdefault(call_id, []).append((number, outcome))
    assert all(len(rows) == CALL_ATTEMPT_LIMIT for rows in per_call.values())
    assert all(
        rows[:-1] == [(n, "incomplete") for n in range(1, CALL_ATTEMPT_LIMIT)]
        for rows in per_call.values()
    )
    assert {status for _id, status in call_statuses(paths["db_path"])} == {"unanswered"}

    turns_by_invocation: dict[int, list[str]] = {}
    for record in read_all(paths["run_dir"]):
        if record["kind"] == "model_turn":
            turns_by_invocation.setdefault(int(record["invocation_id"]), []).append(
                str(record["stop_reason"])
            )
    call_invocations = [
        invocation_id
        for invocation_id, kind, _status in _invocation_rows(paths["db_path"])
        if kind == "call"
    ]
    assert call_invocations
    assert all(
        turns_by_invocation[invocation_id] == ["invocation_timeout"] * CALL_ATTEMPT_LIMIT
        for invocation_id in call_invocations
    )
    assert _run_status(paths["db_path"]) in ("completed", "bankrupt")


def test_acceptance_37_a_timed_out_opening_completes_the_day(tmp_path):
    def responder(sent: SentPrompt) -> ScriptedReply:
        if sent.kind in ("opening", "closing"):
            return TIMED_OUT_REPLY
        return INCOMPLETE_REPLY

    adapter = ScriptedAdapter(responder=responder)
    paths = harness_paths(tmp_path)
    controlled_run(adapter, paths, scripted_config(ONE_DAY, **TIMEOUT_CONFIG_KEYS))

    assert _run_status(paths["db_path"]) in ("completed", "bankrupt")
    kinds = [kind for _id, kind, _status in _invocation_rows(paths["db_path"])]
    assert "opening" in kinds and "closing" in kinds
    assert all(
        status == "completed" for _id, _kind, status in _invocation_rows(paths["db_path"])
    )
