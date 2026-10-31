from __future__ import annotations

import json
import os
import pwd
import signal
from pathlib import Path

import pytest

import policies
import trades_bench
from policies import pi_run
from policies.pi_run import parser, pi_config_from_args
from policies.runner import FAILURE_FILENAME, REPORT_FILENAME
from trades_bench.harness.pi_adapter import PiConfig
from trades_bench.harness.runner import HarnessRunFailed

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED = [
    "--run-dir",
    "runs/example",
    "--provider",
    "opencode-go",
    "--model",
    "gpt-5.6-luna",
    "--thinking",
    "high",
]


def test_pi_config_requires_provider_model_and_thinking():
    with pytest.raises(TypeError):
        PiConfig()


def test_pi_run_parser_requires_provider_model_and_thinking():
    required = ("--provider", "--model", "--thinking")
    for omitted in required:
        arguments = list(REQUIRED)
        index = arguments.index(omitted)
        del arguments[index : index + 2]
        with pytest.raises(SystemExit) as exc_info:
            parser().parse_args(arguments)
        assert exc_info.value.code == 2


def test_pi_run_parser_accepts_explicit_agent_configuration():
    args = parser().parse_args(REQUIRED)
    assert (args.provider, args.model, args.thinking) == (
        "opencode-go",
        "gpt-5.6-luna",
        "high",
    )
    assert args.workspace is None
    assert args.sandbox_user is None
    assert args.pi_agent_dir is None
    assert args.offline is False
    assert args.forbid_path == []


def test_pi_run_parser_carries_the_isolation_options(tmp_path):
    args = parser().parse_args(
        [
            *REQUIRED,
            "--workspace",
            "/home/sunridge/workspace",
            "--sandbox-user",
            "sunridge",
            "--pi-agent-dir",
            str(tmp_path / "pi-agent"),
            "--offline",
            "--forbid-path",
            "/root/.pi",
            "--forbid-path",
            "/srv/scenarios",
        ]
    )
    config = pi_config_from_args(args)

    assert args.workspace == Path("/home/sunridge/workspace")
    assert args.forbid_path == [Path("/root/.pi"), Path("/srv/scenarios")]
    assert config.sandbox_user == "sunridge"
    assert config.agent_dir == str((tmp_path / "pi-agent").resolve())
    assert config.offline is True


def test_forbid_path_without_a_sandbox_user_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc_info:
        pi_run.main([*REQUIRED, "--forbid-path", "/root/.pi"])

    assert exc_info.value.code == 2
    assert "--forbid-path requires --sandbox-user" in capsys.readouterr().err


class _FakeEngine:

    port = 8123

    def stop(self) -> int:
        return 0


