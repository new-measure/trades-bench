from __future__ import annotations

# Rebuild run.db for a run that was published without it (pi-logs + policy-report.json only).
#
# The engine is exactly replayable: same seed + same requests at the same simulated moments
# gives the same world. We drive the real engine through the real harness runner, and answer
# each prompt by re-executing that turn's recorded tool calls (bash/edit/write) inside a Docker
# sandbox laid out like the original agent host. The result is checked against the recorded
# dump_hash in policy-report.json.

import argparse
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

from policies.calibration import SCENARIO_DIR, scenario_config  # noqa: E402
from policies.runner import PolicyRunPaths, build_report, report_bytes  # noqa: E402
from trades_bench._dump import dump  # noqa: E402
from trades_bench.adapter import AgentTurn, RunMetadata  # noqa: E402
from trades_bench.harness.client import ControlClient  # noqa: E402
from trades_bench.harness.launcher import (  # noqa: E402
    EngineLaunchSpec,
    EngineTokens,
    agent_env,
    launch_engine,
)
from trades_bench.harness.pi_adapter import PiAdapter, PiConfig  # noqa: E402
from trades_bench.harness.runner import (  # noqa: E402
    ControlledRunner,
    HarnessLimits,
    initialize_controlled_run,
)
from trades_bench.harness.trajectory import TrajectoryWriter  # noqa: E402

DEFAULT_GEO_DIR = REPO / "data/geography-arizona-v1"
REBUILT_DIRNAME = "rebuilt"
VERIFICATION_FILENAME = "verification.json"
MEMORY_HISTORY_FILENAME = "memory-history.jsonl"
MEMORY_FILENAME = "MEMORY.md"
SANDBOX_IMAGE = "trades-bench-replay:1"
SANDBOX_DIR = Path(__file__).resolve().parent / "sandbox"
COMMAND_TIMEOUT_S = 600
PI_PACKAGE_URL = "https://registry.npmjs.org/@earendil-works/pi-coding-agent"


class ReplayDiverged(RuntimeError):
    pass


@dataclass(frozen=True)
class Action:

    tool: str
    args: Mapping[str, Any]
    output: str
    is_error: bool


@dataclass(frozen=True)
class RecordedTurn:

    stem: str
    service_date: str
    kind: str
    prompt: str
    actions: tuple[Action, ...]
    text: str
    timed_out: bool
    ended_normally: bool
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class AgentHost:

    workspace: str
    ports: tuple[int, ...]
    token: str
    pi_config: PiConfig


def _result_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    return "".join(
        part.get("text", "") for part in result.get("content") or () if isinstance(part, Mapping)
    )


