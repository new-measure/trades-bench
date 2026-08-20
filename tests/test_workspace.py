from __future__ import annotations

from pathlib import Path

from trades_bench.harness import workspace


def test_create_materializes_docs_and_empty_memory(tmp_path):
    root = workspace.create(tmp_path / "ws")
    assert root.is_dir()
    assert workspace.memory_path(root).read_bytes() == b""
    docs = workspace.docs_path(root)
    assert docs.is_dir()
    assert any(entry.is_file() for entry in docs.iterdir())


def test_create_never_rewrites_existing_agent_files(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    workspace.memory_path(root).write_text("day one notes\n", encoding="utf-8")
    (root / "helper.py").write_text("print(1)\n", encoding="utf-8")
    (root / "exports").mkdir()
    (root / "exports" / "jobs.csv").write_text("id\n1\n", encoding="utf-8")

    workspace.create(root)

    assert workspace.memory_path(root).read_text(encoding="utf-8") == "day one notes\n"
    assert (root / "helper.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (root / "exports" / "jobs.csv").read_text(encoding="utf-8") == "id\n1\n"


def test_workspace_digest_is_stable_and_content_sensitive(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    first = workspace.workspace_digest(root)
    assert first == workspace.workspace_digest(root)
    workspace.memory_path(root).write_text("notes\n", encoding="utf-8")
    assert workspace.workspace_digest(root) != first


def test_workspace_digest_ignores_directory_walk_order(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        root.mkdir()
    for name in ("b.txt", "a.txt"):
        (left / name).write_text(name, encoding="utf-8")
    for name in ("a.txt", "b.txt"):
        (right / name).write_text(name, encoding="utf-8")
    assert workspace.workspace_digest(left) == workspace.workspace_digest(right)


def test_workspace_exposes_no_memory_injection_surface():
    for name in ("MEMORY_LABEL", "MEMORY_PLACEHOLDER", "memory_message", "truncation_notice"):
        assert not hasattr(workspace, name), name
        assert name not in workspace.__all__, name


def test_workspace_module_learns_no_private_paths():
    text = Path(workspace.__file__).read_text(encoding="utf-8").lower()
    for needle in ("checkpoint", "trajectory", "token", ".sqlite", "ready_file"):
        assert needle not in text, needle
