"""The `actions` table: the result cache. One row per action key (a hash of the action's inputs) with
what the run produced; the same key is answered from here without a run."""

from __future__ import annotations

import sqlite3


def get(db: sqlite3.Connection, key: str) -> dict | None:
    row = db.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def put(db: sqlite3.Connection, rec: dict) -> None:
    """Store (or replace) the result under its key. `rec` carries the column names as keys."""
    db.execute(
        "INSERT OR REPLACE INTO actions(key,tree_in,argv,cwd,env,sandbox,tree_out,exit_code,stdout,stderr,ms,actor,at)"
        " VALUES(:key,:tree_in,:argv,:cwd,:env,:sandbox,:tree_out,:exit_code,:stdout,:stderr,:ms,:actor,:at)",
        rec,
    )
