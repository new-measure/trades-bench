from __future__ import annotations

# Work out which artifacts a run folder has and where each piece of the replay comes from.
# A full harness run has everything; a Hugging Face run has pi-logs + policy-report.json, and
# gains a `rebuilt/` folder (run.db, memory history) once rebuild_run.py has been run on it.

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REBUILT_DIRNAME = "rebuilt"
VERIFICATION_FILENAME = "verification.json"
MEMORY_HISTORY_FILENAME = "memory-history.jsonl"

RECORDED = "recorded"
REBUILT = "rebuilt"
MISSING = "missing"

_OPTIONAL = {
    "index": "index.jsonl",
    "days": "days",
    "pi_sessions": "pi-sessions",
    "workspace": "workspace",
    "usage": "pi-usage.json",
    "engine_logs": "engine.stdout.log",
}


@dataclass(frozen=True)
class Artifact:

    name: str
    origin: str
    path: Path | None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "origin": self.origin, "path": str(self.path) if self.path else None, "note": self.note}


@dataclass(frozen=True)
class RunSources:

    run_dir: Path
    report: Path | None
    pi_logs: Path | None
    world_db: Path | None
    world_origin: str
    world_verified: bool | None
    memory_history: Path | None
    optional: dict[str, Path]

    def artifacts(self) -> list[Artifact]:
        rows = [
            Artifact("policy-report.json", RECORDED if self.report else MISSING, self.report),
            Artifact("pi-logs/", RECORDED if self.pi_logs else MISSING, self.pi_logs),
            Artifact(
                "run.db",
                self.world_origin,
                self.world_db,
                {True: "dump_hash matches policy report", False: "dump_hash does NOT match", None: "not verified"}[
                    self.world_verified
                ],
            ),
            Artifact(
                "MEMORY.md history",
                REBUILT if self.memory_history else MISSING,
                self.memory_history,
                "" if self.memory_history else "approximated from edit/write tool calls",
            ),
        ]
        for name, filename in _OPTIONAL.items():
            path = self.optional.get(name)
            rows.append(Artifact(filename, RECORDED if path else MISSING, path))
        return rows


def detect(run_dir: str | Path) -> RunSources:
    run_dir = Path(run_dir).resolve()
    rebuilt = run_dir / REBUILT_DIRNAME
    report = run_dir / "policy-report.json"
    pi_logs = run_dir / "pi-logs"

    world_db: Path | None = None
    world_origin = MISSING
    world_verified: bool | None = None
    if (run_dir / "run.db").exists():
        world_db, world_origin = run_dir / "run.db", RECORDED
    elif (rebuilt / "run.db").exists():
        world_db, world_origin = rebuilt / "run.db", REBUILT
        verification = rebuilt / VERIFICATION_FILENAME
        if verification.exists():
            world_verified = bool(json.loads(verification.read_text(encoding="utf-8")).get("verified"))

    memory_history = rebuilt / MEMORY_HISTORY_FILENAME
    return RunSources(
        run_dir=run_dir,
        report=report if report.exists() else None,
        pi_logs=pi_logs if pi_logs.is_dir() else None,
        world_db=world_db,
        world_origin=world_origin,
        world_verified=world_verified,
        memory_history=memory_history if memory_history.exists() else None,
        optional={name: run_dir / f for name, f in _OPTIONAL.items() if (run_dir / f).exists()},
    )


__all__ = ["Artifact", "MISSING", "REBUILT", "RECORDED", "RunSources", "detect"]
