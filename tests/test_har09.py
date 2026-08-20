from __future__ import annotations

import ast
from pathlib import Path

import trades_bench
from trades_bench.registry import OPERATIONS

PACKAGE_ROOT = Path(trades_bench.__file__).parent
HARNESS_ROOT = PACKAGE_ROOT / "harness"

FORBIDDEN_TEXT = (
    "MEMORY.md",
    "controlled_daily_reset",
    "native_continuous",
    "native_compaction",
    "is still unresolved",
    "Final reminder",
)

FORBIDDEN_ROUTE_ARGS = frozenset(
    {
        "harness",
        "harness_name",
        "harness_version",
        "context_policy",
        "attempt_definition",
        "system_prompt",
        "system_prompt_hash",
        "memory_file",
        "prompt",
        "prompt_text",
        "model_id",
        "track",
    }
)


def _engine_modules() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if HARNESS_ROOT not in path.parents and path != HARNESS_ROOT
    )


def _imported_targets(tree: ast.AST, module_path: Path) -> list[str]:
    package_parts = module_path.relative_to(PACKAGE_ROOT).parts[:-1]
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                prefix = ("trades_bench", *package_parts)[: len(package_parts) + 2 - node.level]
                base = ".".join((*prefix, node.module)) if node.module else ".".join(prefix)
            targets.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
            targets.append(base)
    return targets


def test_har09_engine_modules_import_no_harness_module():
    offenders: list[tuple[str, str]] = []
    for path in _engine_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in _imported_targets(tree, path):
            if target == "trades_bench.harness" or target.startswith("trades_bench.harness."):
                offenders.append((path.name, target))
    assert offenders == []


def test_har09_engine_modules_are_scanned_and_the_harness_exists():
    names = {path.name for path in _engine_modules()}
    assert {"controller.py", "invocations.py", "http_engine.py", "run_init.py"} <= names
    assert "scripted.py" not in names
    assert HARNESS_ROOT.is_dir()


def test_har09_engine_source_never_names_memory_context_policy_or_prompt_text():
    offenders: list[tuple[str, str]] = []
    for path in _engine_modules():
        source = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_TEXT:
            if needle in source:
                offenders.append((path.name, needle))
    assert offenders == []


def test_har09_no_public_operation_accepts_a_harness_parameter():
    offenders = [
        (spec.name, arg.name)
        for spec in OPERATIONS
        for arg in spec.args
        if arg.name in FORBIDDEN_ROUTE_ARGS
    ]
    assert offenders == []


def test_har09_harness_modules_may_depend_on_the_engine():
    harness_modules = sorted(HARNESS_ROOT.glob("*.py"))
    assert harness_modules
    dependents = 0
    for path in harness_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module != "harness":
                dependents += 1
                break
    assert dependents > 0
