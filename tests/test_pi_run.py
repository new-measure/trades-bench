from __future__ import annotations

import pytest

from policies.pi_run import parser
from trades_bench.harness.pi_adapter import PiConfig


def test_pi_config_requires_provider_model_and_thinking():
    with pytest.raises(TypeError):
        PiConfig()


def test_pi_run_parser_requires_provider_model_and_thinking():
    required = ("--provider", "--model", "--thinking")
    for omitted in required:
        arguments = [
            "--run-dir",
            "runs/example",
            "--provider",
            "opencode-go",
            "--model",
            "gpt-5.6-luna",
            "--thinking",
            "high",
        ]
        index = arguments.index(omitted)
        del arguments[index : index + 2]
        with pytest.raises(SystemExit) as exc_info:
            parser().parse_args(arguments)
        assert exc_info.value.code == 2


def test_pi_run_parser_accepts_explicit_agent_configuration():
    args = parser().parse_args(
        [
            "--run-dir",
            "runs/example",
            "--provider",
            "opencode-go",
            "--model",
            "gpt-5.6-luna",
            "--thinking",
            "high",
        ]
    )
    assert (args.provider, args.model, args.thinking) == (
        "opencode-go",
        "gpt-5.6-luna",
        "high",
    )
