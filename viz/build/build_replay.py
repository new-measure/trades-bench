from __future__ import annotations

# Turn a run folder into viz/data/<label>/replay.json for the viewer.
#   Office: every agent turn (prompt, reasoning, tool calls, decision) + MEMORY.md versions.
#   Field:  technicians, jobs, appointment events, drives and ledger from run.db.

import argparse
import difflib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sources as run_sources  # noqa: E402

VIZ = Path(__file__).resolve().parents[1]
REPO = VIZ.parent
DATA_DIR = VIZ / "app" / "public" / "data"
ZONES_GEOJSON = REPO / "data/geography-arizona-v1/zones/zone_definitions.geojson"
PHOENIX = timezone(timedelta(hours=-7))
OUTPUT_LIMIT = 2400
THINKING_LIMIT = 4000
LEARNING_MOMENTS = 6
TECH_COLORS = ("#3b82f6", "#f59e0b", "#10b981", "#ec4899", "#8b5cf6", "#06b6d4", "#ef4444", "#84cc16")
MUTATIONS = {
    "jobs.book": "book",
    "calls.reject": "reject",
    "appointments.acknowledge_cancellation": "acknowledge",
    "dispatch.assign": "assign",
    "crm.create_customer": "create_customer",
    "crm.create_location": "create_location",
}
_HEADER = re.compile(r"\[(?:[A-Za-z]+, )?(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")


def _ms(day: str, hhmm: str) -> int:
    local = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=PHOENIX)
    return int(local.timestamp() * 1000)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more chars]"


def _prompt_fields(prompt: str) -> dict[str, str]:
    fields = {}
    for line in prompt.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key and len(key) < 32 and not key.startswith("["):
            fields[key.strip().lower().replace(" ", "_")] = value.strip()
    return fields


def _result_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    return "".join(p.get("text", "") for p in result.get("content") or () if isinstance(p, Mapping))


def _tool_summary(tool: str, args: Mapping[str, Any]) -> str:
    if tool == "bash":
        return str(args.get("command", ""))
    if tool == "edit":
        edits = args.get("edits") or [{"oldText": args.get("oldText"), "newText": args.get("newText")}]
        return f"edit {args.get('path')} ({len(edits)} change{'s' if len(edits) != 1 else ''})"
    if tool == "write":
        return f"write {args.get('path')} ({len(str(args.get('content', '')))} chars)"
    return f"{tool} {args.get('path', '')}"


def _ops_in(text: str) -> list[str]:
    return [op for op in MUTATIONS if op in text]


