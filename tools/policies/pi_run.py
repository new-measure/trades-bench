from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType
from typing import Any

import policies
import trades_bench
from policies.calibration import SCENARIO_DIR, scenario_config
from policies.runner import (
    FAILURE_FILENAME,
    REPORT_FILENAME,
    SCORED_STATUSES,
    PolicyRunPaths,
    RunNotScored,
    build_report,
    crash_reason,
    write_run_failure,
    write_scored_report,
)
from trades_bench.config import RunConfig
from trades_bench.harness import prompts, workspace
from trades_bench.harness.client import ControlClient
from trades_bench.harness.launcher import (
    EngineLaunchSpec,
    EngineTokens,
    agent_env,
    launch_engine,
)
from trades_bench.harness.pi_adapter import PiAdapter, PiAdapterError, PiConfig
from trades_bench.harness.runner import (
    ControlledRunner,
    HarnessLimits,
    HarnessRunFailed,
    initialize_controlled_run,
)
from trades_bench.harness.sandbox import (
    ProbeRunner,
    SandboxError,
    SandboxUser,
    create_session_staging,
    default_forbidden_paths,
    hand_over_workspace,
    preflight,
    probe_readable,
    remove_session_staging,
    require_existing,
    require_fresh_workspace,
    require_sealed_ancestors,
    secure_private_dir,
)
from trades_bench.harness.trajectory import TrajectoryWriter

DEFAULT_GEO_DIR = Path("data/geography-arizona-v1")
USAGE_FILENAME = "pi-usage.json"
SESSION_ARCHIVE_DIRNAME = "pi-sessions"
LOG_DIRNAME = "pi-logs"
TIMEOUT_FLAG = "invocation_timeout"
ABNORMAL_TURN_FLAG = "abnormal_turn"
CATALOG_CHANGED_FLAG = "catalog_changed"
ARGS_FILE_PREFIX = "@"
REFUSED_STATUS = "refused"
REFUSED_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130
INTERRUPTED_REASON = "interrupted"
FAILED_STATUS = "failed"
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class InterruptGuard:
    def __init__(self) -> None:
        self.received = False
        self._shielded = False
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> InterruptGuard:
        if threading.current_thread() is threading.main_thread():
            self._previous = {sig: signal.signal(sig, self._handle) for sig in INTERRUPT_SIGNALS}
        return self

    def __exit__(self, *exc_info: object) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous = {}

    def shield(self) -> None:
        self._shielded = True

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        first = not (self.received or self._shielded)
        self.received = True
        if first:
            raise KeyboardInterrupt


def harness_source_paths() -> tuple[Path, ...]:
    return default_forbidden_paths(
        (
            Path(trades_bench.__file__).parent,
            Path(policies.__file__).parent,
            SCENARIO_DIR,
        )
    )


def agent_label(config: PiConfig) -> str:
    return f"pi:{config.provider}/{config.model}@{config.thinking}"


