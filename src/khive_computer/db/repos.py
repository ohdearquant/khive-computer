"""The `repos` table: repositories imported into the computer, by name: where the clone is under `repos/`
and the source it came from."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import NotFound


def put(db: sqlite3.Connection, name: str, path: Path, src: str | None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO repos(name,path,src,created_at) VALUES(?,?,?,?)", (name, str(path), src, time.time())
    )


def get(db: sqlite3.Connection, name: str) -> dict:
    row = db.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
    if not row:
        raise NotFound(f"repo not found: {name}")
    return dict(row)