def _stub_run(monkeypatch, *, outcome, report_status: str = "completed") -> None:
    class _Runner:
        def __init__(self, **kwargs: object) -> None:
            self.adapter = kwargs["adapter"]

        def run(self) -> str:
            self.adapter.end_run("failed" if isinstance(outcome, Exception) else outcome)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(pi_run, "initialize_controlled_run", lambda *a, **k: "run-under-test")
    monkeypatch.setattr(pi_run, "launch_engine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(pi_run, "ControlledRunner", _Runner)
    monkeypatch.setattr(
        pi_run,
        "build_report",
        lambda *a, **k: {
            "policy": k["policy_name"],
            "scenario": "oct_default",
            "master_seed": 5,
            "run_status": report_status,
            "final_cash_cents": 123,
            "bankruptcy_date": None,
        },
    )


def _run(tmp_path: Path, **kwargs: object) -> dict:
    options: dict[str, object] = {
        "pi_config": PiConfig(
            provider="openai",
            model="gpt-test",
            thinking="xhigh",
            executable=str(tmp_path / "missing-pi"),
        ),
        **kwargs,
    }
    return pi_run.run_pi_agent(
        scenario_name="oct_default",
        master_seed=5,
        run_dir=tmp_path / "run",
        geo_dir=tmp_path / "geo",
        sleep=lambda _seconds: None,
        **options,
    )


def test_a_failed_run_writes_a_failure_record_and_no_policy_report(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome=HarnessRunFailed("adapter integrity check failed: thinking"))

    result = _run(tmp_path)

    run_dir = tmp_path / "run"
    assert result["run_status"] == "failed"
    assert result["report"] is None
    assert not (run_dir / REPORT_FILENAME).exists()
    failure = json.loads((run_dir / FAILURE_FILENAME).read_text(encoding="utf-8"))
    assert failure["run_status"] == "failed"
    assert failure["reason"] == "adapter integrity check failed: thinking"
    assert failure["agent"] == "pi:openai/gpt-test@xhigh"
    assert failure["run_id"] == "run-under-test"
    assert failure["usage"]["turns"] == 0
    assert (run_dir / pi_run.USAGE_FILENAME).exists()
    summary = pi_run.summary(result)
    assert summary["run_status"] == "failed"
    assert summary["reason"] == "adapter integrity check failed: thinking"
    assert "final_cash_cents" not in summary


def test_a_run_the_engine_did_not_complete_is_not_reported(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome="failed")

    result = _run(tmp_path)

    assert result["report"] is None
    assert not (tmp_path / "run" / REPORT_FILENAME).exists()
    assert json.loads((tmp_path / "run" / FAILURE_FILENAME).read_text())["run_status"] == "failed"


def test_a_report_whose_run_file_is_not_scored_is_withheld(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome="completed", report_status="failed")

    result = _run(tmp_path)

    assert result["report"] is None
    assert result["run_status"] == "failed"
    assert not (tmp_path / "run" / REPORT_FILENAME).exists()
    assert (tmp_path / "run" / FAILURE_FILENAME).exists()


def test_a_completed_run_is_reported_with_its_flags(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome="completed")

    result = _run(tmp_path, workspace_dir=tmp_path / "neutral" / "workspace")

    run_dir = tmp_path / "run"
    assert (run_dir / REPORT_FILENAME).exists()
    assert not (run_dir / FAILURE_FILENAME).exists()
    assert (tmp_path / "neutral" / "workspace" / "MEMORY.md").exists()
    assert not (run_dir / "workspace").exists()
    usage = json.loads((run_dir / pi_run.USAGE_FILENAME).read_text(encoding="utf-8"))
    assert usage["agent"] == "pi:openai/gpt-test@xhigh"
    assert usage["flags"] == []
    assert usage["timed_out_turns"] == 0
    assert usage["workspace"] == str((tmp_path / "neutral" / "workspace").resolve())
    summary = pi_run.summary(result)
    assert summary["flags"] == []
    assert summary["timed_out_turns"] == 0
    assert summary["final_cash_cents"] == 123


def test_an_unexpected_harness_error_still_writes_a_failure_record(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome=ConnectionResetError("engine went away"))

    with pytest.raises(ConnectionResetError):
        _run(tmp_path)

    run_dir = tmp_path / "run"
    assert not (run_dir / REPORT_FILENAME).exists()
    failure = json.loads((run_dir / FAILURE_FILENAME).read_text(encoding="utf-8"))
    assert failure["run_status"] == "failed"
    assert failure["reason"] == "the harness raised ConnectionResetError: engine went away"


class _StoppableEngine(_FakeEngine):
    def __init__(self, on_stop=lambda: None) -> None:
        self.stops = 0
        self._on_stop = on_stop

    def stop(self) -> int:
        self.stops += 1
        self._on_stop()
        return 0


def _stub_interrupted_run(monkeypatch, interrupt, engine: _StoppableEngine) -> None:
    _stub_run(monkeypatch, outcome="completed")

    class _Runner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self) -> str:
            interrupt()
            return "completed"

    monkeypatch.setattr(pi_run, "launch_engine", lambda *a, **k: engine)
    monkeypatch.setattr(pi_run, "ControlledRunner", _Runner)


def _raise_interrupt() -> None:
    raise KeyboardInterrupt


def _assert_interrupted_record(run_dir: Path, *, run_id: str | None) -> None:
    assert not (run_dir / REPORT_FILENAME).exists()
    failure = json.loads((run_dir / FAILURE_FILENAME).read_text(encoding="utf-8"))
    assert (failure["run_status"], failure["reason"]) == ("failed", pi_run.INTERRUPTED_REASON)
    assert failure.get("run_id") == run_id
    assert failure["usage"]["agent"] == "pi:openai/gpt-test@xhigh"
    usage = json.loads((run_dir / pi_run.USAGE_FILENAME).read_text(encoding="utf-8"))
    assert usage == failure["usage"]


def test_an_interrupted_run_stops_the_engine_and_records_the_failure_and_usage(
    tmp_path, monkeypatch
):
    engine = _StoppableEngine()
    _stub_interrupted_run(monkeypatch, _raise_interrupt, engine)

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path)

    assert engine.stops == 1
    _assert_interrupted_record(tmp_path / "run", run_id="run-under-test")


def test_an_interrupt_before_the_runner_starts_still_records_the_failure(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome="completed")

    def interrupted_launch(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pi_run, "launch_engine", interrupted_launch)

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path)

    _assert_interrupted_record(tmp_path / "run", run_id=None)


def _main_args(tmp_path: Path) -> list[str]:
    return [
        "--run-dir",
        str(tmp_path / "run"),
        "--provider",
        "openai",
        "--model",
        "gpt-test",
        "--thinking",
        "xhigh",
        "--executable",
        str(tmp_path / "missing-pi"),
        "--geo-dir",
        str(tmp_path / "geo"),
    ]


def test_sigterm_becomes_an_interrupted_failure_and_a_second_signal_cannot_skip_it(
    tmp_path, monkeypatch, capsys
):
    def terminate() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    engine = _StoppableEngine(on_stop=terminate)
    _stub_interrupted_run(monkeypatch, terminate, engine)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    code = pi_run.main(_main_args(tmp_path))

    assert code == pi_run.INTERRUPTED_EXIT_CODE
    assert "interrupted" in capsys.readouterr().err
    assert engine.stops == 1
    _assert_interrupted_record(tmp_path / "run", run_id="run-under-test")
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