def run_pi_agent(
    *,
    scenario_name: str,
    master_seed: int,
    run_dir: str | Path,
    geo_dir: str | Path,
    pi_config: PiConfig,
    workspace_dir: str | Path | None = None,
    forbid_paths: Sequence[str | Path] = (),
    sleep: Callable[[float], None] = time.sleep,
    probe: ProbeRunner = probe_readable,
) -> dict[str, Any]:
    geo_dir = Path(geo_dir)
    scenario, config = scenario_config(SCENARIO_DIR / f"{scenario_name}.json")
    missing_env = [name for name in pi_config.secret_env if not os.environ.get(name)]
    if missing_env:
        raise PiAdapterError(
            f"the pi environment is missing {', '.join(missing_env)}; refusing to start"
        )
    chosen_workspace = Path(run_dir) / "workspace" if workspace_dir is None else Path(workspace_dir)
    if pi_config.sandbox_user is not None:
        require_existing([Path(path) for path in forbid_paths])
        require_sealed_ancestors(chosen_workspace, SandboxUser.lookup(pi_config.sandbox_user))
    require_fresh_workspace(chosen_workspace)
    paths = PolicyRunPaths.under(run_dir, workspace=workspace_dir)
    paths.create()
    sandbox = _prepare_sandbox(paths, pi_config)
    staging = None if sandbox is None else create_session_staging(sandbox)
    archive = paths.run_dir / SESSION_ARCHIVE_DIRNAME
    adapter = PiAdapter(
        session_dir=archive if staging is None else staging,
        log_dir=paths.run_dir / LOG_DIRNAME,
        config=pi_config,
        archive_dir=None if staging is None else archive,
    )
    try:
        return _run(
            adapter,
            paths=paths,
            scenario=scenario,
            config=config,
            master_seed=master_seed,
            geo_dir=geo_dir,
            sandbox=sandbox,
            forbid_paths=tuple(Path(path) for path in forbid_paths),
            sleep=sleep,
            probe=probe,
        )
    except KeyboardInterrupt:
        _record_interrupt(adapter, paths)
        raise
    finally:
        adapter.archive_sessions()
        adapter.close()
        if staging is not None:
            remove_session_staging(staging)


def _prepare_sandbox(paths: PolicyRunPaths, pi_config: PiConfig) -> SandboxUser | None:
    if pi_config.sandbox_user is None:
        return None
    sandbox = SandboxUser.lookup(pi_config.sandbox_user)
    secure_private_dir(paths.run_dir)
    hand_over_workspace(
        paths.workspace,
        sandbox,
        read_only=workspace.docs_path(paths.workspace),
        writable=(workspace.memory_path(paths.workspace),),
    )
    return sandbox