def _read_turn(log_dir: Path, meta_path: Path) -> RecordedTurn:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stem = meta_path.name[: -len(".turn.json")]
    starts: dict[str, Action] = {}
    order: list[str] = []
    text = ""
    for line in (log_dir / f"{stem}.stdout.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        kind = event.get("type")
        if kind == "tool_execution_start":
            order.append(event["toolCallId"])
            starts[event["toolCallId"]] = Action(event["toolName"], event.get("args") or {}, "", False)
        elif kind == "tool_execution_end" and event["toolCallId"] in starts:
            started = starts[event["toolCallId"]]
            starts[event["toolCallId"]] = Action(
                started.tool, started.args, _result_text(event.get("result")), bool(event.get("isError"))
            )
        elif kind == "message_end" and event["message"].get("role") == "assistant":
            parts = event["message"].get("content") or ()
            texts = [p.get("text", "") for p in parts if isinstance(p, Mapping) and p.get("type") == "text"]
            if texts:
                text = "".join(texts)
    return RecordedTurn(
        stem=stem,
        service_date=str(meta["service_date"]),
        kind=str(meta["kind"]),
        prompt=(log_dir / f"{stem}.prompt.txt").read_text(encoding="utf-8"),
        actions=tuple(starts[call_id] for call_id in order),
        text=text,
        timed_out=bool(meta["timed_out"]),
        ended_normally=bool(meta["ended_normally"]),
        input_tokens=meta.get("input_tokens"),
        output_tokens=meta.get("output_tokens"),
    )


def load_turns(run_dir: Path) -> list[RecordedTurn]:
    log_dir = run_dir / "pi-logs"
    return [_read_turn(log_dir, path) for path in sorted(log_dir.glob("*.turn.json"))]


def _argv_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def discover_host(run_dir: Path, turns: list[RecordedTurn]) -> AgentHost:
    first = json.loads(next(iter(sorted((run_dir / "pi-logs").glob("*.turn.json")))).read_text())
    workspace = str(Path(first["stdout_path"]).parent.parent / "workspace")
    argv = first["argv"]
    outputs = "\n".join(a.output for t in turns for a in t.actions)
    commands = "\n".join(str(a.args.get("command", "")) for t in turns for a in t.actions)
    tokens = sorted(set(re.findall(r"TOKEN=([0-9a-f]{64})", outputs)))
    ports = {int(p) for p in re.findall(r"PORT=(\d+)", outputs)}
    ports |= {int(p) for p in re.findall(r"127\.0\.0\.1:(\d{4,5})", outputs + commands)}
    if len(tokens) != 1:
        raise ReplayDiverged(f"expected exactly one agent token in the logs, found {len(tokens)}")
    return AgentHost(
        workspace=workspace,
        ports=tuple(sorted(ports)),
        token=tokens[0],
        pi_config=PiConfig(
            provider=_argv_value(argv, "--provider"),
            model=_argv_value(argv, "--model"),
            thinking=_argv_value(argv, "--thinking"),
        ),
    )


class Sandbox:

    def __init__(self, *, host: AgentHost, workspace_dir: Path, engine_port: int) -> None:
        self.host = host
        self.workspace_dir = workspace_dir
        self.engine_port = engine_port
        self.name = f"trades-bench-replay-{secrets.token_hex(4)}"
        self.env = {
            "HOME": "/home/ubuntu",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "SUNRIDGE_API_PORT": str(host.ports[0]),
            "SUNRIDGE_API_TOKEN": host.token,
            "SUNRIDGE_WORKSPACE": host.workspace,
        }

    def start(self) -> None:
        if subprocess.run(["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True).returncode:
            subprocess.run(["docker", "build", "-t", SANDBOX_IMAGE, str(SANDBOX_DIR)], check=True)
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", self.name,
                "-v", f"{self.workspace_dir.resolve()}:{self.host.workspace}",
                SANDBOX_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        # The engine only binds host loopback; forward the original port(s) inside the sandbox to it.
        for port in self.host.ports:
            subprocess.run(
                [
                    "docker", "exec", "-d", self.name, "socat",
                    f"TCP-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr",
                    f"TCP:host.docker.internal:{self.engine_port}",
                ],
                check=True,
            )

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def _exec(self, argv: list[str], stdin: str | None = None) -> tuple[str, int]:
        env_args = [arg for name, value in self.env.items() for arg in ("-e", f"{name}={value}")]
        completed = subprocess.run(
            ["docker", "exec", "-i", "-w", self.host.workspace, *env_args, self.name, *argv],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
        )
        return completed.stdout + completed.stderr, completed.returncode

    def bash(self, command: str) -> tuple[str, int]:
        return self._exec(["bash", "-c", command])

    def file_op(self, op: Mapping[str, Any]) -> tuple[str, int]:
        return self._exec(["python3", "-c", _FILE_OP_SCRIPT], stdin=json.dumps(op))


_FILE_OP_SCRIPT = r"""
import json, os, sys
op = json.load(sys.stdin)
path = os.path.expanduser(op["path"])
if op["tool"] == "write":
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    open(path, "w", encoding="utf-8").write(op["content"])
    print("ok")
    sys.exit(0)
text = open(path, encoding="utf-8").read()
edits = op.get("edits") or [{"oldText": op.get("oldText"), "newText": op.get("newText")}]
for edit in edits:
    if edit["oldText"] not in text:
        print("edit target not found"); sys.exit(1)
    text = text.replace(edit["oldText"], edit["newText"], 1)
open(path, "w", encoding="utf-8").write(text)
print("ok")
"""


@dataclass
class Divergence:

    turn: str
    index: int
    tool: str
    detail: str


@dataclass
class ReplayStats:

    actions: int = 0
    matched_outputs: int = 0
    divergences: list[Divergence] = field(default_factory=list)


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


class ReplayAdapter:

    def __init__(self, turns: list[RecordedTurn], metadata: RunMetadata, sandbox: Sandbox | None) -> None:
        self.name = metadata.name
        self.version = metadata.version
        self.context_policy = metadata.context_policy
        self.attempt_definition = metadata.attempt_definition
        self._metadata = metadata
        self._turns = turns
        self._next = 0
        self._day: str | None = None
        self.sandbox = sandbox
        self.stats = ReplayStats()
        self.memory_log: Path | None = None
        self._memory = ""

    def describe(self) -> RunMetadata:
        return self._metadata

    def start_run(self, workspace: Path, system_prompt: str, env: Mapping[str, str]) -> None:
        pass

    def start_day(self, service_date: date) -> None:
        self._day = service_date.isoformat()

    def end_day(self, service_date: date) -> None:
        self._day = None

    def end_run(self, status: str) -> None:
        if self._next != len(self._turns):
            raise ReplayDiverged(f"run ended after {self._next} of {len(self._turns)} recorded turns")

    def send(self, prompt: str, kind: str, deadline_s: float) -> AgentTurn:
        if self._next >= len(self._turns):
            raise ReplayDiverged(f"engine issued a {kind} prompt beyond the recorded turns")
        turn = self._turns[self._next]
        self._next += 1
        if (turn.service_date, turn.kind) != (self._day, kind) or turn.prompt.strip() != prompt.strip():
            raise ReplayDiverged(
                f"prompt mismatch at {turn.stem}: engine sent {self._day} {kind}:\n{prompt}\n"
                f"--- recorded ---\n{turn.prompt}"
            )
        print(f"  {turn.stem}  ({len(turn.actions)} actions)", flush=True)
        for index, action in enumerate(turn.actions):
            self._apply(turn, index, action)
        if turn.timed_out:
            raise TimeoutError(f"recorded turn {turn.stem} timed out")
        return AgentTurn(
            text=turn.text,
            ended_normally=turn.ended_normally,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
        )

    def _apply(self, turn: RecordedTurn, index: int, action: Action) -> None:
        assert self.sandbox is not None
        if action.tool == "bash":
            output, _ = self.sandbox.bash(str(action.args["command"]))
            matched = _normalize(output) == _normalize(action.output)
            detail = "" if matched else _first_difference(output, action.output)
        elif action.tool in ("edit", "write"):
            _, code = self.sandbox.file_op({"tool": action.tool, **action.args})
            matched = (code != 0) == action.is_error
            detail = "" if matched else f"error={code != 0}, recorded error={action.is_error}"
        else:
            return
        self.stats.actions += 1
        self.stats.matched_outputs += int(matched)
        if not matched:
            self.stats.divergences.append(Divergence(turn.stem, index, action.tool, detail))
        self._snapshot_memory(turn, index)

    def _snapshot_memory(self, turn: RecordedTurn, index: int) -> None:
        # The notebook view needs every version of MEMORY.md; agents rewrite it with edits,
        # heredocs and python, so read it back from the sandbox after each action.
        assert self.sandbox is not None and self.memory_log is not None
        memory = self.sandbox.workspace_dir / MEMORY_FILENAME
        content = memory.read_text(encoding="utf-8", errors="replace") if memory.exists() else ""
        if content == self._memory:
            return
        self._memory = content
        record = {"turn": turn.stem, "action_index": index, "content": content}
        with self.memory_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def _first_difference(replayed: str, recorded: str) -> str:
    left, right = _normalize(replayed).splitlines(), _normalize(recorded).splitlines()
    for number, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return f"line {number + 1}: replayed {a[:160]!r} vs recorded {b[:160]!r}"
    return f"replayed {len(left)} lines vs recorded {len(right)} lines"


def pi_version_candidates() -> list[str]:
    with urllib.request.urlopen(PI_PACKAGE_URL, timeout=30) as response:
        versions = list(json.load(response).get("versions", {}))
    forms = [form for version in reversed(versions) for form in (version, f"pi {version}", f"v{version}")]
    return ["unknown", *forms]


def match_harness_version(db_path: Path, expected_hash: str, run_id: str) -> str | None:
    # pi's version string is stored in _run but never logged; find the one that reproduces the hash.
    # Serialize the dump once with a placeholder, then swap each candidate into the bytes.
    placeholder = "HARNESS_VERSION_PLACEHOLDER_7f3a9c"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = dump(conn, run_id)
    finally:
        conn.close()
    for row in snapshot["tables"]["_run"]:  # type: ignore[index]
        row["harness_version"] = placeholder
    template = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    needle = json.dumps(placeholder).encode()
    for candidate in pi_version_candidates():
        candidate_bytes = template.replace(needle, json.dumps(candidate, ensure_ascii=True).encode())
        if hashlib.sha256(candidate_bytes).hexdigest() == expected_hash:
            return candidate
    return None


def rebuild(run_dir: Path, *, geo_dir: Path, force: bool = False) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    recorded = json.loads((run_dir / "policy-report.json").read_text(encoding="utf-8"))
    out_dir = run_dir / REBUILT_DIRNAME
    if out_dir.exists():
        if not force:
            raise SystemExit(f"{out_dir} already exists; pass --force to rebuild it")
        shutil.rmtree(out_dir)

    turns = load_turns(run_dir)
    host = discover_host(run_dir, turns)
    scenario, config = scenario_config(SCENARIO_DIR / f"{recorded['scenario']}.json")
    pi = PiAdapter(session_dir=out_dir / "unused", log_dir=out_dir / "unused", run_label="replay", config=host.pi_config)
    pi._version = "replay"  # placeholder; the real version is recovered from the hash below
    metadata = pi.describe()
    print(f"{len(turns)} recorded turns, agent host {host.workspace} ports {host.ports}, model {metadata.model_id}")

    paths = PolicyRunPaths.under(out_dir)
    paths.create()
    adapter = ReplayAdapter(turns, metadata, None)
    run_id = initialize_controlled_run(
        paths.db_path, master_seed=int(recorded["master_seed"]), config=config, adapter=adapter, artifact_dir=geo_dir
    )
    with sqlite3.connect(paths.db_path) as conn:
        config_hash = conn.execute("SELECT config_hash FROM _run").fetchone()[0]
    if config_hash != recorded["config_hash"]:
        raise ReplayDiverged(f"scenario config hash {config_hash} != recorded {recorded['config_hash']}")
    tokens = EngineTokens(agent=host.token, control=secrets.token_hex(32))
    engine = launch_engine(
        EngineLaunchSpec(db_path=paths.db_path, run_dir=paths.run_dir, geo_dir=geo_dir), tokens=tokens
    )
    sandbox = Sandbox(host=host, workspace_dir=paths.workspace, engine_port=engine.port)
    adapter.sandbox = sandbox
    adapter.memory_log = out_dir / MEMORY_HISTORY_FILENAME
    try:
        sandbox.start()
        runner = ControlledRunner(
            client=ControlClient(port=engine.port, token=tokens.control),
            adapter=adapter,
            trajectory=TrajectoryWriter(paths.run_dir, run_id),
            workspace=paths.workspace,
            limits=HarnessLimits.from_config(config),
            agent_env=agent_env(port=engine.port, token=tokens.agent, workspace=paths.workspace),
            engine=engine,
            sleep=lambda _: None,
        )
        run_status = runner.run()
    finally:
        engine.stop()
        sandbox.stop()

    harness_version = match_harness_version(paths.db_path, recorded["dump_hash"], run_id)
    if harness_version is not None:
        conn = sqlite3.connect(paths.db_path)
        conn.execute("UPDATE _run SET harness_version=?", (harness_version,))
        conn.commit()
        conn.close()
    report = build_report(
        paths.db_path, policy_name=recorded["policy"], config=config, scenario=scenario, geo_dir=geo_dir
    )
    (out_dir / "policy-report.json").write_bytes(report_bytes(report))
    differing = sorted(k for k in set(report) | set(recorded) if report.get(k) != recorded.get(k))
    verification = {
        "run_status": run_status,
        "verified": report["dump_hash"] == recorded["dump_hash"],
        "recorded_dump_hash": recorded["dump_hash"],
        "rebuilt_dump_hash": report["dump_hash"],
        "harness_version": harness_version,
        "report_fields_differing": differing,
        "replayed_actions": adapter.stats.actions,
        "matching_outputs": adapter.stats.matched_outputs,
        "divergences": [d.__dict__ for d in adapter.stats.divergences],
    }
    (out_dir / VERIFICATION_FILENAME).write_text(json.dumps(verification, indent=2) + "\n", encoding="utf-8")
    return verification


def main() -> int:
    parser = argparse.ArgumentParser(prog="rebuild_run.py")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--geo-dir", type=Path, default=DEFAULT_GEO_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = rebuild(args.run_dir, geo_dir=args.geo_dir.resolve(), force=args.force)
    summary = {k: v for k, v in result.items() if k != "divergences"}
    summary["divergences"] = len(result["divergences"])
    print(json.dumps(summary, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
