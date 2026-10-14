# Deploying a khive computer

A computer is one process, one data directory, one port. The image on GHCR runs it; a volume keeps
the data; a token per seat is the only credential a client holds.

## The short way: `kc build -n`, `kc up`

```sh
uv tool install khive-computer     # or `uv sync` in a clone and prefix with `uv run`
kc build -n box --rust --node      # the deployment: its image, its first seat's token (printed once here)
kc up box > mcp.json               # container, readiness, client configuration with the token inside
kc seat create alice --in box      # more seats, each printed once
kc down box                        # stop and remove the container; the volume stays
kc purge box                       # also the volume, the image and the record
kc ps                              # what is on record, and whether it runs
```

`kc build -n NAME` writes `$KC_HOME/NAME.json` (default `~/.kc`, mode 0600): the image (`kc-NAME:latest`
when toolchains are asked for, see Toolchains, else the published one or `--base`), the first seat's name
(`--seat`, default `operator`) and its token: `--token`, else `KC_SEAT_TOKEN`, else the one already on
record, else a fresh one. That file is the one place the token is in the clear; the computer stores a
hash. `kc up NAME` creates volume `kc-NAME-data`, runs container `kc-NAME` with the token in its
environment (passed by name, never on the command line), port `8810` on `127.0.0.1` (`--port`, `--host
0.0.0.0` to reach it from other machines, then put HTTPS in front; both fixed when the container is
created), waits for `/healthz` and prints the client configuration; run again it finds the container and
prints the same configuration. `GITHUB_TOKEN` is passed along when your shell has it. Everything below
is what these commands do, for hosts that are not your laptop.

## Run it

```sh
docker run -d --name kc -p 8810:8810 -v kc_data:/data \
  -e GITHUB_TOKEN=github_pat_... ghcr.io/ohdearquant/khive-computer:latest   # the token is optional
docker exec kc kc seat create demo > demo.token
docker exec kc kc mcp-config --token "$(cat demo.token)" --url http://127.0.0.1:8810/mcp
```

The first command starts the server; it creates the computer under `/data/computer` on first start.
The second mints a seat and prints its token once (the computer stores only a hash). The third prints
the MCP client configuration a model runtime takes: an HTTP server at `/mcp` with a bearer token.

With Compose, `docker compose up -d` does the same from `compose.yaml` in this repository and builds the
image locally when the registry copy is not wanted.

## What a host needs from the computer

Whatever starts and stops the process is the computer's host: Docker on your machine, a cloud machine,
a sandbox provider. The computer cannot wake itself once stopped, so deployment, wake and sleep belong
to the host, and this repository ships none of that. It ships what a host reads and relies on:

- **Readiness.** `GET /healthz` answers `{"ok": true}` without a token when the server is up, and says
  nothing else. A container in state `running` is not a server that answers; poll `/healthz` before
  handing a client the URL. Whether a seat gets in is `seat.whoami` with its token, a third fact.
- **Stop.** SIGTERM, then the drain: calls in flight get `KC_DRAIN_SECONDS` (default 300) to finish
  before the listener closes. The host's stop grace must be longer (`stop_grace_period: 330s` in
  `compose.yaml`); a shorter one kills the calls the drain would have finished.
- **The first seat.** The host sets `KC_SEAT_TOKEN` (any secret; `kc_` and 48 hex characters is the
  shape `kc` mints) and, if it wants another name than `operator`, `KC_SEAT_NAME`. `kc serve` creates
  the seat with that token's hash before the listener opens, or re-keys it when it exists, so changing
  the variable and restarting rotates it. The token never needs to leave the host's secret store for
  the computer's sake; the client that will hold it is the only other reader. Every seat after the
  first is `kc seat create <name>` on the data directory (`kc seat create <name> --in <deployment>`
  from the `kc` command line; `docker exec` into the container on any other host), printed once.
- **Identity.** The data directory is the computer. Recreate the container, update the image, move to
  another machine: as long as `/data` comes along, it is the same computer with the same seats, refs
  and trees. Deleting the volume is deleting the computer; nothing else is.

