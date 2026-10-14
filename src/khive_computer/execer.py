"""Running a command is an action: a tree in, argv, a closed environment; a result tree out.
Keyed by its inputs and cached. The process runs in a fresh slot directory that is removed
afterwards; sandbox "local" is that directory plus a timeout, sandbox "docker" adds
--network none and a container filesystem."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import blake3

from .computer import Computer
from .trees import DEFAULT_IGNORE

DEFAULT_IMAGE = "python:3.12-slim"


def _default_path() -> str:
    """What an action can call: the tools installed beside the server (ruff, pytest from the toolbox
    group), the toolchain under `CARGO_HOME` when one is configured (the image puts it at /opt/cargo),
    the system directories, and the server user's own `~/.cargo/bin` and `~/.local/bin` only when
    they exist: a directory the action user cannot enter makes the executable search fail with
    EACCES instead of moving on, so nothing goes on this PATH on the chance that it is there."""
    dirs = [f"{sys.prefix}/bin"]
    if os.environ.get("CARGO_HOME"):
        dirs.append(str(Path(os.environ["CARGO_HOME"]) / "bin"))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/local/cargo/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    dirs += [str(d) for d in (Path.home() / ".cargo" / "bin", Path.home() / ".local" / "bin") if d.is_dir()]
    return ":".join(dict.fromkeys(dirs))


DEFAULT_PATH = _default_path()
TAIL_LINES = 120


def action_user() -> tuple[int, int] | None:
    """The uid/gid actions run as when KC_ACTION_USER names a system user; None runs them as the
    server (the laptop case). In the container image the server is root and actions run as `kc`, so a
    file the tree marks read-only is read-only to the action and nothing it does reaches the
    computer's own store."""
    name = os.environ.get("KC_ACTION_USER")
    if not name:
        return None
    import pwd

    pw = pwd.getpwnam(name)
    return pw.pw_uid, pw.pw_gid


def check_free_space(c: Computer) -> int:
    """Refuse to start an action when the computer's volume is below KC_MIN_FREE_GB (default 3) free.
    khive-db keeps a 1 GiB disk-reserve floor and refuses writes under it, which turned a full volume
    into seven unrelated test reds; refusing up front names the cause instead."""
    floor_gb = float(os.environ.get("KC_MIN_FREE_GB", "3"))
    free = shutil.disk_usage(c.root).free
    if free < floor_gb * 2**30:
        raise RuntimeError(
            f"computer volume has {free / 2**30:.1f} GB free, below the {floor_gb:g} GB floor; prune caches first"
        )
    return free


def _chown(path: Path, ids: tuple[int, int] | None) -> None:
    """Hand a directory tree to the action user. Materialized files are copies, never links into the
    blob store, so this never changes what the store owns."""
    if ids is None:
        return
    subprocess.run(["chown", "-R", f"{ids[0]}:{ids[1]}", str(path)], check=True, capture_output=True)


def prepare_action_user(c: Computer) -> dict:
    """Once per serve: the shared cache directories and CARGO_HOME belong to the action user, so a
    build as that user can write them. A marker records the uid handed over and a repeat start with
    the same uid skips the walk."""
    ids = action_user()
    if ids is None:
        return {"action_user": None}
    caches = c.root / "caches"
    caches.mkdir(exist_ok=True)
    marker = caches / ".owner"
    if marker.exists() and marker.read_text().strip() == str(ids[0]):
        return {"action_user": os.environ["KC_ACTION_USER"], "uid": ids[0], "chowned": []}
    roots = [caches] + [Path(p) for p in [os.environ.get("CARGO_HOME")] if p and Path(p).exists()]
    for r in roots:
        _chown(r, ids)
    marker.write_text(str(ids[0]))
    return {"action_user": os.environ["KC_ACTION_USER"], "uid": ids[0], "chowned": [str(r) for r in roots]}


def _tail(data: bytes, n: int = TAIL_LINES) -> str:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if len(lines) > n:
        return f"... ({len(lines) - n} lines above)\n" + "\n".join(lines[-n:])
    return text


def action_key(
    tree: str,
    argv: list[str],
    cwd: str,
    env: dict,
    sandbox: str,
    image: str | None,
    network: bool = False,
    gitignore: bool = False,
) -> str:
    payload = json.dumps(
        {
            "tree": tree,
            "argv": argv,
            "cwd": cwd,
            "env": env,
            **({"gitignore": True} if gitignore else {}),
            "network": network,
            "sandbox": sandbox,
            "image": image,
        },
        sort_keys=True,
    )
    return blake3.blake3(payload.encode()).hexdigest()


