"""Content-addressed blob store: bytes in, BLAKE3 id out. Immutable, deduplicated."""

from __future__ import annotations

import os
from pathlib import Path

import blake3


class Blobs:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data: bytes) -> str:
        return blake3.blake3(data).hexdigest()

    def _path(self, cid: str) -> Path:
        return self.root / cid[:2] / cid[2:]

    def put(self, data: bytes) -> str:
        cid = self.digest(data)
        p = self._path(cid)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp." + str(os.getpid()))
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return cid

    def get(self, cid: str) -> bytes:
        p = self._path(cid)
        if not p.exists():
            raise KeyError(f"blob not found: {cid}")
        data = p.read_bytes()
        if self.digest(data) != cid:
            raise ValueError(f"blob corrupt on disk: {cid}")
        return data

    def has(self, cid: str) -> bool:
        return self._path(cid).exists()

    def size(self, cid: str) -> int:
        return self._path(cid).stat().st_size

    def path(self, cid: str) -> Path:
        """Host path of the blob file, for clone-based materialization."""
        return self._path(cid)
