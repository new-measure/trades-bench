from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from policies.calibration import SCENARIO_DIR, scenario_config
from policies.runner import REPORT_FILENAME, PolicyRunPaths, build_report, report_bytes
from trades_bench.harness.client import ControlClient
from trades_bench.harness.launcher import (
    EngineLaunchSpec,
    EngineTokens,
    agent_env,
    launch_engine,
)
from trades_bench.harness.pi_adapter import PiAdapter, PiConfig
from trades_bench.harness.runner import (
    ControlledRunner,
    HarnessLimits,
    initialize_controlled_run,
)
from trades_bench.harness.trajectory import TrajectoryWriter

DEFAULT_GEO_DIR = Path("data/geography-arizona-v1")
USAGE_FILENAME = "pi-usage.json"
SCORED_STATUSES = ("completed", "bankrupt")


def agent_label(config: PiConfig) -> str:
    return f"pi:{config.provider}/{config.model}"


def run_pi_agent(
    *,
    scenario_name: str,
    master_seed: int,
    run_dir: str | Path,
    geo_dir: str | Path,
    pi_config: PiConfig,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    geo_dir = Path(geo_dir)
    scenario, config = scenario_config(SCENARIO_DIR / f"{scenario_name}.json")
    paths = PolicyRunPaths.under(run_dir)
    paths.create()
    adapter = PiAdapter(
        session_dir=paths.run_dir / "pi-sessions",
        log_dir=paths.run_dir / "pi-logs",
        run_label=f"{scenario}-{master_seed}",
        config=pi_config,
    )
    run_id = initialize_controlled_run(
        paths.db_path,
        master_seed=master_seed,
        config=config,
        adapter=adapter,
        artifact_dir=geo_dir,
    )
    tokens = EngineTokens.mint()
    engine = launch_engine(
        EngineLaunchSpec(db_path=paths.db_path, run_dir=paths.run_dir, geo_dir=geo_dir),
        tokens=tokens,
    )
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
        run_status = runner.run()
    finally:
        engine.stop()

    report = build_report(
        paths.db_path,
        policy_name=agent_label(pi_config),
        config=config,
        scenario=scenario,
        geo_dir=geo_dir,
    )
    (paths.run_dir / REPORT_FILENAME).write_bytes(report_bytes(report))
    usage = _usage(adapter)
    (paths.run_dir / USAGE_FILENAME).write_text(
        json.dumps(usage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "run_id": run_id,
        "run_status": run_status,
        "report": report,
        "usage": usage,
        "run_dir": str(paths.run_dir),
    }


def _usage(adapter: PiAdapter) -> dict[str, Any]:
    return {
        "agent": agent_label(adapter.config),
        "turns": len(adapter.turns),
        "input_tokens": sum(turn.input_tokens or 0 for turn in adapter.turns),
        "output_tokens": sum(turn.output_tokens or 0 for turn in adapter.turns),
        "timed_out_turns": sum(1 for turn in adapter.turns if turn.timed_out),
        "abnormal_turns": sum(1 for turn in adapter.turns if not turn.ended_normally),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m policies.pi_run")
    result.add_argument("--run-dir", type=Path, required=True)
    result.add_argument("--scenario", default="oct_default")
    result.add_argument("--seed", type=int, default=5)
    result.add_argument("--geo-dir", type=Path, default=DEFAULT_GEO_DIR)
    result.add_argument("--provider", required=True)
    result.add_argument("--model", required=True)
    result.add_argument("--thinking", required=True)
    result.add_argument("--executable", default=PiConfig.executable)
    return result


def main() -> int:
    args = parser().parse_args()
    result = run_pi_agent(
        scenario_name=args.scenario,
        master_seed=args.seed,
        run_dir=args.run_dir,
        geo_dir=args.geo_dir.resolve(),
        pi_config=PiConfig(
            executable=args.executable,
            provider=args.provider,
            model=args.model,
            thinking=args.thinking,
        ),
    )
    report = result["report"]
    print(
        json.dumps(
            {
                "agent": report["policy"],
                "scenario": report["scenario"],
                "master_seed": report["master_seed"],
                "run_status": result["run_status"],
                "final_cash_cents": report["final_cash_cents"],
                "bankruptcy_date": report["bankruptcy_date"],
                "usage": result["usage"],
                "run_dir": result["run_dir"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result["run_status"] in SCORED_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_GEO_DIR",
    "SCORED_STATUSES",
    "USAGE_FILENAME",
    "agent_label",
    "run_pi_agent",
]