def test_a_workspace_left_by_another_run_is_refused(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome="completed")
    stale = tmp_path / "neutral" / "workspace"
    stale.mkdir(parents=True)
    (stale / "MEMORY.md").write_text("notes from another run", encoding="utf-8")

    with pytest.raises(pi_run.SandboxError, match="refusing to reuse it"):
        _run(tmp_path, workspace_dir=stale)

    assert (stale / "MEMORY.md").read_text(encoding="utf-8") == "notes from another run"
    assert not (tmp_path / "run").exists()


def test_main_exits_non_zero_for_a_failed_run(tmp_path, monkeypatch, capsys):
    _stub_run(monkeypatch, outcome=HarnessRunFailed("model turn failed after 3 retries"))
    monkeypatch.setattr(pi_run, "DEFAULT_GEO_DIR", tmp_path / "geo")

    code = pi_run.main(
        [
            "--run-dir",
            str(tmp_path / "run"),
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--thinking",
            "high",
            "--executable",
            str(tmp_path / "missing-pi"),
            "--geo-dir",
            str(tmp_path / "geo"),
        ]
    )

    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["run_status"] == "failed"
    assert printed["reason"] == "model turn failed after 3 retries"


def test_the_parser_reads_its_arguments_from_a_root_only_args_file(tmp_path):
    args_file = tmp_path / "args"
    args_file.write_text(
        "\n".join(
            [
                "--run-dir",
                "/root/runs/a1",
                "--seed",
                "11",
                "--scenario",
                "oct_default",
                "--provider",
                "openai",
                "--model",
                "gpt-6-astra",
                "--thinking",
                "xhigh",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    args = parser().parse_args([f"{pi_run.ARGS_FILE_PREFIX}{args_file}", "--offline"])

    assert args.run_dir == Path("/root/runs/a1")
    assert (args.seed, args.scenario) == (11, "oct_default")
    assert (args.provider, args.model, args.thinking) == ("openai", "gpt-6-astra", "xhigh")
    assert args.offline is True


def _unprivileged_name() -> str:
    return pwd.getpwuid(os.getuid()).pw_name if os.getuid() != 0 else "nobody"


def _sandboxed_config(tmp_path: Path) -> PiConfig:
    return PiConfig(
        provider="openai",
        model="gpt-test",
        thinking="xhigh",
        executable=str(tmp_path / "missing-pi"),
        sandbox_user=_unprivileged_name(),
    )


def test_a_mistyped_forbid_path_refuses_the_run_before_anything_is_created(tmp_path):
    with pytest.raises(pi_run.SandboxError, match="do not exist"):
        _run(
            tmp_path,
            forbid_paths=[tmp_path / "scenarois"],
            pi_config=_sandboxed_config(tmp_path),
        )

    assert not (tmp_path / "run").exists()


def test_the_preflight_always_forbids_the_harness_sources(tmp_path, monkeypatch):
    _stub_run(monkeypatch, outcome="completed")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(pi_run, "require_sealed_ancestors", lambda *a: None)
    user = pi_run.SandboxUser.lookup(_unprivileged_name())
    monkeypatch.setattr(pi_run, "_prepare_sandbox", lambda paths, config: user)
    monkeypatch.setattr(pi_run, "create_session_staging", lambda user: staging)
    monkeypatch.setattr(pi_run, "remove_session_staging", lambda path: None)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    workspace_dir = (tmp_path / "neutral" / "workspace").resolve()
    probed: list[Path] = []

    def probe(_user: object, path: Path) -> bool:
        probed.append(Path(path))
        return Path(path) == workspace_dir or Path(path) in pi_run.harness_source_paths()

    with pytest.raises(pi_run.SandboxError, match="can read private paths"):
        _run(
            tmp_path,
            workspace_dir=tmp_path / "neutral" / "workspace",
            forbid_paths=[secrets],
            pi_config=_sandboxed_config(tmp_path),
            probe=probe,
        )

    sources = pi_run.harness_source_paths()
    assert Path(trades_bench.__file__).parent.resolve() in sources
    assert Path(policies.__file__).parent.resolve() in sources
    assert pi_run.SCENARIO_DIR.resolve() in sources
    assert REPO_ROOT in sources
    assert set(sources) | {secrets} <= set(probed)
    failure = json.loads((tmp_path / "run" / FAILURE_FILENAME).read_text(encoding="utf-8"))
    assert failure["run_status"] == pi_run.REFUSED_STATUS


def test_pi_env_names_are_carried_but_an_unset_one_refuses_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    args = parser().parse_args([*REQUIRED, "--pi-env", "OPENAI_API_KEY", "--pi-env", "XAI_API_KEY"])

    assert pi_config_from_args(args).secret_env == ("OPENAI_API_KEY", "XAI_API_KEY")
    code = pi_run.main(
        [
            "--run-dir",
            str(tmp_path / "run"),
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--thinking",
            "high",
            "--pi-env",
            "OPENAI_API_KEY",
            "--pi-env",
            "XAI_API_KEY",
        ]
    )

    assert code == pi_run.REFUSED_EXIT_CODE
    assert "XAI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()