def _write_usage(adapter: PiAdapter, paths: PolicyRunPaths) -> dict[str, Any]:
    usage = usage_summary(adapter, workspace_dir=paths.workspace)
    (paths.run_dir / USAGE_FILENAME).write_text(
        json.dumps(usage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return usage


def _record_interrupt(adapter: PiAdapter, paths: PolicyRunPaths) -> None:
    if any((paths.run_dir / name).exists() for name in (REPORT_FILENAME, FAILURE_FILENAME)):
        return
    usage = _write_usage(adapter, paths)
    write_run_failure(
        paths.run_dir,
        run_status=FAILED_STATUS,
        reason=INTERRUPTED_REASON,
        extra={"agent": usage["agent"], "usage": usage},
    )


def _run(
    adapter: PiAdapter,
    *,
    paths: PolicyRunPaths,
    scenario: str,
    config: RunConfig,
    master_seed: int,
    geo_dir: Path,
    sandbox: SandboxUser | None,
    forbid_paths: tuple[Path, ...],
    sleep: Callable[[float], None],
    probe: ProbeRunner,
) -> dict[str, Any]:
    run_id = initialize_controlled_run(
        paths.db_path,
        master_seed=master_seed,
        config=config,
        adapter=adapter,
        artifact_dir=geo_dir,
    )
    if sandbox is not None:
        try:
            preflight(
                sandbox,
                workspace=paths.workspace,
                forbidden=(
                    paths.db_path,
                    paths.run_dir,
                    *harness_source_paths(),
                    *forbid_paths,
                ),
                probe=probe,
            )
        except SandboxError as exc:
            write_run_failure(
                paths.run_dir,
                run_status=REFUSED_STATUS,
                reason=str(exc),
                extra={"agent": agent_label(adapter.config), "run_id": run_id},
            )
            raise
    tokens = EngineTokens.mint()
    engine = launch_engine(
        EngineLaunchSpec(db_path=paths.db_path, run_dir=paths.run_dir, geo_dir=geo_dir),
        tokens=tokens,
    )
    reason: str | None = None
    crash: Exception | None = None
    interrupted = False
    try:
        runner = ControlledRunner(
            client=ControlClient(port=engine.port, token=tokens.control),
            adapter=adapter,
            trajectory=TrajectoryWriter(paths.run_dir, run_id),
            workspace=paths.workspace,
            limits=HarnessLimits.from_config(config),
            agent_env=agent_env(
                port=engine.port, token=tokens.agent, workspace=paths.workspace
            ),
            engine=engine,
            sleep=sleep,
        )
        try:
            run_status = runner.run()
        except HarnessRunFailed as exc:
            run_status, reason = FAILED_STATUS, exc.reason
        except KeyboardInterrupt:
            run_status, reason, interrupted = FAILED_STATUS, INTERRUPTED_REASON, True
        except Exception as exc:
            run_status, reason = FAILED_STATUS, crash_reason(exc)
            crash = exc
    finally:
        engine.stop()

    adapter.archive_sessions()
    usage = _write_usage(adapter, paths)
    failure_extra = {"agent": usage["agent"], "run_id": run_id, "usage": usage}
    report: dict[str, Any] | None = None
    if run_status in SCORED_STATUSES:
        report = build_report(
            paths.db_path,
            policy_name=agent_label(adapter.config),
            config=config,
            scenario=scenario,
            geo_dir=geo_dir,
        )
        try:
            write_scored_report(paths.run_dir, report, extra=failure_extra)
        except RunNotScored as exc:
            run_status, reason, report = exc.run_status, exc.reason, None
    else:
        reason = reason or f"the engine finished the run as {run_status}"
        write_run_failure(
            paths.run_dir, run_status=run_status, reason=reason, extra=failure_extra
        )
    if crash is not None:
        raise crash
    if interrupted:
        raise KeyboardInterrupt
    return {
        "run_id": run_id,
        "run_status": run_status,
        "reason": reason,
        "report": report,
        "usage": usage,
        "run_dir": str(paths.run_dir),
    }


def usage_summary(adapter: PiAdapter, *, workspace_dir: str | Path) -> dict[str, Any]:
    turns = adapter.turns
    config = adapter.config
    timed_out = sum(1 for turn in turns if turn.timed_out)
    abnormal = sum(1 for turn in turns if not turn.ended_normally)
    flags: list[str] = []
    if timed_out:
        flags.append(TIMEOUT_FLAG)
    if abnormal > timed_out:
        flags.append(ABNORMAL_TURN_FLAG)
    catalog_at_end = adapter.catalog_digests()
    catalog_changed = (
        adapter.catalog_at_start is not None and adapter.catalog_at_start != catalog_at_end
    )
    if catalog_changed:
        flags.append(CATALOG_CHANGED_FLAG)
    return {
        "agent": agent_label(config),
        "pi_version": adapter.version,
        "thinking_requested": config.thinking,
        "thinking_applied": sorted(
            {turn.thinking_level for turn in turns if turn.thinking_level is not None}
        ),
        "turns": len(turns),
        "input_tokens": sum(turn.input_tokens or 0 for turn in turns),
        "output_tokens": sum(turn.output_tokens or 0 for turn in turns),
        "cache_read_tokens": sum(turn.cache_read_tokens or 0 for turn in turns),
        "cache_write_tokens": sum(turn.cache_write_tokens or 0 for turn in turns),
        "reasoning_tokens": sum(turn.reasoning_tokens or 0 for turn in turns),
        "cost_total": round(sum(turn.cost_total or 0.0 for turn in turns), 6),
        "timed_out_turns": timed_out,
        "abnormal_turns": abnormal,
        "flags": flags,
        "system_prompt_sha256": prompts.system_prompt_hash(),
        "days": [
            {
                "service_date": day.service_date,
                "session_id": day.session_id,
                "system_sha256": day.system_sha256,
                "system_cwd": day.system_cwd,
                "thinking_level": day.thinking_level,
            }
            for day in adapter.days
        ],
        "pi_agent_dir": config.agent_dir,
        "pi_env": list(config.secret_env),
        "model_catalog_sha256": catalog_at_end,
        "model_catalog_sha256_at_start": adapter.catalog_at_start,
        "catalog_changed": catalog_changed,
        "offline": config.offline,
        "sandbox_user": config.sandbox_user,
        "workspace": str(Path(workspace_dir)),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="python -m policies.pi_run", fromfile_prefix_chars=ARGS_FILE_PREFIX
    )
    result.add_argument("--run-dir", type=Path, required=True)
    result.add_argument("--workspace", type=Path, default=None)
    result.add_argument("--scenario", default="oct_default")
    result.add_argument("--seed", type=int, default=5)
    result.add_argument("--geo-dir", type=Path, default=DEFAULT_GEO_DIR)
    result.add_argument("--provider", required=True)
    result.add_argument("--model", required=True)
    result.add_argument("--thinking", required=True)
    result.add_argument("--executable", default=PiConfig.executable)
    result.add_argument("--sandbox-user", default=None)
    result.add_argument("--pi-agent-dir", type=Path, default=None)
    result.add_argument("--offline", action="store_true")
    result.add_argument("--forbid-path", type=Path, action="append", default=[])
    result.add_argument("--pi-env", action="append", default=[])
    return result


def pi_config_from_args(args: argparse.Namespace) -> PiConfig:
    return PiConfig(
        executable=args.executable,
        provider=args.provider,
        model=args.model,
        thinking=args.thinking,
        offline=bool(args.offline),
        agent_dir=None if args.pi_agent_dir is None else str(args.pi_agent_dir.resolve()),
        sandbox_user=args.sandbox_user,
        secret_env=tuple(args.pi_env),
    )


def summary(result: dict[str, Any]) -> dict[str, Any]:
    usage = result["usage"]
    payload: dict[str, Any] = {
        "agent": usage["agent"],
        "run_status": result["run_status"],
        "timed_out_turns": usage["timed_out_turns"],
        "flags": usage["flags"],
        "usage": usage,
        "run_dir": result["run_dir"],
    }
    report = result["report"]
    if report is None:
        payload["reason"] = result["reason"]
        return payload
    payload.update(
        {
            "scenario": report["scenario"],
            "master_seed": report["master_seed"],
            "final_cash_cents": report["final_cash_cents"],
            "bankruptcy_date": report["bankruptcy_date"],
        }
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.forbid_path and args.sandbox_user is None:
        cli.error("--forbid-path requires --sandbox-user")
    try:
        with InterruptGuard():
            result = run_pi_agent(
                scenario_name=args.scenario,
                master_seed=args.seed,
                run_dir=args.run_dir,
                geo_dir=args.geo_dir.resolve(),
                pi_config=pi_config_from_args(args),
                workspace_dir=args.workspace,
                forbid_paths=args.forbid_path,
            )
    except (SandboxError, PiAdapterError) as exc:
        print(f"pi_run refused to start: {exc}", file=sys.stderr)
        return REFUSED_EXIT_CODE
    except KeyboardInterrupt:
        print("pi_run interrupted; the failure record and usage were written", file=sys.stderr)
        return INTERRUPTED_EXIT_CODE
    print(json.dumps(summary(result), indent=2, sort_keys=True))
    return 0 if result["report"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ABNORMAL_TURN_FLAG",
    "ARGS_FILE_PREFIX",
    "CATALOG_CHANGED_FLAG",
    "DEFAULT_GEO_DIR",
    "FAILED_STATUS",
    "INTERRUPTED_EXIT_CODE",
    "INTERRUPTED_REASON",
    "LOG_DIRNAME",
    "REFUSED_EXIT_CODE",
    "REFUSED_STATUS",
    "SCORED_STATUSES",
    "SESSION_ARCHIVE_DIRNAME",
    "TIMEOUT_FLAG",
    "USAGE_FILENAME",
    "InterruptGuard",
    "agent_label",
    "harness_source_paths",
    "main",
    "parser",
    "pi_config_from_args",
    "run_pi_agent",
    "summary",
    "usage_summary",
]
