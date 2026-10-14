"""The `events` table: one row per executed tool call, under the verb `family.action`, with the seat, the
arguments (as JSON, cut at 4,000 characters), the outcome, the latency and the bytes in and out."""

from __future__ import annotations

import json
import sqlite3
import time


def put(
    db: sqlite3.Connection,
    actor: str | None,
    verb: str,
    args: dict | None,
    ok: bool,
    ms: int,
    bytes_in: int = 0,
    bytes_out: int = 0,
    error: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO events(at,actor,verb,args,ok,ms,bytes_in,bytes_out,error) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            time.time(),
            actor,
            verb,
            json.dumps(args, default=str)[:4000] if args is not None else None,
            int(ok),
            ms,
            bytes_in,
            bytes_out,
            error,
        ),
    )


def recent(db: sqlite3.Connection, n: int = 50) -> list[dict]:
    """The last n events, newest first."""
    return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (n,))]


def metrics(db: sqlite3.Connection, window_s: float = 3600) -> dict:
    """Calls, errors, bytes and per-verb latency percentiles over the last `window_s` seconds."""
    since = time.time() - window_s
    rows = [
        dict(r)
        for r in db.execute(
            "SELECT at, verb, ok, ms, bytes_in, bytes_out FROM events WHERE at>=? ORDER BY at",
            (since,),
        )
    ]
    if not rows:
        return {"window_s": window_s, "calls": 0}
    span = max(rows[-1]["at"] - rows[0]["at"], 1.0)
    per_sec: dict[int, int] = {}
    for r in rows:
        per_sec[int(r["at"])] = per_sec.get(int(r["at"]), 0) + 1
    by_verb: dict[str, list[int]] = {}
    for r in rows:
        by_verb.setdefault(r["verb"], []).append(r["ms"])

    def pct(xs: list[int], p: float) -> int:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, round((len(xs) - 1) * p))]

    return {
        "window_s": window_s,
        "calls": len(rows),
        "errors": sum(1 for r in rows if not r["ok"]),
        "span_s": round(span, 1),
        "avg_calls_per_s": round(len(rows) / span, 3),
        "peak_calls_per_s": max(per_sec.values()),
        "bytes_in": sum(r["bytes_in"] for r in rows),
        "bytes_out": sum(r["bytes_out"] for r in rows),
        "per_verb": {
            v: {"n": len(xs), "p50_ms": pct(xs, 0.5), "p99_ms": pct(xs, 0.99), "max_ms": max(xs)}
            for v, xs in sorted(by_verb.items())
        },
    }