## What is where

| path or setting | meaning |
| --- | --- |
| `/data/computer` | the computer: `blobs/` (content), `computer.db` (seats, refs and their history, actions, snapshots, events, repositories), `slots/`, `caches/`, `repos/` |
| `KC_ROOT` | the computer directory; `/data/computer` in the image |
| `KC_SEAT_TOKEN` | the first seat's token; `kc serve` creates or re-keys the seat to it before listening. The computer stores a hash |
| `KC_SEAT_NAME` | the first seat's name (default `operator`) |
| `KC_HOME` | on the operator's machine: where `kc build -n` keeps deployment records (default `~/.kc`) |
| `KC_ACTION_USER` | the system user actions run as; `kc` in the image, the server itself runs as root so it can hand each slot over. Unset it to run actions as the server's user |
| `KC_DRAIN_SECONDS` | how long in-flight calls may finish after SIGTERM before the listener closes (default 300). The host's stop grace period must be longer |
| `KC_MIN_FREE_GB` | refuse to start an action when the volume holding `/data` has less than this free |
| `KC_GIT_IDENTITY` | the committer on every `git.export`, as `Name <email>`; default `khive computer <kc@localhost>`. Set it to an identity your forge knows (a bot account and its noreply address). The author is per export; unset, it is the seat's name at `localhost` |
| `GITHUB_TOKEN` | optional. With it set, `git.push` authenticates to GitHub as the computer through an in-process credential helper; the token never reaches a seat |
| `8810` | the HTTP port; MCP is served at `/mcp` |

## Sandboxes inside the image

Inside the image an action runs in the `local` sandbox: its own slot, a closed environment, a private
temporary directory, the configured user and a timeout. The `local` sandbox does not close the network;
if actions must not reach out, run the container with `--network none` and give the computer only what
it needs (a git remote through a proxy, for example). The `docker` and `container` sandboxes need a
container runtime the server can reach; mounting the host's Docker socket into the image gives it one,
and also gives anything that runs in the computer the same reach, so prefer a second machine or a
runtime with its own user namespace for that.

## Toolchains

Actions see what is on `PATH` in the image: Python 3.12, git, uv, pytest and ruff. The published image
carries nothing else. Anything your projects need is a layer on top, and `kc build` writes and builds it:

```sh
kc build --rust --node                          # Rust (rustup, stable) and Node 22 on the published image
kc build --apt build-essential --apt libssl-dev -t my-kc   # Debian packages, your own tag
kc build --rust --print                         # show the Dockerfile it would build, build nothing
```

The result is an ordinary image (`khive-computer:custom` unless `-t` says otherwise) that you run exactly
like the published one. `--rust` installs a minimal stable toolchain through rustup under `/opt/rustup`
and `/opt/cargo`, owned by the action user so cargo can write its registry; the server passes
`CARGO_HOME` and `RUSTUP_HOME` into every action and puts `$CARGO_HOME/bin` on every action's `PATH`.
`--node` installs Node 22 and npm from NodeSource. `--base` starts from another image (a pinned
version, or a build of this tree). Install toolchains at build time, not at first use: a container's
writable layer is gone when the container is recreated (an image update, a new machine); only `/data`
persists.

## HTTPS and reach

The server speaks plain HTTP. Put it behind the reverse proxy you already run (Caddy, nginx, a cloud
load balancer) for TLS, and keep the port closed to anything but that proxy. A token is a bearer
credential: whoever holds it holds the seat.

## Backups

Stop the container or accept a point-in-time copy, then copy `/data/computer`. Blobs are immutable and
named by hash, so a copy of `blobs/` is always consistent; `computer.db` is SQLite in WAL mode and
should be copied with its `-wal` file or after a checkpoint.

## Updating

Pull the new image and restart. The schema is created with `CREATE TABLE IF NOT EXISTS`; a version
that changes a table says so in its release notes and in `docs/adr/`.
