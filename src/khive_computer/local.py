"""Deployments on this machine's Docker: `kc build -n`, `kc up`, `kc down`, `kc purge`, `kc prune`, `kc ps`.

A deployment is a name. `kc build -n NAME` fixes what it runs (the published image, or one with the
toolchains asked for layered on it) and the token of its first seat: the one given with `--token` or
`KC_SEAT_TOKEN`, else one minted here and printed. The record lives in `$KC_HOME` (default `~/.kc`),
mode 0600; it is the one place the token exists in the clear, the computer stores a hash. `kc up NAME`
starts container `kc-NAME` on volume `kc-NAME-data`, hands the token to the server through its
environment (the server creates or re-keys the seat on start; see `kc serve`), waits until the computer
answers and prints the MCP client configuration. `down` stops with a grace longer than the drain and
removes the container; the volume, which is the computer (ADR-0008 C5), stays. `purge` removes the
volume, the image and the record too. Nobody types a docker command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from . import image
from .db.seats import mint

DOCKER = "docker"
DRAIN_SECONDS = 300  # the image's KC_DRAIN_SECONDS; `down` waits longer than this
PASS_THROUGH = ("GITHUB_TOKEN",)  # environment the container also gets when this process has it


# ------------------------------------------------------------------ records
def home() -> Path:
    return Path(os.environ.get("KC_HOME") or "~/.kc").expanduser()


def record_path(name: str) -> Path:
    return home() / f"{name}.json"


def load(name: str) -> dict | None:
    p = record_path(name)
    return json.loads(p.read_text()) if p.is_file() else None


def save(rec: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    home().chmod(0o700)
    p = record_path(rec["name"])
    p.write_text(json.dumps(rec, indent=2) + "\n")
    p.chmod(0o600)


def container(name: str) -> str:
    return f"kc-{name}"


def volume(name: str) -> str:
    return f"kc-{name}-data"


# ------------------------------------------------------------------- docker
def _docker(*argv: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    try:
        p = subprocess.run([DOCKER, *argv], capture_output=True, text=True, check=False, env=env)
    except FileNotFoundError:
        raise SystemExit("docker is not on PATH; kc up/down/purge/prune/ps need Docker") from None
    if check and p.returncode != 0:
        raise SystemExit(f"docker {' '.join(argv)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip()


def state(name: str) -> str | None:
    """The container's status (`running`, `exited`, ...) or None when no container has this name."""
    p = subprocess.run(
        [DOCKER, "inspect", "-f", "{{.State.Status}}", name], capture_output=True, text=True, check=False
    )
    return p.stdout.strip() if p.returncode == 0 else None


def run_argv(name: str, image_tag: str, port: int, host: str, env: Sequence[str]) -> list[str]:
    """The `docker run` for a deployment. Environment goes by NAME (`-e KEY`): docker copies each value
    from this process's environment, so the token is never in the argument list or in `ps`."""
    argv = [DOCKER, "run", "-d", "--name", container(name), "-p", f"{host}:{port}:8810"]
    argv += ["-v", f"{volume(name)}:/data", "--restart", "unless-stopped"]
    for key in env:
        argv += ["-e", key]
    return [*argv, image_tag]


