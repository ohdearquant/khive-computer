"""The image, end to end, against a real container. Skipped unless `E2E_IMAGE` names a built image and
`docker` answers; CI builds the image and runs this against it. Everything the record set promises a
host (ADR-0008) and a seat (ADR-0003) is exercised once, in order: the first seat minted on the volume
before any server runs, the server ready when `/healthz` answers, the eight tools over MCP from a
client that holds only the token, a rotated token, a stop that keeps the data, and the same computer
coming back on the same volume under a new container."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from khive_computer import cli

IMAGE = os.environ.get("E2E_IMAGE")
DOCKER = shutil.which("docker")

pytestmark = pytest.mark.skipif(not IMAGE or not DOCKER, reason="set E2E_IMAGE to a built image; needs docker")


def sh(*argv: str, check: bool = True) -> str:
    p = subprocess.run(argv, capture_output=True, timeout=120, check=False)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} -> {p.returncode}\n{p.stderr.decode(errors='replace')[-800:]}")
    return p.stdout.decode().strip()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def healthz(base: str) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError):
        return None, ""


def wait_ready(base: str, limit_s: float = 60) -> float:
    t = time.time()
    while healthz(base) != (200, '{"ok":true}'):
        assert time.time() - t < limit_s, f"{base}/healthz did not answer within {limit_s}s"
        time.sleep(0.25)
    return time.time() - t


async def call(url: str, token: str, calls: list[tuple[str, dict]]) -> list:
    out = []
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport, timeout=60) as cl:
        for tool, args in calls:
            out.append((await cl.call_tool(tool, args)).data)
    return out


async def refused(url: str, token: str) -> bool:
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    try:
        async with Client(transport, timeout=20) as cl:
            await cl.call_tool("seat", {"action": "whoami"})
    except Exception:  # noqa: BLE001 - any refusal counts; admission is the defect
        return True
    return False


@pytest.fixture
def deployment():
    name = f"kc-test-{uuid.uuid4().hex[:8]}"
    vol = f"{name}-data"
    sh(DOCKER, "volume", "create", vol)
    started: list[str] = []

    def start(port: int) -> str:
        cid = sh(
            DOCKER,
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:8810",
            "-v",
            f"{vol}:/data",
            "-e",
            "KC_DRAIN_SECONDS=5",
            IMAGE,
        )
        started.append(cid)
        return cid

    yield name, vol, start
    sh(DOCKER, "rm", "-f", name, check=False)
    sh(DOCKER, "volume", "rm", vol, check=False)


def test_docker_round_trip(deployment):
    name, vol, start = deployment

    # the first seat, minted on the volume before any server runs (ADR-0008 C4/D3)
    token = sh(
        DOCKER,
        "run",
        "--rm",
        "-v",
        f"{vol}:/data",
        IMAGE,
        "kc",
        "--root",
        "/data/computer",
        "seat",
        "create",
        "operator",
    )
    assert token.startswith("kc_") and len(token) == 51

    port = free_port()
    start(port)
    base = f"http://127.0.0.1:{port}"
    url = base + "/mcp"
    assert wait_ready(base) < 60  # running is the host's word; this is the computer's (ADR-0008 C2)

    who, empty = asyncio.run(call(url, token, [("seat", {"action": "whoami"}), ("fs", {"action": "empty"})]))
    assert who["seat"] == "operator"
    (w,) = asyncio.run(
        call(
            url,
            token,
            [
                (
                    "fs",
                    {
                        "action": "write",
                        "tree": empty["tree"],
                        "path": "hello.py",
                        "content": "print('from a khive computer')\n",
                    },
                ),
            ],
        )
    )
    _ref, ex = asyncio.run(
        call(
            url,
            token,
            [
                ("ref", {"action": "create", "name": "main", "tree": w["tree"]}),
                ("exec", {"action": "run", "tree": "main", "argv": ["sh", "-c", "id -un; python3 hello.py > out.txt"]}),
            ],
        )
    )
    assert ex["exit_code"] == 0 and ex["stdout_tail"].strip() == "kc"  # actions run as the action user (ADR-0005)
    (uvr,) = asyncio.run(
        call(
            url,
            token,
            [
                (
                    "exec",
                    {
                        "action": "run",
                        "tree": "main",
                        "argv": ["sh", "-c", "uv --version && uv init d -q && cd d && uv run python -c 'print(7)'"],
                        "network": True,
                        "timeout": 240,
                    },
                ),
            ],
        )
    )
    assert uvr["exit_code"] == 0 and uvr["stdout_tail"].strip().endswith("7")  # uv is in every image, usable by actions
    assert ex["changed"] == [{"path": "out.txt", "change": "added"}]
    mv, ev = asyncio.run(
        call(
            url,
            token,
            [
                ("ref", {"action": "move", "name": "main", "expected_tree": w["tree"], "new_tree": ex["tree_out"]}),
                ("computer", {"action": "events", "n": 4}),
            ],
        )
    )
    assert mv["rev"] == 1
    assert [(e["actor"], e["verb"]) for e in ev] == [
        ("operator", "ref.move"),
        ("operator", "exec.run"),
        ("operator", "exec.run"),
        ("operator", "ref.create"),
    ]

    # rotation: the old token stops matching at once, the seat is the same seat (ADR-0003)
    new_token = sh(DOCKER, "exec", name, "kc", "--root", "/data/computer", "seat", "rotate", "operator")
    assert new_token.startswith("kc_") and new_token != token
    assert asyncio.run(refused(url, token))
    (who2,) = asyncio.run(call(url, new_token, [("seat", {"action": "whoami"})]))
    assert who2["seat"] == "operator"

    # the anonymous surface is one route saying one thing (ADR-0008 C1)
    assert healthz(base) == (200, '{"ok":true}')
    req = urllib.request.Request(
        url,
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401

    # stop keeps the data; the same volume under a new container is the same computer (ADR-0008 C5)
    sh(DOCKER, "stop", "-t", "10", name)
    assert sh(DOCKER, "inspect", "-f", "{{.State.Status}}", name) == "exited"
    sh(DOCKER, "rm", name)
    port2 = free_port()
    start(port2)
    base2 = f"http://127.0.0.1:{port2}"
    wait_ready(base2)
    who3, main = asyncio.run(
        call(base2 + "/mcp", new_token, [("seat", {"action": "whoami"}), ("ref", {"action": "get", "name": "main"})])
    )
    assert who3["seat"] == "operator" and main["rev"] == 1 and main["tree"] == ex["tree_out"]


def test_kc_build_up_down_purge_without_the_docker_cli(capsys, monkeypatch, tmp_path):
    """The deployment verbs: `kc build -n` fixes the image and mints the first seat's token; `kc up` starts
    the container, the server creates the seat from its environment, the client config comes out; `kc up`
    again finds the container; `--in` reaches it; `kc down` keeps the volume; `kc purge` leaves nothing."""
    monkeypatch.setenv("KC_HOME", str(tmp_path / "kc"))
    monkeypatch.delenv("KC_SEAT_TOKEN", raising=False)
    name = f"test-{uuid.uuid4().hex[:8]}"
    port = free_port()
    try:
        assert cli.main(["build", "-n", name, "--base", IMAGE]) == 0
        token = capsys.readouterr().out.strip()
        assert token.startswith("kc_") and len(token) == 51

        assert cli.main(["up", name, "--port", str(port)]) == 0
        out = capsys.readouterr()
        srv = json.loads(out.out)["mcpServers"]["kc"]
        assert srv["url"] == f"http://127.0.0.1:{port}/mcp" and srv["headers"]["Authorization"] == f"Bearer {token}"
        assert "started kc-" in out.err and "kc_" not in out.err
        (who,) = asyncio.run(call(srv["url"], token, [("seat", {"action": "whoami"})]))
        assert who["seat"] == "operator"
        assert "kc_" not in sh(DOCKER, "inspect", "-f", "{{.Path}} {{.Args}}", f"kc-{name}")  # not in argv either

        cid = sh(DOCKER, "inspect", "-f", "{{.Id}}", f"kc-{name}")
        assert cli.main(["up", name]) == 0
        again = capsys.readouterr()
        assert json.loads(again.out)["mcpServers"]["kc"]["headers"]["Authorization"] == f"Bearer {token}"
        assert "is running" in again.err and sh(DOCKER, "inspect", "-f", "{{.Id}}", f"kc-{name}") == cid

        assert cli.main(["seat", "create", "alice", "--in", name]) == 0
        alice = capsys.readouterr().out.strip()
        (who2,) = asyncio.run(call(srv["url"], alice, [("seat", {"action": "whoami"})]))
        assert who2["seat"] == "alice"
        assert cli.main(["ps"]) == 0
        assert capsys.readouterr().out.split()[:2] == [name, "running"]

        assert cli.main(["down", name]) == 0
        assert "stays in volume" in capsys.readouterr().err
        assert subprocess.run([DOCKER, "inspect", f"kc-{name}"], capture_output=True, check=False).returncode != 0
        assert f"kc-{name}-data" in sh(DOCKER, "volume", "ls", "-q").split()
        assert cli.main(["up", name]) == 0  # the same computer comes back: both seats are still there
        capsys.readouterr()
        (who3,) = asyncio.run(call(srv["url"], alice, [("seat", {"action": "whoami"})]))
        assert who3["seat"] == "alice"

        assert cli.main(["purge", name]) == 0
        assert "purged" in capsys.readouterr().err
        assert f"kc-{name}-data" not in sh(DOCKER, "volume", "ls", "-q").split()
        assert cli.main(["ps"]) == 0 and capsys.readouterr().out == ""
    finally:
        sh(DOCKER, "rm", "-f", f"kc-{name}", check=False)
        sh(DOCKER, "volume", "rm", f"kc-{name}-data", check=False)


TOOLS_IMAGE = os.environ.get("E2E_TOOLS_IMAGE")


@pytest.mark.skipif(
    not TOOLS_IMAGE or not DOCKER, reason="set E2E_TOOLS_IMAGE to an image from `kc build --rust --node`"
)
def test_docker_toolchains_run_as_the_action_user(deployment):
    """The toolchains `kc build --rust --node` layers on the image: cargo from `$CARGO_HOME/bin` and npm,
    callable inside an action as `kc`."""
    name, vol, _start = deployment
    token = sh(
        DOCKER,
        "run",
        "--rm",
        "-v",
        f"{vol}:/data",
        TOOLS_IMAGE,
        "kc",
        "--root",
        "/data/computer",
        "seat",
        "create",
        "operator",
    )
    port = free_port()
    sh(DOCKER, "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8810", "-v", f"{vol}:/data", TOOLS_IMAGE)
    base = f"http://127.0.0.1:{port}"
    wait_ready(base)
    (empty,) = asyncio.run(call(base + "/mcp", token, [("fs", {"action": "empty"})]))
    runs = asyncio.run(
        call(
            base + "/mcp",
            token,
            [
                (
                    "exec",
                    {
                        "action": "run",
                        "tree": empty["tree"],
                        "argv": ["sh", "-c", "id -un; cargo --version; npm --version; node --version"],
                        "timeout": 120,
                    },
                ),
                (
                    "exec",
                    {
                        "action": "run",
                        "tree": empty["tree"],
                        "argv": ["sh", "-c", "cargo init --name hello -q && cargo build && ./target/debug/hello"],
                        "timeout": 600,
                        "caches": {"target": "target"},
                    },
                ),
                (
                    "exec",
                    {
                        "action": "run",
                        "tree": empty["tree"],
                        "argv": [
                            "sh",
                            "-c",
                            "npm init -y >/dev/null && node -e \"console.log(require('./package.json').name)\"",
                        ],
                        "timeout": 120,
                    },
                ),
            ],
        )
    )
    versions, build, npm = runs
    assert versions["exit_code"] == 0 and versions["stdout_tail"].startswith("kc\ncargo ")
    assert build["exit_code"] == 0, build["stderr_tail"]
    assert build["stdout_tail"].strip().endswith("Hello, world!")
    assert not any(c["path"].startswith("target/") for c in build["changed"])  # the named cache stays out of the tree
    assert npm["exit_code"] == 0 and npm["stdout_tail"].strip() == "m0"
