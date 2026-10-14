"""The computer's SQLite file: how it is opened, what tables it has, and every statement that reads or
writes them. The tables are `schema.sql`; the statements are one module per table (`seats`, `refs`,
`snapshots`, `actions`, `events`, `repos`), each a set of functions over a connection. Nothing here
knows about blobs, trees, slots or locks; `computer.py` holds those and calls in."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA = resources.files(__package__).joinpath("schema.sql").read_text()


class RefConflict(Exception):
    """A compare-and-swap that found the ref somewhere else than expected."""

    def __init__(self, name: str, expected: str | None, found: str | None):
        super().__init__(f"ref {name}: expected {expected}, found {found}")
        self.name, self.expected, self.found = name, expected, found


class NotFound(KeyError):
    pass


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (or create) `computer.db` the way the computer uses it: WAL so readers do not block the
    writer, `synchronous=NORMAL` (durable across a process crash, not across power loss before the
    checkpoint), autocommit (`isolation_level=None`; the computer serializes writers with its own lock),
    rows as `sqlite3.Row`, and the schema applied (every statement is IF NOT EXISTS, so an older
    computer gains missing tables and loses nothing)."""
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db
