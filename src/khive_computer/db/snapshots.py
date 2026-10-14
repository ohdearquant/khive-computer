"""The `snapshots` table: where every ref pointed at one moment, as one JSON map of ref to (tree, rev)
under a name. Nothing is copied: trees are values and blobs are never deleted. A name is taken once."""

from __future__ import annotations

import json
import sqlite3
import time

from . import NotFound


def create(db: sqlite3.Connection, name: str, refs: dict[str, dict], actor: str | None, note: str | None) -> None:
    """Record `refs` (ref name to {tree, rev}) under `name`. A name that exists is refused: a snapshot is a
    point to go back to, and replacing it silently would move that point under whoever counts on it."""
    if db.execute("SELECT 1 FROM snapshots WHERE name=?", (name,)).fetchone():
        raise ValueError(f"snapshot exists: {name}; pick another name (snapshots are not replaced)")
    db.execute(
        "INSERT INTO snapshots(name,refs,actor,note,at) VALUES(?,?,?,?,?)",
        (name, json.dumps(refs, sort_keys=True), actor, note, time.time()),
    )


def get(db: sqlite3.Connection, name: str) -> dict[str, dict]:
    """The recorded map, ref name to {tree, rev}."""
    row = db.execute("SELECT refs FROM snapshots WHERE name=?", (name,)).fetchone()
    if not row:
        raise NotFound(f"snapshot not found: {name}")
    return json.loads(row["refs"])


def listing(db: sqlite3.Connection) -> list[dict]:
    return [
        {"name": r["name"], "at": r["at"], "refs": json.loads(r["refs"]), "note": r["note"]}
        for r in db.execute("SELECT * FROM snapshots ORDER BY at")
    ]
