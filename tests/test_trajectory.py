from __future__ import annotations

import json

import pytest

from trades_bench.harness import trajectory

RUN_ID = "run-abc"
SIM_TS = "2026-10-01T10:24:00-07:00"


def _writer(tmp_path):
    return trajectory.TrajectoryWriter(tmp_path / "runs" / RUN_ID, RUN_ID)


def _emit_every_kind(writer):
    writer.control_request(operation="next_invocation", body={"request_id": "next-invocation-1"})
    writer.control_response(
        operation="next_invocation",
        status=200,
        body={"invocation_id": 7, "kind": "call", "ts_ms": 1},
    )
    writer.open_day("2026-10-01")
    writer.world_event_applied(
        invocation_id=7, simulated_ts=SIM_TS, event_id=41, event_kind="call_arrival", event_ts_ms=1
    )
    writer.prompt_issued(
        invocation_id=7, simulated_ts=SIM_TS, prompt_kind="call", text="[stamp]\n\nIncoming call"
    )
    writer.model_turn(
        invocation_id=7,
        simulated_ts=SIM_TS,
        text="thinking",
        tool_calls=[{"name": "crm.create_customer"}],
        input_tokens=120,
        output_tokens=45,
        stop_reason="end_turn",
        ended_normally=True,
    )
    writer.attempt_recorded(invocation_id=7, simulated_ts=SIM_TS, call_id=5512, attempt_number=1)
    writer.reminder_issued(
        invocation_id=7,
        simulated_ts=SIM_TS,
        call_id=5512,
        attempt_number=1,
        final=False,
        text="reminder",
    )
    writer.checkpoint(
        invocation_id=7,
        simulated_time=SIM_TS,
        kind="call",
        engine_checkpoint_id=41,
        workspace_digest="0" * 64,
    )
    writer.close_day()
    writer.run_outcome(run_status="completed")


def test_every_record_kind_is_produced(tmp_path):
    writer = _writer(tmp_path)
    _emit_every_kind(writer)
    kinds = {record["kind"] for record in trajectory.read_all(writer.run_dir)}
    assert kinds == set(trajectory.RECORD_KINDS)


def test_every_record_carries_the_common_fields(tmp_path):
    writer = _writer(tmp_path)
    _emit_every_kind(writer)
    for record in trajectory.read_all(writer.run_dir):
        assert record["run_id"] == RUN_ID
        assert "simulated_ts" in record
        assert record["wall_ts"]
        assert "invocation_id" in record


def test_records_land_in_the_day_file_while_a_day_is_open(tmp_path):
    writer = _writer(tmp_path)
    _emit_every_kind(writer)
    day_records = trajectory.read_day(writer.run_dir, "2026-10-01")
    assert [record["kind"] for record in day_records] == [
        "world_event_applied",
        "prompt_issued",
        "model_turn",
        "attempt_recorded",
        "reminder_issued",
        "checkpoint",
    ]
    index_kinds = [record["kind"] for record in trajectory.read_index(writer.run_dir)]
    assert index_kinds == ["control_request", "control_response", "run_outcome"]


def test_one_file_per_simulated_day(tmp_path):
    writer = _writer(tmp_path)
    for day in ("2026-10-01", "2026-10-02"):
        writer.open_day(day)
        writer.prompt_issued(
            invocation_id=1, simulated_ts=f"{day}T06:45:00-07:00", prompt_kind="opening", text="x"
        )
    writer.close_day()
    assert [path.name for path in writer.day_files()] == [
        "2026-10-01.jsonl",
        "2026-10-02.jsonl",
    ]


def test_prompt_issued_records_the_template_version(tmp_path):
    writer = _writer(tmp_path)
    writer.open_day("2026-10-01")
    record = writer.prompt_issued(
        invocation_id=3, simulated_ts=SIM_TS, prompt_kind="opening", text="hello"
    )
    assert record["prompt_template_version"] == trajectory.PROMPT_TEMPLATE_VERSION


def test_checkpoint_record_carries_the_pinned_fields(tmp_path):
    writer = _writer(tmp_path)
    writer.open_day("2026-10-01")
    record = writer.checkpoint(
        invocation_id=9,
        simulated_time=SIM_TS,
        kind="closing",
        engine_checkpoint_id=88,
        workspace_digest="a" * 64,
    )
    assert record["run_id"] == RUN_ID
    assert record["invocation_id"] == 9
    assert record["invocation_kind"] == "closing"
    assert record["simulated_time"] == SIM_TS
    assert record["engine_checkpoint_id"] == 88
    assert record["workspace_digest"] == "a" * 64


def test_control_traffic_never_records_tokens(tmp_path):
    writer = _writer(tmp_path)
    writer.control_request(
        operation="next_invocation",
        body={
            "request_id": "next-invocation-1",
            "control_token": "secret",
            "headers": {"Authorization": "Bearer secret"},
        },
    )
    text = (writer.run_dir / trajectory.INDEX_FILENAME).read_text(encoding="utf-8")
    assert "secret" not in text
    body = json.loads(text.splitlines()[0])["body"]
    assert body == {"request_id": "next-invocation-1", "headers": {}}


def test_scrub_walks_nested_sequences():
    scrubbed = trajectory.scrub({"items": [{"api_token": "x", "keep": 1}]})
    assert scrubbed == {"items": [{"keep": 1}]}


def test_unknown_record_kind_is_rejected(tmp_path):
    writer = _writer(tmp_path)
    with pytest.raises(ValueError):
        writer._write("day_started", SIM_TS, None, {})


def test_lines_are_valid_jsonl(tmp_path):
    writer = _writer(tmp_path)
    _emit_every_kind(writer)
    for path in [writer.index_path, *writer.day_files()]:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)
            assert "\n" not in line


def test_count_control_requests_supports_resume_ordinals(tmp_path):
    writer = _writer(tmp_path)
    for n in (1, 2, 3):
        writer.control_request(operation="next_invocation", body={"request_id": f"next-{n}"})
    writer.control_request(operation="complete_invocation", body={"request_id": "c-1"})
    records = trajectory.read_all(writer.run_dir)
    assert trajectory.count_control_requests(records, "next_invocation") == 3
    assert trajectory.count_control_requests(records, "complete_invocation") == 1


def test_run_outcome_is_a_run_level_record(tmp_path):
    writer = _writer(tmp_path)
    writer.open_day("2026-10-01")
    record = writer.run_outcome(run_status="failed", detail="model retries exhausted")
    assert record["invocation_id"] is None
    assert trajectory.read_day(writer.run_dir, "2026-10-01") == []
    assert trajectory.read_index(writer.run_dir)[0]["run_status"] == "failed"
    assert writer.service_date == "2026-10-01"
