"""The `refs` and `ref_history` tables: the only mutable names in the computer. A ref points at a tree
id and moves only by compare-and-swap; every create and move is a history row (who, when, from, to).
The caller serializes writers; these functions assume one writer at a time."""

from __future__ import annotations

import sqlite3
import time

from . import NotFound, RefConflict


def create(
    db: sqlite3.Connection,
    name: str,
    tree: str,
    actor: str | None = None,
    base_ref: str | None = None,
    note: str | None = None,
) -> dict:
    """Insert a ref at `tree` with rev 0 and its first history row. `base_ref` makes it a branch: the
    ref it was cut from and the tree it was cut at are remembered for a later three-way merge."""
    now = time.time()
    if db.execute("SELECT 1 FROM refs WHERE name=?", (name,)).fetchone():
        raise ValueError(f"ref exists: {name}")
    db.execute(
        "INSERT INTO refs(name,tree,rev,base_ref,base_tree,updated_at) VALUES(?,?,0,?,?,?)",
        (name, tree, base_ref, tree if base_ref else None, now),
    )
    db.execute(
        "INSERT INTO ref_history(name,rev,old_tree,new_tree,actor,note,at) VALUES(?,0,NULL,?,?,?,?)",
        (name, tree, actor, note or "create", now),
    )
    return get(db, name)


def get(db: sqlite3.Connection, name: str) -> dict:
    row = db.execute("SELECT * FROM refs WHERE name=?", (name,)).fetchone()
    if not row:
        raise NotFound(f"ref not found: {name}")
    return dict(row)


def listing(db: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT * FROM refs ORDER BY name")]


def move(
    db: sqlite3.Connection,
    name: str,
    expected_tree: str,
    new_tree: str,
    actor: str | None = None,
    note: str | None = None,
) -> dict:
    """Compare-and-swap: the ref moves to `new_tree` only if it still points at `expected_tree`;
    otherwise `RefConflict` names the tree found. A move bumps `rev` and writes one history row."""
    now = time.time()
    row = db.execute("SELECT tree, rev FROM refs WHERE name=?", (name,)).fetchone()
    if not row:
        raise NotFound(f"ref not found: {name}")
    if row["tree"] != expected_tree:
        raise RefConflict(name, expected_tree, row["tree"])
    rev = row["rev"] + 1
    db.execute("UPDATE refs SET tree=?, rev=?, updated_at=? WHERE name=?", (new_tree, rev, now, name))
    db.execute(
        "INSERT INTO ref_history(name,rev,old_tree,new_tree,actor,note,at) VALUES(?,?,?,?,?,?,?)",
        (name, rev, expected_tree, new_tree, actor, note, now),
    )
    return get(db, name)


def delete(db: sqlite3.Connection, name: str) -> None:
    """Drop the ref. Its history rows stay; its trees stay (nothing deletes content)."""
    db.execute("DELETE FROM refs WHERE name=?", (name,))


def history(db: sqlite3.Connection, name: str, limit: int = 50) -> list[dict]:
    """The ref's moves, newest first."""
    return [
        dict(r) for r in db.execute("SELECT * FROM ref_history WHERE name=? ORDER BY rev DESC LIMIT ?", (name, limit))
    ]


def current_tree(db: sqlite3.Connection, name: str) -> str | None:
    """The tree a ref points at, or None when no ref has that name."""
    row = db.execute("SELECT tree FROM refs WHERE name=?", (name,)).fetchone()
    return row["tree"] if row else None