def run(
    c: Computer,
    ref_or_tree: str,
    argv: list[str],
    cwd: str = ".",
    env: dict[str, str] | None = None,
    timeout: int = 600,
    sandbox: str = "local",
    image: str | None = None,
    fresh: bool = False,
    actor: str | None = None,
    ignore: tuple[str, ...] = DEFAULT_IGNORE,
    caches: dict[str, str] | None = None,
    network: bool = False,
    gitignore: bool = False,
) -> dict:
    tree = c.resolve_tree(ref_or_tree)
    caches = dict(caches or {})
    for name, where in caches.items():
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ValueError(f"bad cache name: {name!r}")
        if sandbox == "local" and (where.startswith("/") or ".." in where.split("/")):
            raise ValueError(f"local sandbox caches mount inside the work tree only: {where!r}")
    env = dict(env or {})
    if sandbox not in ("local", "docker", "container"):
        raise ValueError("sandbox must be local, docker or container")
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    key = action_key(tree, argv, cwd, env, sandbox, image, network, gitignore)
    if not fresh:
        hit = c.action_get(key)
        if hit and hit["tree_out"]:
            return _result(hit, cached=True, changed=c.trees.diff(tree, hit["tree_out"]))
    check_free_space(c)

    slot = c.slots / uuid.uuid4().hex
    mount, home, tmp = slot / "m0", slot / "home", slot / "tmp"
    for d in (home, tmp):
        d.mkdir(parents=True)
    t0 = time.time()
    c.trees.materialize(tree, mount)
    workdir = (mount / cwd).resolve()
    if mount not in workdir.parents and workdir != mount:
        shutil.rmtree(slot, ignore_errors=True)
        raise ValueError("cwd escapes the mount")

    closed_env = {
        "PATH": env.pop("PATH", DEFAULT_PATH),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "KC_SLOT": str(slot),
        "KC_TREE": tree,
    }
    # build caches are declared inputs, not inherited ambient state; CARGO_HOME is the one
    # exception so a build does not re-download the registry on every action, and RUSTUP_HOME
    # rides with it because the cargo on PATH is a rustup proxy that resolves the toolchain there
    if "CARGO_HOME" in os.environ:
        closed_env["CARGO_HOME"] = os.environ["CARGO_HOME"]
    elif (Path.home() / ".cargo").exists():
        closed_env["CARGO_HOME"] = str(Path.home() / ".cargo")
    if "RUSTUP_HOME" in os.environ:
        closed_env["RUSTUP_HOME"] = os.environ["RUSTUP_HOME"]
    closed_env.update(env)

    ids = action_user() if sandbox == "local" else None  # a container runs as itself
    if sandbox in ("docker", "container"):
        # docker = OrbStack/Docker Desktop daemon; container = Apple's container CLI (one VM per
        # container). Same flag shape: no network, the slot mounted at /work, a closed env.
        img = image or DEFAULT_IMAGE
        workdir_in = ("/work/" + cwd).rstrip("/.") or "/work"
        cmd = [sandbox, "run", "--rm", "-v", f"{mount}:/work", "-w", workdir_in]
        if not network:
            cmd += ["--network", "none"]
        for name, where in caches.items():
            host = c.root / "caches" / name
            host.mkdir(parents=True, exist_ok=True)
            cmd += ["-v", f"{host}:{where if where.startswith('/') else '/work/' + where}"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [img, *argv]
        popen_cwd = str(mount)
        popen_env = {"PATH": DEFAULT_PATH, "HOME": os.environ.get("HOME", str(home))}
        popen_env.update(
            {k: v for k, v in os.environ.items() if k in ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")}
        )
    else:
        cmd = argv
        popen_cwd = str(workdir)
        popen_env = closed_env
        for name, where in caches.items():
            host = c.root / "caches" / name
            host.mkdir(parents=True, exist_ok=True)
            if ids and host.stat().st_uid != ids[0]:
                _chown(host, ids)  # a cache this action mints belongs to the action user
            link = mount / where
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                shutil.rmtree(link) if link.is_dir() and not link.is_symlink() else link.unlink()
            link.symlink_to(host)

    _chown(slot, ids)
    as_user: dict = {}
    if ids:
        as_user = {"user": ids[0], "group": ids[1]}
        if os.geteuid() == 0:
            as_user["extra_groups"] = []  # drop the server's supplementary groups too
    ended_by = "process"
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=popen_cwd,
            env=popen_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **as_user,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            code = -9
            ended_by = "timeout"
    except (FileNotFoundError, NotADirectoryError) as e:
        out, err, code, ended_by = b"", str(e).encode(), 127, "spawn_failed"  # the shell's "not found"
    except PermissionError as e:
        out, err, code, ended_by = b"", str(e).encode(), 126, "spawn_failed"  # the shell's "not executable"
    ms = int((time.time() - t0) * 1000)

    for where in caches.values():
        if not where.startswith("/"):
            mp = mount / where
            if mp.is_symlink():
                mp.unlink()
            elif mp.is_dir():
                shutil.rmtree(mp)  # the empty mount point a container leaves behind; the cache lives in caches/
    tree_out = c.trees.from_dir(mount, ignore, gitignore=gitignore)
    changed = c.trees.diff(tree, tree_out)
    rec = {
        "key": key,
        "tree_in": tree,
        "argv": json.dumps(argv),
        "cwd": cwd,
        "env": json.dumps(env, sort_keys=True),
        "sandbox": sandbox if not image else f"{sandbox}:{image}",
        "tree_out": tree_out,
        "exit_code": code,
        "stdout": c.blobs.put(out),
        "stderr": c.blobs.put(err),
        "ms": ms,
        "actor": actor,
        "at": time.time(),
    }
    if ended_by == "process":
        c.action_put(rec)  # only a result the process produced answers later submits
    shutil.rmtree(slot, ignore_errors=True)
    return _result(rec, cached=False, changed=changed, ended_by=ended_by)


def _result(rec: dict, cached: bool, changed: list[dict], ended_by: str = "process") -> dict:
    from .computer import Computer  # noqa: F401  (type only)

    return {
        "key": rec["key"],
        "cached": cached,
        "ended_by": ended_by,
        "exit_code": rec["exit_code"],
        "ms": rec["ms"],
        "tree_in": rec["tree_in"],
        "tree_out": rec["tree_out"],
        "changed_count": len(changed),
        "changed": changed[:200],
        "stdout_blob": rec["stdout"],
        "stderr_blob": rec["stderr"],
    }


def attach_tails(c: Computer, result: dict) -> dict:
    result["stdout_tail"] = _tail(c.blobs.get(result["stdout_blob"]))
    result["stderr_tail"] = _tail(c.blobs.get(result["stderr_blob"]))
    return result
