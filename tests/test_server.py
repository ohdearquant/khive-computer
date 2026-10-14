"""End to end over HTTP: a seat token in, tools out, every call an event."""

import asyncio
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
import uvicorn
from fastmcp import Client
from mcp.shared.exceptions import MCPError

from khive_computer.computer import Computer
from khive_computer.server import FAMILIES, build_server


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def served(tmp_path):
    c = Computer(tmp_path / "computer")
    token = c.seat_create(
        "tester",
        identity="I am the tester.",
        system_prompt="sp",
        loop_state="## LOG none",
    )
    port = _free_port()
    app = build_server(c).http_app(path="/mcp")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    import time

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield c, token, f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    th.join(timeout=5)


def test_whoami_and_edit_exec_move(served):
    c, token, url = served
    t0 = c.trees.write(c.trees.empty(), "hello.txt", b"hi\n")
    c.ref_create("main", t0, "setup")

    async def go():
        async with Client(url, auth=token) as cl:
            w = (await cl.call_tool("seat", {"action": "whoami"})).data
            assert w["seat"] == "tester" and w["identity"] == "I am the tester."
            ed = (
                await cl.call_tool(
                    "fs",
                    {"action": "edit", "tree": "main", "path": "hello.txt", "old": "hi", "new": "hello"},
                )
            ).data
            ex = (await cl.call_tool("exec", {"action": "run", "tree": ed["tree"], "argv": ["cat", "hello.txt"]})).data
            assert ex["exit_code"] == 0 and ex["stdout_tail"] == "hello\n"
            mv = (
                await cl.call_tool(
                    "ref",
                    {"action": "move", "name": "main", "expected_tree": t0, "new_tree": ed["tree"]},
                )
            ).data
            assert mv["rev"] == 1
            r = await cl.call_tool(
                "ref",
                {"action": "move", "name": "main", "expected_tree": t0, "new_tree": ed["tree"]},
                raise_on_error=False,
            )
            assert r.is_error
            m = (await cl.call_tool("computer", {"action": "metrics", "window_s": 60})).data
            assert m["calls"] >= 5 and m["errors"] == 1
            ev = (await cl.call_tool("computer", {"action": "events", "n": 3})).data
            assert all(e["actor"] == "tester" for e in ev)
            assert ev[0]["verb"] == "computer.metrics" and "fs.edit" in [e["verb"] for e in c.events_recent(10)]

    asyncio.run(go())


def test_bad_token_is_refused(served):
    _c, _token, url = served

    async def go():
        with pytest.raises(MCPError):
            async with Client(url, auth="kc_not_a_token") as cl:
                await cl.call_tool("seat", {"action": "whoami"})

    asyncio.run(go())


def test_eight_families_dispatch_by_action(served):
    c, token, url = served

    async def go():
        async with Client(url, auth=token) as cl:
            tools = {t.name: t for t in await cl.list_tools()}
            assert sorted(tools) == sorted(FAMILIES)
            props = tools["fs"].input_schema["properties"]
            assert set(props) >= {"action", "tree", "path", "old", "new", "pattern", "a", "b"}
            assert sorted(props["action"]["enum"]) == sorted(
                ["read", "list", "glob", "grep", "write", "edit", "remove", "diff", "empty"]
            )
            assert "- merge(into: str, from_ref: str" in tools["branch"].description
            empty = (await cl.call_tool("fs", {"action": "empty"})).data
            assert empty["tree"] == c.trees.empty()
            r = await cl.call_tool("branch", {"action": "fly"}, raise_on_error=False)
            assert r.is_error and "Input should be 'create', 'merge' or 'discard'" in r.content[0].text
            r = await cl.call_tool("branch", {"action": "merge", "into": "main"}, raise_on_error=False)
            assert r.is_error and "branch.merge" in r.content[0].text and "from_ref" in r.content[0].text
            ev = c.events_recent(3)  # a call refused at binding never ran, so it is not an event
            assert ev[0]["verb"] == "fs.empty" and ev[0]["ok"] == 1

    asyncio.run(go())


def test_healthz_is_anonymous_and_says_nothing(served):
    _c, _token, url = served
    base = url.removesuffix("/mcp")
    with urllib.request.urlopen(base + "/healthz", timeout=5) as r:  # no token
        assert r.status == 200 and json.loads(r.read()) == {"ok": True}
    # the MCP path itself still takes a token
    req = urllib.request.Request(
        url,
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
