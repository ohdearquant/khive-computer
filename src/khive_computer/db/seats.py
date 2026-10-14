"""The `seats` table: one identity that acts, one token stored as a SHA-256 hash and never in the clear."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid

from . import NotFound

UPDATABLE = frozenset({"identity", "system_prompt", "loop_state"})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint() -> str:
    """A fresh token: `kc_` and 192 random bits. The shape every seat token has."""
    return "kc_" + secrets.token_hex(24)


def create(db: sqlite3.Connection, name: str, identity: str = "", system_prompt: str = "", loop_state: str = "") -> str:
    """Insert a seat and return its token, the only time the token exists in the clear."""
    token = mint()
    now = time.time()
    db.execute(
        "INSERT INTO seats(id,name,token_hash,identity,system_prompt,loop_state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), name, hash_token(token), identity, system_prompt, loop_state, now, now),
    )
    return token


def rotate(db: sqlite3.Connection, name: str) -> str:
    """Replace the seat's token; the old one stops matching at once."""
    token = mint()
    cur = db.execute("UPDATE seats SET token_hash=?, updated_at=? WHERE name=?", (hash_token(token), time.time(), name))
    if cur.rowcount == 0:
        raise NotFound(name)
    return token


def ensure(db: sqlite3.Connection, name: str, token: str) -> None:
    """A seat whose token is exactly `token`: created if missing, re-keyed if present. The host's bootstrap
    path (`KC_SEAT_TOKEN`): the token is chosen outside, only its hash is stored here."""
    now = time.time()
    cur = db.execute("UPDATE seats SET token_hash=?, updated_at=? WHERE name=?", (hash_token(token), now, name))
    if cur.rowcount == 0:
        db.execute(
            "INSERT INTO seats(id,name,token_hash,identity,system_prompt,loop_state,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), name, hash_token(token), "", "", "", now, now),
        )


def by_token(db: sqlite3.Connection, token: str) -> dict | None:
    row = db.execute("SELECT * FROM seats WHERE token_hash=?", (hash_token(token),)).fetchone()
    return dict(row) if row else None


def get(db: sqlite3.Connection, name: str) -> dict:
    row = db.execute("SELECT * FROM seats WHERE name=?", (name,)).fetchone()
    if not row:
        raise NotFound(name)
    return dict(row)


def listing(db: sqlite3.Connection) -> list[dict]:
    return [
        {"name": r["name"], "created_at": r["created_at"]}
        for r in db.execute("SELECT name, created_at FROM seats ORDER BY name")
    ]


def update(db: sqlite3.Connection, name: str, fields: dict[str, str]) -> None:
    """Set the seat's texts. Only `identity`, `system_prompt` and `loop_state` are updatable; the name,
    the token and the timestamps are not."""
    bad = set(fields) - UPDATABLE
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE seats SET {sets}, updated_at=? WHERE name=?", (*fields.values(), time.time(), name))
