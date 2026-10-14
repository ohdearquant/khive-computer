"""kc: deployments in Docker (build, up, down, purge, prune, ps), seats, import, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import gitio, image, local
from .computer import Computer


def _root(args) -> Path:
    return Path(args.root or os.environ.get("KC_ROOT", "./computer")).expanduser().resolve()


def _read(path: str | None) -> str:
    return Path(path).read_text() if path else ""


def _mcp_config(url: str, token: str) -> dict:
    """An MCP server entry any client takes: HTTP at /mcp with the seat's bearer token."""
    return {"mcpServers": {"kc": {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kc", description="khive computer: workspace for AI agents, over MCP")
    ap.add_argument("--root", help="computer directory (default $KC_ROOT or ./computer)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the computer directory and database")

    inside = argparse.ArgumentParser(add_help=False)
    inside.add_argument("--in", dest="deployment", metavar="NAME", help="act on the computer of deployment NAME")
    s = sub.add_parser("seat", help="seats")
    ss = s.add_subparsers(dest="seat_cmd", required=True)
    sc = ss.add_parser("create", parents=[inside])
    sc.add_argument("name")
    sc.add_argument("--identity")
    sc.add_argument("--system-prompt")
    sc.add_argument("--loop-state")
    sr = ss.add_parser("rotate", parents=[inside])
    sr.add_argument("name")
    ss.add_parser("list", parents=[inside])

    im = sub.add_parser("import", help="clone a repo into the computer and bind a ref")
    im.add_argument("src")
    im.add_argument("name")
    im.add_argument("--ref", default="HEAD")

    sv = sub.add_parser("serve", help="serve MCP over HTTP")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8810)
    sv.add_argument("--path", default="/mcp")

    mc = sub.add_parser("mcp-config", help="print a Claude Code --mcp-config JSON for a token")
    mc.add_argument("--token", required=True)
    mc.add_argument("--url", default="http://127.0.0.1:8810/mcp")

    me = sub.add_parser("metrics")
    me.add_argument("--window", type=float, default=3600)

    bd = sub.add_parser("build", help="fix a deployment: its image (with your toolchains) and its first seat's token")
    bd.add_argument("-n", "--name", help="the deployment; without it only an image is built (-t)")
    bd.add_argument(
        "--rust", action="store_true", help="a stable Rust toolchain under /opt/cargo, owned by the action user"
    )
    bd.add_argument("--node", action="store_true", help="Node 22 and npm")
    bd.add_argument("--apt", action="append", default=[], metavar="PKG", help="a Debian package; repeat for more")
    bd.add_argument("--base", default=image.DEFAULT_BASE, help="the image to start from")
    bd.add_argument("--token", help="the first seat's token; default $KC_SEAT_TOKEN, else one is minted and printed")
    bd.add_argument("--seat", help="the first seat's name (default operator)")
    bd.add_argument("--tag", "-t", default="khive-computer:custom", help="without -n: the image to produce")
    bd.add_argument("--print", action="store_true", help="print the Dockerfile instead of building anything")

    up = sub.add_parser("up", help="start a deployment here and print its MCP client configuration")
    up.add_argument("name", nargs="?", default="kc", help="the deployment (default kc; kc build -n is implied)")
    up.add_argument("--port", type=int, help="host port for /mcp (default: on record, else 8810); fixed at creation")
    up.add_argument("--host", help="bind address (default: on record, else 127.0.0.1); 0.0.0.0 for other machines")
    dn = sub.add_parser("down", help="stop and remove the container; the volume stays")
    dn.add_argument("name", nargs="?", default="kc")
    pg = sub.add_parser("purge", help="down, then remove the volume, the image and the record: nothing is left")
    pg.add_argument("name")
    sub.add_parser("prune", help="remove stopped kc-* containers and kc-* images of deployments no longer on record")
    sub.add_parser("ps", help="deployments on record and their state")

    args = ap.parse_args(argv)
    # deployments talk to Docker, not to a computer directory
    if args.cmd == "build":
        if args.print:
            print(image.dockerfile(base=args.base, rust=args.rust, node=args.node, apt=args.apt), end="")
            return 0
        if not args.name:
            return image.build(args.tag, image.dockerfile(base=args.base, rust=args.rust, node=args.node, apt=args.apt))
        rec = local.build(
            args.name, base=args.base, rust=args.rust, node=args.node, apt=args.apt, token=args.token, seat=args.seat
        )
        print(rec["token"])  # the first seat's token, in the clear, for the client that will hold it
        return 0
    if args.cmd == "up":
        rec = local.up(args.name, port=args.port, host=args.host)
        print(json.dumps(_mcp_config(f"http://127.0.0.1:{rec['port']}/mcp", rec["token"]), indent=2))
        return 0
    if args.cmd == "down":
        local.down(args.name)
        return 0
    if args.cmd == "purge":
        local.purge(args.name)
        return 0
    if args.cmd == "prune":
        local.prune()
        return 0
    if args.cmd == "ps":
        for row in local.ps():
            print(f"{row['name']:<16} {row['state']:<8} {row['url'] or '-':<32} {row['seat']:<12} {row['image']}")
        return 0
    if args.cmd == "seat" and args.deployment:
        if args.seat_cmd == "create" and (args.identity or args.system_prompt or args.loop_state):
            ap.error(
                "--identity/--system-prompt/--loop-state are local files; with --in, set them through the seat tool"
            )
        print(local.exec_kc(args.deployment, "seat", args.seat_cmd, *([] if args.seat_cmd == "list" else [args.name])))
        return 0
    root = _root(args)

    if args.cmd == "init":
        Computer(root)
        print(f"computer at {root}")
        return 0

    c = Computer(root)
    if args.cmd == "seat":
        if args.seat_cmd == "create":
            token = c.seat_create(
                args.name,
                _read(args.identity),
                _read(args.system_prompt),
                _read(args.loop_state),
            )
            print(token)
        elif args.seat_cmd == "rotate":
            print(c.seat_rotate_token(args.name))
        else:
            for s in c.seat_list():
                print(s["name"])
        return 0
    if args.cmd == "import":
        print(
            json.dumps(
                gitio.import_repo(c, args.src, args.name, args.ref, actor="cli"),
                indent=2,
            )
        )
        return 0
    if args.cmd == "serve":
        from . import execer
        from .server import build_server

        # actions run as KC_ACTION_USER when it is set (the container image: server root, actions `kc`)
        print(json.dumps({"serve": execer.prepare_action_user(c)}), file=sys.stderr)
        # the host hands the first seat's token through the environment: the seat exists with exactly
        # that token when the listener opens, created or re-keyed (ADR-0008 C4)
        if os.environ.get("KC_SEAT_TOKEN"):
            c.seat_ensure(os.environ.get("KC_SEAT_NAME", "operator"), os.environ["KC_SEAT_TOKEN"])
        # a deploy sends SIGTERM; calls in flight get KC_DRAIN_SECONDS to finish before the listener
        # closes. The host's stop grace period has to be longer than this.
        drain = int(os.environ.get("KC_DRAIN_SECONDS", "300"))
        build_server(c).run(
            transport="http",
            host=args.host,
            port=args.port,
            path=args.path,
            show_banner=False,
            uvicorn_config={"timeout_graceful_shutdown": drain},
        )
        return 0
    if args.cmd == "mcp-config":
        print(json.dumps(_mcp_config(args.url, args.token), indent=2))
        return 0
    if args.cmd == "metrics":
        print(json.dumps(c.metrics(args.window), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
