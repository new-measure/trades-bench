from __future__ import annotations

import hashlib
import os
import tracemalloc
from pathlib import Path

import pytest

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


def test_workspace_digest_hashes_a_symlink_target_as_text_without_reading_it(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    private = tmp_path / "run.db"
    private.write_text("future calls", encoding="utf-8")
    (root / "peek").symlink_to(private)
    first = workspace.workspace_digest(root)

    private.write_text("different future calls", encoding="utf-8")

    assert workspace.workspace_digest(root) == first
    (root / "peek").unlink()
    (root / "peek").symlink_to(tmp_path / "elsewhere.db")
    assert workspace.workspace_digest(root) != first


def test_workspace_digest_never_descends_a_symlinked_directory(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    outside = tmp_path / "run"
    outside.mkdir()
    (outside / "index.jsonl").write_text("{}", encoding="utf-8")
    (root / "run-link").symlink_to(outside, target_is_directory=True)
    first = workspace.workspace_digest(root)

    (outside / "index.jsonl").write_text('{"kind": "checkpoint"}', encoding="utf-8")
    (outside / "new.jsonl").write_text("{}", encoding="utf-8")

    assert workspace.workspace_digest(root) == first


def test_workspace_digest_skips_a_fifo_without_blocking(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    before = workspace.workspace_digest(root)

    os.mkfifo(root / "pipe")

    assert workspace.workspace_digest(root) == before


def test_large_files_are_hashed_and_copied_in_bounded_chunks(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    workspace.create(root)
    sparse = root / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.truncate(64 * workspace.CHUNK_BYTES)
    tracemalloc.start()
    try:
        digest = workspace.workspace_digest(root)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(digest) == 64
    assert peak < 8 * workspace.CHUNK_BYTES
    expected = hashlib.sha256(bytes(64 * workspace.CHUNK_BYTES)).hexdigest()
    assert workspace.regular_file_sha256(sparse) == expected
    monkeypatch.setattr(workspace, "CHUNK_BYTES", 7)
    source = root / "MEMORY.md"
    source.write_bytes(b"notes that span several tiny chunks")
    target = tmp_path / "copy.md"
    assert workspace.copy_regular_file(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert not workspace.copy_regular_file(source, target)
    assert not workspace.copy_regular_file(root / "missing", tmp_path / "other.md")


def test_workspace_digest_hashes_a_name_that_is_not_utf8(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    before = workspace.workspace_digest(root)
    try:
        descriptor = os.open(os.fsencode(root) + b"/notes-\xff.md", os.O_CREAT | os.O_WRONLY, 0o644)
    except OSError:
        pytest.skip("the file system refuses names that are not UTF-8")
    os.close(descriptor)

    assert workspace.workspace_digest(root) != before


def test_workspace_digest_skips_a_path_longer_than_the_system_allows(tmp_path):
    root = tmp_path / "ws"
    workspace.create(root)
    before = workspace.workspace_digest(root)
    parent = os.open(root, os.O_RDONLY)
    try:
        for _ in range(20):
            os.close(os.open("f" * 250, os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=parent))
            os.mkdir("d" * 250, dir_fd=parent)
            child = os.open("d" * 250, os.O_RDONLY, dir_fd=parent)
            os.close(parent)
            parent = child
    finally:
        os.close(parent)

    assert len(workspace.workspace_digest(root)) == len(before)