def ready(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
            return r.status == 200 and json.loads(r.read()) == {"ok": True}
    except (urllib.error.URLError, OSError, ValueError):
        return False


def exec_kc(name: str, *argv: str) -> str:
    """Run `kc ...` inside the deployment's container, on its data directory."""
    return _docker("exec", container(name), "kc", "--root", "/data/computer", *argv)


# ------------------------------------------------------------------- verbs
def build(
    name: str,
    *,
    base: str = image.DEFAULT_BASE,
    rust: bool = False,
    node: bool = False,
    apt: Sequence[str] = (),
    token: str | None = None,
    seat: str | None = None,
    log=None,
) -> dict:
    """Fix the deployment: its image (built here when toolchains are asked for) and its first seat's token.
    Token precedence: `token`, then `KC_SEAT_TOKEN`, then the token already on record, then a new one."""
    log = log or sys.stderr
    rec = load(name) or {"name": name}
    if rust or node or apt:
        tag = f"kc-{name}:latest"
        print(f"building {tag} on {base}", file=log)
        rc = image.build(tag, image.dockerfile(base=base, rust=rust, node=node, apt=apt))
        if rc:
            raise SystemExit(rc)
        rec["image"] = tag
    else:
        rec["image"] = base
    rec["seat"] = seat or rec.get("seat") or "operator"
    rec["token"] = token or os.environ.get("KC_SEAT_TOKEN") or rec.get("token") or mint()
    rec["toolchains"] = {"rust": rust, "node": node, "apt": sorted(set(apt))}
    save(rec)
    return rec


def up(name: str, *, port: int | None = None, host: str | None = None, log=None) -> dict:
    """Start the deployment (recording it first when `kc build -n` was skipped) and wait until it answers.
    Port and bind address default to the ones on record, then 8810 on 127.0.0.1; they are fixed when the
    container is created, so after `down` an `up` with new ones applies them."""
    log = log or sys.stderr
    rec = load(name) or build(name, log=log)
    port = port or rec.get("port") or 8810
    host = host or rec.get("host") or "127.0.0.1"
    st = state(container(name))
    if st is None:
        _docker("volume", "create", volume(name))
        env = {"KC_SEAT_TOKEN": rec["token"], "KC_SEAT_NAME": rec["seat"]}
        env.update({k: os.environ[k] for k in PASS_THROUGH if os.environ.get(k)})
        _docker(*run_argv(name, rec["image"], port, host, list(env))[1:], env={**os.environ, **env})
        rec["port"], rec["host"] = port, host
        save(rec)
        print(f"started {container(name)} from {rec['image']}; the computer lives in volume {volume(name)}", file=log)
    elif st != "running":
        _docker("start", container(name))
        print(f"started {container(name)} (was {st})", file=log)
    else:
        print(f"{container(name)} is running", file=log)
    if (port, host) != (rec["port"], rec["host"]):
        print(
            f"note: {container(name)} keeps {rec['host']}:{rec['port']}; `kc down {name}` then `kc up` applies new ones",
            file=log,
        )
    base = f"http://127.0.0.1:{rec['port']}"
    t0 = time.time()
    while not ready(base):
        if time.time() - t0 > 60:
            raise SystemExit(f"{base}/healthz did not answer within 60s; `docker logs {container(name)}` says why")
        time.sleep(0.25)
    return rec


def down(name: str, log=None) -> None:
    """Stop with a grace longer than the drain and remove the container; the volume stays."""
    log = log or sys.stderr
    if state(container(name)) is None:
        print(f"no container {container(name)}", file=log)
        return
    _docker("stop", "-t", str(DRAIN_SECONDS + 30), container(name))
    _docker("rm", container(name))
    print(f"{container(name)} stopped and removed; the computer stays in volume {volume(name)}", file=log)


def purge(name: str, log=None) -> None:
    """`down`, then remove the volume (the computer, with every tree and ref in it), the deployment's own
    image and the record. Nothing of the deployment is left."""
    log = log or sys.stderr
    down(name, log=log)
    if _docker("volume", "ls", "-q", "--filter", f"name=^{volume(name)}$"):
        _docker("volume", "rm", volume(name))
        print(f"removed volume {volume(name)}", file=log)
    rec = load(name)
    if rec and rec.get("image", "").startswith(f"kc-{name}:"):
        _docker("rmi", rec["image"], check=False)
    if record_path(name).is_file():
        record_path(name).unlink()
    print(f"purged {name}", file=log)


def ps() -> list[dict]:
    """Every deployment on record with its container state."""
    rows = []
    for p in sorted(home().glob("*.json")) if home().is_dir() else []:
        rec = json.loads(p.read_text())
        rows.append(
            {
                "name": rec["name"],
                "state": state(container(rec["name"])) or "down",
                "url": f"http://127.0.0.1:{rec['port']}/mcp" if rec.get("port") else None,
                "image": rec["image"],
                "seat": rec["seat"],
            }
        )
    return rows


def prune(log=None) -> None:
    """Remove stopped `kc-*` containers and `kc-*` images that belong to no deployment on record. Volumes
    are never pruned: a volume is a computer, `kc purge NAME` is the only thing that removes one."""
    log = log or sys.stderr
    known = {container(r["name"]) for r in ps()} | {r["image"] for r in ps()}
    for line in _docker("ps", "-a", "--filter", "name=^kc-", "--format", "{{.Names}} {{.State}}").splitlines():
        cname, st = line.split()
        if cname not in known and st != "running":
            _docker("rm", cname)
            print(f"removed container {cname}", file=log)
    for tag in _docker("images", "kc-*", "--format", "{{.Repository}}:{{.Tag}}").splitlines():
        if tag not in known:
            _docker("rmi", tag, check=False)
            print(f"removed image {tag}", file=log)
    if shutil.which(DOCKER) is None:  # unreachable after the calls above; keeps the import honest
        raise SystemExit("docker is not on PATH")
