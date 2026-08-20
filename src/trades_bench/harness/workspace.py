from __future__ import annotations

import hashlib
from pathlib import Path

from trades_bench import docs_gen

MEMORY_FILENAME = "MEMORY.md"
DOCS_DIRNAME = "docs"


def memory_path(root: str | Path) -> Path:
    return Path(root) / MEMORY_FILENAME


def docs_path(root: str | Path) -> Path:
    return Path(root) / DOCS_DIRNAME


def create(root: str | Path) -> Path:
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    docs_gen.render_docs(docs_path(directory))
    memory = memory_path(directory)
    if not memory.exists():
        memory.write_bytes(b"")
    return directory


def workspace_digest(root: str | Path) -> str:
    directory = Path(root)
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(payload)
        digest.update(b"\x00")
    return digest.hexdigest()


__all__ = [
    "DOCS_DIRNAME",
    "MEMORY_FILENAME",
    "create",
    "docs_path",
    "memory_path",
    "workspace_digest",
]
