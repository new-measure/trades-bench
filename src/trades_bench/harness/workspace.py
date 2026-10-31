from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from trades_bench import docs_gen

MEMORY_FILENAME = "MEMORY.md"
DOCS_DIRNAME = "docs"
CHUNK_BYTES = 1 << 20


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
    for parts in sorted(_entries(directory)):
        relative = os.fsencode("/".join(parts))
        path = directory.joinpath(*parts)
        try:
            mode = path.lstat().st_mode
            target = os.readlink(path) if stat.S_ISLNK(mode) else None
        except OSError:
            continue
        if target is not None:
            digest.update(relative)
            digest.update(b"\x00symlink\x00")
            digest.update(os.fsencode(target))
            digest.update(b"\x00")
            continue
        content = regular_file_sha256(path)
        if content is None:
            continue
        digest.update(relative)
        digest.update(b"\x00")
        digest.update(content.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _entries(directory: Path) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        base = Path(current).relative_to(directory).parts
        for name in dirnames:
            if os.path.islink(os.path.join(current, name)):
                found.append((*base, name))
        found.extend((*base, name) for name in filenames)
    return found


def open_regular_file(path: str | Path) -> BinaryIO | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    handle = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        return None
    return handle


def regular_file_chunks(handle: BinaryIO) -> Iterator[bytes]:
    while chunk := handle.read(CHUNK_BYTES):
        yield chunk


def regular_file_sha256(path: str | Path) -> str | None:
    handle = open_regular_file(path)
    if handle is None:
        return None
    digest = hashlib.sha256()
    with handle:
        for chunk in regular_file_chunks(handle):
            digest.update(chunk)
    return digest.hexdigest()


def copy_regular_file(
    source: str | Path, target: str | Path, *, max_bytes: int | None = None
) -> bool:
    handle = open_regular_file(source)
    if handle is None:
        return False
    with handle:
        if max_bytes is not None and os.fstat(handle.fileno()).st_size > max_bytes:
            return False
        try:
            with open(target, "xb") as written:
                for chunk in regular_file_chunks(handle):
                    written.write(chunk)
        except FileExistsError:
            return False
    return True


__all__ = [
    "CHUNK_BYTES",
    "DOCS_DIRNAME",
    "MEMORY_FILENAME",
    "copy_regular_file",
    "create",
    "docs_path",
    "memory_path",
    "open_regular_file",
    "regular_file_chunks",
    "regular_file_sha256",
    "workspace_digest",
]
