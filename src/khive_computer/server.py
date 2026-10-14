"""The MCP face of the computer: one tool per family of actions (seat, fs, ref, branch, exec, git,
snapshot, computer), the action named in the call, a bearer token per seat, every call an event with
its actor, latency and bytes."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import execer, fs, gitio
from .computer import Computer

INSTRUCTIONS = """You are connected to a khive computer. Eight tools, each a family of actions picked by
the `action` argument: seat, fs, ref, branch, exec, git, snapshot, computer. Call seat(action="whoami")
first: it tells you which seat you are, its identity and its current loop state. Files live in trees; a
tree id is a value and a ref is a named pointer to one. Read with fs read, change with fs edit or fs
write (each returns a NEW tree id), run commands with exec run on a tree, and publish by moving the ref
(ref move) with the tree you started from as expected_tree. Export and push through git export and git
push. Nothing on your side needs to persist."""

FAMILIES = ("seat", "fs", "ref", "branch", "exec", "git", "snapshot", "computer")


class SeatVerifier(TokenVerifier):
    def __init__(self, computer: Computer):
        super().__init__()
        self.computer = computer

    async def verify_token(self, token: str) -> AccessToken | None:
        seat = self.computer.seat_by_token(token)
        if not seat:
            return None
        return AccessToken(
            token=token,
            client_id=seat["name"],
            scopes=["computer"],
            claims={"seat": seat["name"]},
        )


def _actor() -> str:
    tok = get_access_token()
    if tok is None:
        raise PermissionError("no seat: this computer takes a bearer token per seat")
    return tok.client_id


def _size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return 0


def _params(sig: inspect.Signature) -> str:
    return ", ".join(str(p) for p in sig.parameters.values())


def build_server(c: Computer, name: str = "khive-computer") -> FastMCP:
    mcp = FastMCP(name, instructions=INSTRUCTIONS, auth=SeatVerifier(c))

    def family(fam: str, summary: str, actions: dict[str, Callable]) -> None:
        """Register one tool for a family of actions. Its parameters are `action` plus the union of the
        actions' parameters, all optional in the schema; the chosen action's own signature is what the
        call is bound to, and a call that does not fit is refused naming that signature. Every call
        lands as an event under the verb `<family>.<action>`."""
        sigs = {a: inspect.signature(fn, eval_str=True) for a, fn in actions.items()}
        union: dict[str, Any] = {}
        for sig in sigs.values():
            for p in sig.parameters.values():
                ann = p.annotation if p.annotation is not inspect.Parameter.empty else Any
                if p.name in union and union[p.name] != ann:
                    ann = Any
                union[p.name] = ann
        lines = [summary, "", "Actions:"]
        for a, fn in actions.items():
            first = (inspect.getdoc(fn) or "").split("\n\n")[0].replace("\n", " ")
            lines.append(f"- {a}({_params(sigs[a])}): {first}")

        async def tool(**kwargs):
            action = kwargs.pop("action", None)
            fn = actions.get(action)
            if fn is None:
                raise ValueError(f"{fam}: unknown action {action!r}; actions are {', '.join(actions)}")
            given = {k: v for k, v in kwargs.items() if v is not None}
            try:
                bound = sigs[action].bind(**given)
            except TypeError as e:
                raise ValueError(f"{fam}.{action}: {e}; takes ({_params(sigs[action])})") from None
            actor = _actor()
            verb = f"{fam}.{action}"
            t0 = time.time()
            try:
                result = await asyncio.to_thread(fn, *bound.args, **bound.kwargs)
                c.event(actor, verb, given, True, int((time.time() - t0) * 1000), _size(given), _size(result))
                return result
            except Exception as e:  # the error itself is the result the caller sees
                c.event(
                    actor,
                    verb,
                    given,
                    False,
                    int((time.time() - t0) * 1000),
                    _size(given),
                    0,
                    f"{type(e).__name__}: {e}",
                )
                raise

        action_t = Literal[tuple(actions)]
        params = [inspect.Parameter("action", inspect.Parameter.KEYWORD_ONLY, annotation=action_t)]
        params += [
            inspect.Parameter(k, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=v | None)
            for k, v in union.items()
        ]
        tool.__signature__ = inspect.Signature(params, return_annotation=dict | list)
        tool.__annotations__ = {"action": action_t, **{k: v | None for k, v in union.items()}, "return": dict | list}
        tool.__name__ = tool.__qualname__ = fam
        tool.__doc__ = "\n".join(lines)
        mcp.tool(tool, name=fam)

    # ------------------------------------------------------------- identity
    def whoami() -> dict:
        """Who am I on this computer: seat name, identity, system prompt, loop state."""
        seat = c.seat_get(_actor())
        out = Computer.whoami(seat)
        out["computer"] = c.root.name
        return out

    def seat_update(
        loop_state: str | None = None,
        identity: str | None = None,
        system_prompt: str | None = None,
    ) -> dict:
        """Write back the seat's own loop state (or identity / system prompt)."""
        fields = {
            k: v
            for k, v in {
                "loop_state": loop_state,
                "identity": identity,
                "system_prompt": system_prompt,
            }.items()
            if v is not None
        }
        seat = c.seat_update(_actor(), **fields)
        return {"seat": seat["name"], "updated": sorted(fields)}

    # ----------------------------------------------------------------- refs
    def ref_list() -> list[dict]:
        """Every named ref on the computer with its current tree id and revision."""
        return c.ref_list()

    def ref_get(name: str) -> dict:
        """One ref: tree, rev, base."""
        return c.ref_get(name)

    def ref_create(name: str, tree: str, note: str | None = None) -> dict:
        """Create a ref pointing at an existing tree id."""
        return c.ref_create(name, tree, _actor(), note=note)

    def ref_move(name: str, expected_tree: str, new_tree: str, note: str | None = None) -> dict:
        """Move a ref by compare-and-swap. A mismatch refuses and names the tree found; every move is
        a row in the ref's history."""
        return c.ref_move(name, expected_tree, new_tree, _actor(), note)

    def ref_history(name: str, limit: int = 50) -> list[dict]:
        """Every move of a ref, newest first."""
        return c.ref_history(name, limit)

    # ------------------------------------------------------------------- fs
    def fs_read(tree: str, path: str, offset: int = 1, limit: int = 200) -> dict:
        """Read a file from a tree (or ref) as numbered lines, like a Read tool."""
        return fs.read(c, tree, path, offset, limit)

    def fs_list(tree: str, path: str = "") -> dict:
        """List a directory of a tree (or ref)."""
        return fs.list_dir(c, tree, path)

    def fs_glob(tree: str, pattern: str, limit: int = 500) -> dict:
        """Find paths in a tree by glob pattern (matched on the full path and on the basename)."""
        return fs.glob(c, tree, pattern, limit)

    def fs_grep(
        tree: str,
        pattern: str,
        path: str = "",
        limit: int = 200,
        ignore_case: bool = False,
    ) -> dict:
        """Regex search across the text files of a tree (or under one path)."""
        return fs.grep(c, tree, pattern, path, limit, ignore_case)

    def fs_write(tree: str, path: str, content: str, executable: bool | None = None) -> dict:
        """Write a whole file. Returns a NEW tree id; the input tree is unchanged."""
        return fs.write(c, tree, path, content, executable)

    def fs_edit(tree: str, path: str, old: str, new: str, replace_all: bool = False) -> dict:
        """Replace one exact old string with new in a file. Returns a NEW tree id."""
        return fs.edit(c, tree, path, old, new, replace_all)

    def fs_remove(tree: str, path: str) -> dict:
        """Remove a path. Returns a NEW tree id."""
        return fs.remove(c, tree, path)

    def tree_empty() -> dict:
        """The empty tree, so a client can start a ref from nothing."""
        return {"tree": c.trees.empty()}

    def tree_diff(a: str, b: str) -> dict:
        """Paths that differ between two trees (or refs)."""
        return fs.diff(c, a, b)

    # ----------------------------------------------------------------- exec
    def exec_run(
        tree: str,
        argv: list[str],
        cwd: str = ".",
        env: dict[str, str] | None = None,
        timeout: int = 600,
        sandbox: str = "local",
        image: str | None = None,
        fresh: bool = False,
        caches: dict[str, str] | None = None,
        network: bool = False,
        gitignore: bool = False,
    ) -> dict:
        """Run a command against a tree in a fresh sandboxed directory. Returns exit code, output
        tails, the result tree id and the changed paths. Same inputs answer from the cache unless fresh.
        `caches` = {name: path} named persistent directories mounted into the action (build caches,
        registries) and excluded from the result tree; `network` opens egress (off by default);
        `gitignore` keeps out of the result tree whatever the tree's own .gitignore files ignore."""
        res = execer.run(
            c,
            tree,
            argv,
            cwd,
            env,
            timeout,
            sandbox,
            image,
            fresh,
            actor=_actor(),
            caches=caches,
            network=network,
            gitignore=gitignore,
        )
        return execer.attach_tails(c, res)

    def exec_output(blob: str, offset: int = 1, limit: int = 400) -> dict:
        """Read a stored stdout/stderr blob of an action as numbered lines."""
        data = c.blobs.get(blob).decode("utf-8", errors="replace").split("\n")
        chunk = data[offset - 1 : offset - 1 + limit]
        return {
            "blob": blob,
            "offset": offset,
            "total_lines": len(data),
            "content": "\n".join(f"{offset + i:6d}\t{line}" for i, line in enumerate(chunk)),
        }

    # ------------------------------------------------------------------ git
    def git_import(src: str, name: str, ref: str = "HEAD", gitignore: bool = False) -> dict:
        """Clone a repository (path or URL) into the computer and bind ref `name` to its tree at `ref`.
        `gitignore` leaves out what the clone's own .gitignore files ignore."""
        return gitio.import_repo(c, src, name, ref, actor=_actor(), gitignore=gitignore)

    def git_export(
        name: str,
        tree: str,
        branch: str,
        message: str,
        start_point: str | None = None,
        author: str | None = None,
    ) -> dict:
        """Commit a tree (or ref) as a commit on `branch` of the imported repository `name`.
        `author` = "Name <email>" when the repository requires one; the committer is the computer."""
        return gitio.export(c, name, tree, branch, message, actor=_actor(), start_point=start_point, author=author)

    def git_push(name: str, branch: str, remote: str = "origin") -> dict:
        """Push a branch of an imported repository with the computer's own credentials."""
        return gitio.push(c, name, branch, remote)

    def git_log(name: str, n: int = 10) -> dict:
        """Recent commits of an imported repository's current branch."""
        return gitio.log(c, name, n)

    # ------------------------------------------------------------ snapshots
    def snapshot_create(name: str, note: str | None = None) -> dict:
        """Record every ref's current tree under a name."""
        return c.snapshot_create(name, _actor(), note)

    def snapshot_list() -> list[dict]:
        """All snapshots."""
        return c.snapshot_list()

    def snapshot_rollback(name: str) -> dict:
        """Move every ref back to where the snapshot recorded it (each move is in ref_history)."""
        return c.snapshot_rollback(name, _actor())

    def branch_create(name: str, from_ref: str, note: str | None = None) -> dict:
        """Create a new ref at the same tree as from_ref, remembering the base for merge."""
        base = c.ref_get(from_ref)
        return c.ref_create(
            name,
            base["tree"],
            _actor(),
            base_ref=from_ref,
            note=note or f"branch of {from_ref}",
        )

    def branch_merge(into: str, from_ref: str, note: str | None = None) -> dict:
        """Three-way merge from_ref into `into` using the branch's base tree. Moves `into` on a
        clean merge; on conflicts returns the conflicted paths and moves nothing."""
        from .merge import three_way

        src, dst = c.ref_get(from_ref), c.ref_get(into)
        base = src["base_tree"] or dst["tree"]
        result = three_way(c, base, dst["tree"], src["tree"])
        if result["conflicts"]:
            return {"merged": False, **result}
        moved = c.ref_move(into, dst["tree"], result["tree"], _actor(), note or f"merge {from_ref}")
        return {"merged": True, "into": moved, **result}

    def branch_discard(name: str) -> dict:
        """Delete a ref. Its trees stay in the store; nothing it pointed at is lost."""
        c.ref_get(name)
        c.ref_delete(name)
        return {"deleted": name}

    # -------------------------------------------------------------- metrics
    def metrics(window_s: float = 3600) -> dict:
        """Calls per second, bytes, p50/p99 per verb over the window, from the events table."""
        return c.metrics(window_s)

    def events_recent(n: int = 50) -> list[dict]:
        """The last n events on this computer."""
        return c.events_recent(n)

    family(
        fam="seat",
        summary="The seat this token is: who am I, and write back my own loop state.",
        actions={
            "whoami": whoami,
            "update": seat_update,
        },
    )
    family(
        fam="fs",
        summary="Files in a tree. Reads take a tree id or a ref name; writes return a NEW tree id.",
        actions={
            "read": fs_read,
            "list": fs_list,
            "glob": fs_glob,
            "grep": fs_grep,
            "write": fs_write,
            "edit": fs_edit,
            "remove": fs_remove,
            "diff": tree_diff,
            "empty": tree_empty,
        },
    )
    family(
        fam="ref",
        summary="Named pointers to trees. A ref moves only by compare-and-swap; every move is a history row.",
        actions={
            "list": ref_list,
            "get": ref_get,
            "create": ref_create,
            "move": ref_move,
            "history": ref_history,
        },
    )
    family(
        fam="branch",
        summary="A branch is a ref with a remembered base: create one, merge it three-way, discard it.",
        actions={
            "create": branch_create,
            "merge": branch_merge,
            "discard": branch_discard,
        },
    )
    family(
        fam="exec",
        summary="Run a command on a tree in a sandbox; the same inputs answer from the result cache.",
        actions={
            "run": exec_run,
            "output": exec_output,
        },
    )
    family(
        fam="git",
        summary="Git at the edge: import a repository into a ref, export a tree as a commit, push, log.",
        actions={
            "import": git_import,
            "export": git_export,
            "push": git_push,
            "log": git_log,
        },
    )
    family(
        fam="snapshot",
        summary="Every ref's tree at a moment, by name; rollback is a compare-and-swap move per ref.",
        actions={
            "create": snapshot_create,
            "list": snapshot_list,
            "rollback": snapshot_rollback,
        },
    )
    family(
        fam="computer",
        summary="The computer about itself: call metrics over a window, the last events.",
        actions={
            "metrics": metrics,
            "events": events_recent,
        },
    )

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> JSONResponse:
        """The one anonymous route. Whatever runs the process (Docker, a cloud machine) asks it whether the
        server answers; it says `{"ok": true}` and nothing about the computer: no name, no counts, no
        schema. Readiness is this answer, not the process existing; a seat getting in is `seat.whoami`."""
        return JSONResponse({"ok": True})

    return mcp
