from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trades_bench.harness.prompts import PROMPT_TEMPLATE_VERSION

INDEX_FILENAME = "index.jsonl"
DAYS_DIRNAME = "days"

RECORD_KINDS = (
    "prompt_issued",
    "model_turn",
    "control_request",
    "control_response",
    "attempt_recorded",
    "reminder_issued",
    "world_event_applied",
    "checkpoint",
    "run_outcome",
)

_SECRET_KEY_MARKERS = ("token", "authorization", "secret", "password")


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


def scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: scrub(v) for k, v in value.items() if not _is_secret_key(str(k))}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


class TrajectoryWriter:
    def __init__(self, run_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.days_dir = self.run_dir / DAYS_DIRNAME
        self.index_path = self.run_dir / INDEX_FILENAME
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.days_dir.mkdir(parents=True, exist_ok=True)
        self._service_date: str | None = None

    @property
    def service_date(self) -> str | None:
        return self._service_date

    def open_day(self, service_date: str) -> Path:
        self._service_date = service_date
        path = self.day_path(service_date)
        path.touch()
        return path

    def close_day(self) -> None:
        self._service_date = None

    def day_path(self, service_date: str) -> Path:
        return self.days_dir / f"{service_date}.jsonl"

    def day_files(self) -> list[Path]:
        return sorted(self.days_dir.glob("*.jsonl"))

    def _target(self) -> Path:
        if self._service_date is None:
            return self.index_path
        return self.day_path(self._service_date)

    def _write(
        self,
        kind: str,
        simulated_ts: str | None,
        invocation_id: int | None,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        if kind not in RECORD_KINDS:
            raise ValueError(f"unknown trajectory record kind {kind!r}")
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "kind": kind,
            "simulated_ts": simulated_ts,
            "wall_ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            "invocation_id": invocation_id,
        }
        record.update(scrub(dict(fields)))
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._target().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return record

    def prompt_issued(
        self,
        *,
        invocation_id: int | None,
        simulated_ts: str,
        prompt_kind: str,
        text: str,
        prompt_template_version: str = PROMPT_TEMPLATE_VERSION,
    ) -> dict[str, Any]:
        return self._write(
            "prompt_issued",
            simulated_ts,
            invocation_id,
            {
                "prompt_kind": prompt_kind,
                "prompt_template_version": prompt_template_version,
                "text": text,
            },
        )

    def model_turn(
        self,
        *,
        invocation_id: int | None,
        simulated_ts: str,
        text: str,
        tool_calls: Sequence[Mapping[str, Any]] = (),
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        stop_reason: str | None = None,
        ended_normally: bool = True,
    ) -> dict[str, Any]:
        return self._write(
            "model_turn",
            simulated_ts,
            invocation_id,
            {
                "text": text,
                "tool_calls": [dict(call) for call in tool_calls],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "stop_reason": stop_reason,
                "ended_normally": ended_normally,
            },
        )

    def control_request(
        self,
        *,
        operation: str,
        body: Mapping[str, Any],
        invocation_id: int | None = None,
        simulated_ts: str | None = None,
    ) -> dict[str, Any]:
        return self._write(
            "control_request",
            simulated_ts,
            invocation_id,
            {"operation": operation, "body": dict(body)},
        )

    def control_response(
        self,
        *,
        operation: str,
        status: int,
        body: Mapping[str, Any],
        invocation_id: int | None = None,
        simulated_ts: str | None = None,
        replayed: bool = False,
    ) -> dict[str, Any]:
        return self._write(
            "control_response",
            simulated_ts,
            invocation_id,
            {
                "operation": operation,
                "status": status,
                "body": dict(body),
                "replayed": replayed,
            },
        )

    def attempt_recorded(
        self,
        *,
        invocation_id: int | None,
        simulated_ts: str,
        call_id: int,
        attempt_number: int,
        final: bool = False,
    ) -> dict[str, Any]:
        return self._write(
            "attempt_recorded",
            simulated_ts,
            invocation_id,
            {"call_id": call_id, "attempt_number": attempt_number, "final": final},
        )

    def reminder_issued(
        self,
        *,
        invocation_id: int | None,
        simulated_ts: str,
        call_id: int,
        attempt_number: int,
        final: bool,
        text: str,
    ) -> dict[str, Any]:
        return self._write(
            "reminder_issued",
            simulated_ts,
            invocation_id,
            {
                "call_id": call_id,
                "attempt_number": attempt_number,
                "final": final,
                "text": text,
            },
        )

    def world_event_applied(
        self,
        *,
        invocation_id: int | None,
        simulated_ts: str | None,
        event_id: int,
        event_kind: str,
        event_ts_ms: int,
    ) -> dict[str, Any]:
        return self._write(
            "world_event_applied",
            simulated_ts,
            invocation_id,
            {"event_id": event_id, "event_kind": event_kind, "event_ts_ms": event_ts_ms},
        )

    def checkpoint(
        self,
        *,
        invocation_id: int,
        simulated_time: str,
        kind: str,
        engine_checkpoint_id: int | None,
        workspace_digest: str,
    ) -> dict[str, Any]:
        return self._write(
            "checkpoint",
            simulated_time,
            invocation_id,
            {
                "invocation_kind": kind,
                "simulated_time": simulated_time,
                "engine_checkpoint_id": engine_checkpoint_id,
                "workspace_digest": workspace_digest,
            },
        )

    def run_outcome(
        self,
        *,
        run_status: str,
        detail: str | None = None,
        simulated_ts: str | None = None,
    ) -> dict[str, Any]:
        previous = self._service_date
        self._service_date = None
        try:
            return self._write(
                "run_outcome",
                simulated_ts,
                None,
                {"run_status": run_status, "detail": detail},
            )
        finally:
            self._service_date = previous


def read_records(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def read_day(run_dir: str | Path, service_date: str) -> list[dict[str, Any]]:
    return read_records(Path(run_dir) / DAYS_DIRNAME / f"{service_date}.jsonl")


def read_index(run_dir: str | Path) -> list[dict[str, Any]]:
    return read_records(Path(run_dir) / INDEX_FILENAME)


def read_all(run_dir: str | Path) -> list[dict[str, Any]]:
    directory = Path(run_dir)
    records = read_index(directory)
    for path in sorted((directory / DAYS_DIRNAME).glob("*.jsonl")):
        records.extend(read_records(path))
    return records


def count_control_requests(records: Iterable[Mapping[str, Any]], operation: str) -> int:
    return sum(
        1
        for record in records
        if record.get("kind") == "control_request" and record.get("operation") == operation
    )


__all__ = [
    "DAYS_DIRNAME",
    "INDEX_FILENAME",
    "RECORD_KINDS",
    "TrajectoryWriter",
    "count_control_requests",
    "read_all",
    "read_day",
    "read_index",
    "read_records",
    "scrub",
]
