from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import db
from .config import config_from_canonical
from .controller import Controller
from .errors import EngineError
from .http_engine import Engine, Tokens, open_engine_connection

CONTROL_TOKEN_ENV = "TRADES_BENCH_CONTROL_TOKEN"
AGENT_TOKEN_ENV = "TRADES_BENCH_AGENT_TOKEN"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="trades_bench", add_help=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--geo", default=None)
    return parser.parse_args(argv)


def _tokens_from_env(environ: dict[str, str]) -> Tokens:
    control = environ.get(CONTROL_TOKEN_ENV)
    agent = environ.get(AGENT_TOKEN_ENV)
    if control and agent:
        return Tokens(agent=agent, control=control)
    return Tokens.generate()


def _build_engine(db_path: Path, artifact_dir: str | None) -> tuple[Engine, object]:
    conn = open_engine_connection(db_path)
    run_row = conn.execute("SELECT run_id, config_hash FROM _run").fetchone()
    if run_row is None:
        raise SystemExit(f"{db_path} holds no run record")
    run_id = str(run_row["run_id"])
    stored = db.read_config(conn, run_id)
    if stored is None:
        raise SystemExit(f"{db_path} holds no configuration record")
    config_version, canonical_json = stored
    try:
        config = config_from_canonical(config_version, canonical_json)
    except EngineError as exc:
        raise SystemExit(f"{db_path}: {exc.message}") from None
    if config.config_hash != str(run_row["config_hash"]):
        raise SystemExit(f"{db_path} does not reproduce its recorded configuration hash")
    controller = Controller(conn, config, artifact_dir=artifact_dir)
    engine = Engine(
        services=controller.services,
        config=config,
        db_path=db_path,
        artifact=controller.artifact,
        tokens=_tokens_from_env(dict(os.environ)),
    )
    return engine, conn


def _publish_ready_file(ready_file: Path, port: int) -> None:
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    staging = ready_file.with_name(f"{ready_file.name}.{os.getpid()}.partial")
    staging.write_text(json.dumps({"port": port}) + "\n", encoding="utf-8")
    staging.replace(ready_file)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    engine, conn = _build_engine(Path(args.db), args.geo)
    stop_requested = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    engine.start()
    try:
        _publish_ready_file(Path(args.ready_file), engine.port)
        stop_requested.wait()
    finally:
        engine.stop()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