def read_turns(pi_logs: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    last_ms = 0
    for meta_path in sorted(pi_logs.glob("*.turn.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stem = meta_path.name[: -len(".turn.json")]
        prompt = (pi_logs / f"{stem}.prompt.txt").read_text(encoding="utf-8")
        header = _HEADER.search(prompt)
        sim_ms = _ms(*header.groups()) if header else last_ms
        last_ms = sim_ms
        steps: list[dict[str, Any]] = []
        by_call: dict[str, dict[str, Any]] = {}
        for line in (pi_logs / f"{stem}.stdout.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            kind = event.get("type")
            if kind == "message_end" and event["message"].get("role") == "assistant":
                for part in event["message"].get("content") or ():
                    if part.get("type") == "thinking" and part.get("thinking", "").strip():
                        steps.append({"type": "think", "text": _clip(part["thinking"].strip(), THINKING_LIMIT)})
                    elif part.get("type") == "text" and part.get("text", "").strip():
                        steps.append({"type": "say", "text": part["text"].strip()})
            elif kind == "tool_execution_start":
                step = {
                    "type": "tool",
                    "tool": event["toolName"],
                    "input": _clip(_tool_summary(event["toolName"], event.get("args") or {}), OUTPUT_LIMIT),
                    "ops": _ops_in(json.dumps(event.get("args") or {})),
                    "output": "",
                    "error": False,
                }
                if event["toolName"] in ("edit", "write") and str((event.get("args") or {}).get("path", "")).endswith("MEMORY.md"):
                    step["memory"] = True
                steps.append(step)
                by_call[event["toolCallId"]] = step
            elif kind == "tool_execution_end" and event["toolCallId"] in by_call:
                step = by_call[event["toolCallId"]]
                step["output"] = _clip(_result_text(event.get("result")), OUTPUT_LIMIT)
                step["error"] = bool(event.get("isError"))
        says = [s["text"] for s in steps if s["type"] == "say"]
        fields = _prompt_fields(prompt)
        turns.append(
            {
                "id": len(turns),
                "stem": stem,
                "date": meta["service_date"],
                "kind": meta["kind"],
                "sim_ms": sim_ms,
                "prompt": prompt,
                "call_id": int(fields["call_id"]) if fields.get("call_id", "").isdigit() else None,
                "fields": fields,
                "steps": steps,
                "summary": says[-1] if says else "",
                "duration_s": meta.get("duration_s"),
                "output_tokens": meta.get("output_tokens"),
                "timed_out": bool(meta.get("timed_out")),
            }
        )
    return turns


def _memory_entry(turn_id: int, action_index: int | None, previous: str, content: str) -> dict[str, Any]:
    old, new = previous.splitlines(), content.splitlines()
    added, removed = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(line for line in old[i1:i2] if line.strip())
        if tag in ("replace", "insert"):
            added.extend(line for line in new[j1:j2] if line.strip())
    return {"turn": turn_id, "action_index": action_index, "content": content, "added": added, "removed": removed}


def memory_from_history(history: Path, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turn_ids = {t["stem"]: t["id"] for t in turns}
    entries, previous = [], ""
    for line in history.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        entries.append(_memory_entry(turn_ids[record["turn"]], record["action_index"], previous, record["content"]))
        previous = record["content"]
    return entries


def memory_from_tools(pi_logs: Path, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Approximation when no rebuild exists: only edit/write tool calls are applied.
    entries, content = [], ""
    for turn in turns:
        action_index = -1
        for line in (pi_logs / f"{turn['stem']}.stdout.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("type") != "tool_execution_start":
                continue
            action_index += 1
            if event["toolName"] not in ("edit", "write"):
                continue
            args = event.get("args") or {}
            if not str(args.get("path", "")).endswith("MEMORY.md"):
                continue
            updated = content
            if event["toolName"] == "write":
                updated = str(args.get("content", ""))
            else:
                for edit in args.get("edits") or [{"oldText": args.get("oldText"), "newText": args.get("newText")}]:
                    if edit.get("oldText") and edit["oldText"] in updated:
                        updated = updated.replace(edit["oldText"], edit.get("newText") or "", 1)
            if updated != content:
                entries.append(_memory_entry(turn["id"], action_index, content, updated))
                content = updated
    return entries


def link_memory_steps(turns: list[dict[str, Any]], memory: list[dict[str, Any]]) -> None:
    # Mark the tool call that produced each notebook version so the transcript can show the diff inline.
    for index, entry in enumerate(memory):
        if entry["action_index"] is None:
            continue
        tools = [step for step in turns[entry["turn"]]["steps"] if step["type"] == "tool"]
        if entry["action_index"] < len(tools):
            tools[entry["action_index"]]["memory_entry"] = index


def _rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def read_world(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        techs = _rows(conn, "SELECT id, name, city, home_latitude AS lat, home_longitude AS lon, home_zone_id, hourly_wage_cents FROM _technicians ORDER BY id")
        skills = defaultdict(list)
        for row in _rows(conn, "SELECT technician_id, skill_code FROM _technician_skills WHERE active=1 ORDER BY skill_code"):
            skills[row["technician_id"]].append(row["skill_code"])
        for index, tech in enumerate(techs):
            tech["skills"] = skills[tech["id"]]
            tech["color"] = TECH_COLORS[index % len(TECH_COLORS)]

        calls = _rows(
            conn,
            """SELECT c.id, c.arrived_at, c.service_date, c.call_kind, c.status, c.terminal_reason, c.caller_name,
                      c.segment, c.requested_service_name, c.urgent, c.created_job_id, c.target_appointment_id,
                      c.service_street, c.service_city, c.service_postal_code,
                      COALESCE(l.latitude, a.latitude) AS lat, COALESCE(l.longitude, a.longitude) AS lon
               FROM _calls c
               LEFT JOIN _locations l ON l.id = c.location_id
               LEFT JOIN _address_catalog a ON a.id = c.address_catalog_id
               ORDER BY c.id""",
        )

        jobs = _rows(
            conn,
            """SELECT j.id, j.job_number, j.call_id, j.status, j.outcome, j.priority, j.no_charge,
                      jt.code AS job_type, c.name AS customer, c.segment,
                      l.street, l.city, l.postal_code, l.latitude AS lat, l.longitude AS lon, l.zone_id,
                      i.total_cents AS revenue_cents, i.material_cost_cents
               FROM _jobs j
               JOIN _job_types jt ON jt.id = j.job_type_id
               JOIN _customers c ON c.id = j.customer_id
               JOIN _locations l ON l.id = j.location_id
               LEFT JOIN _invoices i ON i.job_id = j.id
               ORDER BY j.id""",
        )
        appointments = _rows(
            conn,
            """SELECT a.id, a.job_id, a.status, a.scheduled_start, a.scheduled_end, a.arrival_window_start,
                      a.arrival_window_end, a.actual_start, a.actual_end, a.cancelled_at, a.created_at,
                      (SELECT technician_id FROM _appointment_assignments x WHERE x.appointment_id = a.id
                       ORDER BY (x.unassigned_at IS NULL) DESC, x.id DESC LIMIT 1) AS technician_id
               FROM _appointments a ORDER BY a.id""",
        )
        events = _rows(
            conn,
            "SELECT appointment_id, technician_id, event_type AS type, occurred_at AS ts, detail FROM _appointment_events ORDER BY occurred_at, id",
        )
        drives = _rows(
            conn,
            """SELECT technician_id AS tech, movement_kind AS kind, origin_latitude AS lat0, origin_longitude AS lon0,
                      destination_latitude AS lat1, destination_longitude AS lon1, departed_at AS t0, arrives_at AS t1
               FROM _technician_movements ORDER BY departed_at, id""",
        )
        shifts = _rows(
            conn,
            "SELECT technician_id AS tech, shift_type, start_at AS t0, end_at AS t1, service_date, on_call_rotation_id FROM _technician_shifts WHERE active=1 ORDER BY start_at",
        )
        ledger = _rows(
            conn,
            "SELECT occurred_at AS ts, service_date, entry_type AS type, amount_cents, balance_after_cents, job_id, technician_id, description FROM _ledger_entries ORDER BY occurred_at, id",
        )
        run = dict(conn.execute("SELECT * FROM _run").fetchone())
    finally:
        conn.close()
    return {
        "run": run,
        "technicians": techs,
        "calls": calls,
        "jobs": jobs,
        "appointments": appointments,
        "events": events,
        "drives": drives,
        "shifts": shifts,
        "ledger": ledger,
    }


def attach_decisions(turns: list[dict[str, Any]], world: dict[str, Any] | None) -> None:
    calls = {c["id"]: c for c in world["calls"]} if world else {}
    jobs = {j["id"]: j for j in world["jobs"]} if world else {}
    appts_by_job: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for appt in world["appointments"] if world else ():
        appts_by_job[appt["job_id"]].append(appt)
    techs = {t["id"]: t["name"] for t in world["technicians"]} if world else {}
    settled: set[int] = set()
    for turn in turns:
        call_id = turn["call_id"]
        ops = [op for step in turn["steps"] if step["type"] == "tool" and not step["error"] for op in step["ops"]]
        turn["ops"] = sorted(set(ops), key=ops.index)
        if call_id is None or call_id in settled:
            continue
        call = calls.get(call_id)
        if call is None:
            if any(op in ops for op in ("jobs.book", "calls.reject", "appointments.acknowledge_cancellation")):
                turn["decision"] = {"type": "attempted", "ops": turn["ops"]}
            continue
        reason = call["terminal_reason"]
        decision: dict[str, Any] = {
            "type": {
                "accepted": "booked",
                "customer_declined": "lost",
                "agent_rejected": "rejected",
                "cancellation_acknowledged": "cancel_ack",
                "unanswered": "unanswered",
            }.get(reason or "", "pending"),
            "call_status": call["status"],
        }
        job = jobs.get(call["created_job_id"]) if call["created_job_id"] else None
        if job:
            appt = (appts_by_job.get(job["id"]) or [None])[0]
            decision.update(job_id=job["id"], job_type=job["job_type"])
            if appt:
                decision.update(
                    appointment_id=appt["id"],
                    technician=techs.get(appt["technician_id"]),
                    technician_id=appt["technician_id"],
                    scheduled_start=appt["scheduled_start"],
                    scheduled_end=appt["scheduled_end"],
                )
        if call["target_appointment_id"]:
            decision["appointment_id"] = call["target_appointment_id"]
        turn["decision"] = decision
        if reason:
            settled.add(call_id)


def find_moments(turns: list[dict[str, Any]], memory: list[dict[str, Any]], report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    moments = []
    for turn in turns:
        decision = turn.get("decision") or {}
        if decision.get("type") == "lost" and turn["fields"].get("urgent", "").lower().startswith("y"):
            moments.append({"sim_ms": turn["sim_ms"], "turn": turn["id"], "kind": "bad", "title": f"Lost urgent call {turn['call_id']}"})
        if turn["timed_out"]:
            moments.append({"sim_ms": turn["sim_ms"], "turn": turn["id"], "kind": "warn", "title": "Turn hit the 15-minute timeout"})
    turn_ms = {t["id"]: t["sim_ms"] for t in turns}
    rewrites = [
        entry
        for entry in memory
        if len([line for line in entry["removed"] if len(line) > 30]) >= 2
        and any(re.search(r"\bwrong\b|mistake|correction|was incorrect|\bNOT\b", l) for l in entry["added"])
    ]
    for entry in sorted(rewrites, key=lambda e: -len(e["removed"]))[:LEARNING_MOMENTS]:
        headline = next((l.strip("#*- ") for l in entry["added"] if re.search(r"\bwrong\b|mistake|correction|was incorrect|\bNOT\b", l)), "")
        moments.append({"sim_ms": turn_ms[entry["turn"]], "turn": entry["turn"], "kind": "learn", "title": f"Rewrote its notes: {headline[:120]}"})
    if report and report.get("daily_cash_cents"):
        cash = [(d, c) for d, c in report["daily_cash_cents"]]
        start = report["starting_cash_cents"]
        nets = [(d, c - (cash[i - 1][1] if i else start)) for i, (d, c) in enumerate(cash)]
        best = max(nets, key=lambda n: n[1])
        worst = min(nets, key=lambda n: n[1])
        moments.append({"sim_ms": _ms(best[0], "19:00"), "kind": "good", "title": f"Best day {best[0]}: {best[1] / 100:+,.0f}"})
        moments.append({"sim_ms": _ms(worst[0], "19:00"), "kind": "bad", "title": f"Worst day {worst[0]}: {worst[1] / 100:+,.0f}"})
    return sorted(moments, key=lambda m: m["sim_ms"])


def verify_recorded_db(db_path: Path, report: Mapping[str, Any]) -> bool:
    sys.path.insert(0, str(REPO / "src"))
    from trades_bench._dump import dump_hash

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run_id = conn.execute("SELECT run_id FROM _run").fetchone()[0]
        return dump_hash(conn, run_id) == report.get("dump_hash")
    finally:
        conn.close()


def run_label(run_dir: Path) -> str:
    parts = run_dir.resolve().parts
    tail = parts[parts.index("runs") + 1 :] if "runs" in parts else parts[-2:]
    return "-".join(tail)


def build(run_dir: Path, *, verify: bool) -> Path:
    found = run_sources.detect(run_dir)
    if found.pi_logs is None:
        raise SystemExit(f"{run_dir} has no pi-logs/; nothing to show in the office")
    report = json.loads(found.report.read_text(encoding="utf-8")) if found.report else None
    world_verified = found.world_verified
    if found.world_origin == run_sources.RECORDED and verify and report:
        world_verified = verify_recorded_db(found.world_db, report)

    turns = read_turns(found.pi_logs)
    world = read_world(found.world_db) if found.world_db else None
    attach_decisions(turns, world)
    if found.memory_history:
        memory, memory_exact = memory_from_history(found.memory_history, turns), True
    else:
        memory, memory_exact = memory_from_tools(found.pi_logs, turns), False
    link_memory_steps(turns, memory)

    artifacts = [a.as_dict() for a in found.artifacts()]
    for artifact in artifacts:
        if artifact["name"] == "run.db" and found.world_db:
            artifact["note"] = {True: "dump_hash matches policy report", False: "dump_hash does NOT match", None: "not verified"}[world_verified]
        if artifact["path"]:
            artifact["path"] = str(Path(artifact["path"]).relative_to(REPO)) if Path(artifact["path"]).is_relative_to(REPO) else artifact["path"]

    label = run_label(run_dir)
    replay = {
        "label": label,
        "policy": report.get("policy") if report else None,
        "report": {k: v for k, v in (report or {}).items() if k not in ("dump_hash", "config_hash")},
        "sources": {"artifacts": artifacts, "world_verified": world_verified, "memory_exact": memory_exact},
        "turns": turns,
        "memory": memory,
        "world": {k: v for k, v in world.items() if k != "run"} if world else None,
        "moments": find_moments(turns, memory, report),
    }
    out_dir = DATA_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "replay.json"
    out.write_text(json.dumps(replay, separators=(",", ":")), encoding="utf-8")
    _write_index()
    _write_zones()
    return out


def _write_index() -> None:
    runs = []
    for path in sorted(DATA_DIR.glob("*/replay.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        runs.append(
            {
                "label": data["label"],
                "policy": data["policy"],
                "score_cents": data["report"].get("score_cents"),
                "has_field": data["world"] is not None,
                "world_verified": data["sources"]["world_verified"],
            }
        )
    (DATA_DIR / "index.json").write_text(json.dumps(runs, indent=1), encoding="utf-8")


def _write_zones() -> None:
    zones = json.loads(ZONES_GEOJSON.read_text(encoding="utf-8"))
    for feature in zones["features"]:
        geometry = feature["geometry"]
        rings = geometry["coordinates"] if geometry["type"] == "Polygon" else [r for p in geometry["coordinates"] for r in p]
        geometry["type"] = "MultiRing"
        geometry["coordinates"] = [[[round(x, 4), round(y, 4)] for x, y in ring[:: max(1, len(ring) // 400)]] for ring in rings]
    (DATA_DIR / "zones.json").write_text(json.dumps(zones, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_replay.py")
    parser.add_argument("run_dirs", type=Path, nargs="+")
    parser.add_argument("--no-verify", action="store_true", help="skip hashing a recorded run.db")
    args = parser.parse_args()
    for run_dir in args.run_dirs:
        out = build(run_dir, verify=not args.no_verify)
        print(f"{run_dir} -> {out.relative_to(REPO)} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
