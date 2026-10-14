"""The computer: one data directory holding a blob store, trees as values, and `computer.db`. This class
owns the directory, the connection and the one lock that serializes writers; every statement it runs
is a function in `db/` (one module per table). Blobs and trees are immutable; refs are the only
mutable names, and they move by compare-and-swap."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from pathlib import Path

from . import db as _db
from .blobs import Blobs
from .db import NotFound, RefConflict, actions, events, refs, repos, seats, snapshots
from .trees import Trees

__all__ = ["Computer", "NotFound", "RefConflict"]


def locked(method: Callable) -> Callable:
    """Run the method under the computer's one writer lock. Every method that writes `computer.db`
    carries it; reads do not. The lock is re-entrant, so a locked method may call another."""

    @functools.wraps(method)
    def inner(self: Computer, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return inner


class Computer:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = Blobs(self.root / "blobs")
        self.trees = Trees(self.blobs)
        self.slots = self.root / "slots"
        self.repos_dir = self.root / "repos"
        self.slots.mkdir(exist_ok=True)
        self.repos_dir.mkdir(exist_ok=True)
        self.db = _db.connect(self.root / "computer.db")
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ seats
    @locked
    def seat_create(self, name: str, identity: str = "", system_prompt: str = "", loop_state: str = "") -> str:
        return seats.create(self.db, name, identity, system_prompt, loop_state)

    @locked
    def seat_rotate_token(self, name: str) -> str:
        return seats.rotate(self.db, name)

    @locked
    def seat_ensure(self, name: str, token: str) -> None:
        """The seat exists and answers to exactly this token (the host's `KC_SEAT_TOKEN`)."""
        seats.ensure(self.db, name, token)

    def seat_by_token(self, token: str) -> dict | None:
        return seats.by_token(self.db, token)

    def seat_get(self, name: str) -> dict:
        return seats.get(self.db, name)

    def seat_list(self) -> list[dict]:
        return seats.listing(self.db)

    @locked
    def seat_update(self, name: str, **fields: str) -> dict:
        seats.update(self.db, name, fields)
        return seats.get(self.db, name)

    @staticmethod
    def whoami(seat: dict) -> dict:
        return {
            "seat": seat["name"],
            "identity": seat["identity"],
            "system_prompt": seat["system_prompt"],
            "loop_state": seat["loop_state"],
            "since": seat["created_at"],
        }

    # ------------------------------------------------------------------- refs
    @locked
    def ref_create(
        self,
        name: str,
        tree: str,
        actor: str | None = None,
        base_ref: str | None = None,
        note: str | None = None,
    ) -> dict:
        self.trees.get(tree)  # must exist and be a tree
        return refs.create(self.db, name, tree, actor, base_ref, note)

    def ref_get(self, name: str) -> dict:
        return refs.get(self.db, name)

    def ref_list(self) -> list[dict]:
        return refs.listing(self.db)

    @locked
    def ref_move(
        self,
        name: str,
        expected_tree: str,
        new_tree: str,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Compare-and-swap: the ref moves only if it still points at expected_tree; otherwise the
        move refuses and names the tree found."""
        self.trees.get(new_tree)
        return refs.move(self.db, name, expected_tree, new_tree, actor, note)

    @locked
    def ref_delete(self, name: str) -> None:
        refs.delete(self.db, name)

    def ref_history(self, name: str, limit: int = 50) -> list[dict]:
        return refs.history(self.db, name, limit)

    def resolve_tree(self, ref_or_tree: str) -> str:
        """A ref name resolves to its current tree; a tree id passes through."""
        if not ref_or_tree:
            raise NotFound("empty ref or tree id")
        tree = refs.current_tree(self.db, ref_or_tree)
        if tree:
            return tree
        if self.blobs.has(ref_or_tree):
            return ref_or_tree
        raise NotFound(f"no ref or tree: {ref_or_tree}")

    # -------------------------------------------------------------- snapshots
    @locked
    def snapshot_create(self, name: str, actor: str | None = None, note: str | None = None) -> dict:
        """Record where every ref points right now under `name`. Nothing is copied; a name is taken once."""
        recorded = {r["name"]: {"tree": r["tree"], "rev": r["rev"]} for r in refs.listing(self.db)}
        snapshots.create(self.db, name, recorded, actor, note)
        return {"name": name, "refs": recorded}

    def snapshot_list(self) -> list[dict]:
        return snapshots.listing(self.db)

    @locked
    def snapshot_rollback(self, name: str, actor: str | None = None) -> dict:
        """Move every ref the snapshot recorded back to its recorded tree, by compare-and-swap, one
        history row per ref that differs. A ref discarded since is created again at its recorded tree;
        a ref created since is left alone (the snapshot never knew it). The snapshot itself stays."""
        wanted = snapshots.get(self.db, name)
        moved = []
        for ref, v in wanted.items():
            cur = refs.current_tree(self.db, ref)
            if cur is None:
                refs.create(self.db, ref, v["tree"], actor, note=f"rollback:{name}")
                moved.append(ref)
            elif cur != v["tree"]:
                refs.move(self.db, ref, cur, v["tree"], actor, note=f"rollback:{name}")
                moved.append(ref)
        return {"snapshot": name, "moved": moved}

    # ---------------------------------------------------------------- actions
    def action_get(self, key: str) -> dict | None:
        return actions.get(self.db, key)

    @locked
    def action_put(self, rec: dict) -> None:
        actions.put(self.db, rec)

    # ------------------------------------------------------------------ repos
    @locked
    def repo_put(self, name: str, path: Path, src: str | None) -> None:
        repos.put(self.db, name, path, src)

    def repo_get(self, name: str) -> dict:
        return repos.get(self.db, name)

    # ----------------------------------------------------------------- events
    @locked
    def event(
        self,
        actor: str | None,
        verb: str,
        args: dict | None,
        ok: bool,
        ms: int,
        bytes_in: int = 0,
        bytes_out: int = 0,
        error: str | None = None,
    ) -> None:
        events.put(self.db, actor, verb, args, ok, ms, bytes_in, bytes_out, error)

    def events_recent(self, n: int = 50) -> list[dict]:
        return events.recent(self.db, n)

    def metrics(self, window_s: float = 3600) -> dict:
        return events.metrics(self.db, window_s)
