from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import EngineError, ErrorCode

MANIFEST_FILENAME = "manifest.json"

_DOMAIN = b"csr-bench/geo-artifact/canonical/v1\x00"

_CHUNK_BYTES = 1 << 20


def artifact_file_paths(artifact_dir: Path | str) -> list[str]:
    root = Path(artifact_dir)
    if not root.is_dir():
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            "geography artifact path is not a directory",
            details={"path": str(root)},
        )
    paths = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    ]
    return sorted(rel for rel in paths if rel != MANIFEST_FILENAME)


def canonical_artifact_sha256(artifact_dir: Path | str) -> str:
    root = Path(artifact_dir)
    digest = hashlib.sha256()
    digest.update(_DOMAIN)
    for rel in artifact_file_paths(root):
        path = root / rel
        size = path.stat().st_size
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(size.to_bytes(8, "little"))
        with open(path, "rb") as f:
            while chunk := f.read(_CHUNK_BYTES):
                digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "MANIFEST_FILENAME",
    "artifact_file_paths",
    "canonical_artifact_sha256",
    "file_sha256",
]
