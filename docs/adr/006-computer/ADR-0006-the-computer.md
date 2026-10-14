---
adr: ADR-0006
status: proposed
liveness: partial (the data directory's layout, blob verification on read, serving MCP over HTTP with a token per seat and the action user prepared at serve are pinned by `test_whoami_and_edit_exec_move`, `test_bad_token_is_refused` and `test_actions_run_as_the_configured_user`, or enforced in code; the drain is a server setting no test exercises; one owner per directory and the container image are conventions)
date: "2026-10-14"
area: computer
kind: new
depends_on:
  - "[[ADR-0001-trees-are-values|ADR-0001]]"
  - "[[ADR-0002-the-action|ADR-0002]]"
  - "[[ADR-0003-the-seat|ADR-0003]]"
  - "[[ADR-0005-the-sandbox|ADR-0005]]"
tags:
  - adr
  - computer
---

# ADR-0006: The computer: one data directory, served over MCP, drained on shutdown

## Context

The computer has to run on a laptop, inside a container, and on a cloud machine with a persistent
disk, with the same code and the same data layout, and a person should be able to stand one up
without reading the source.

This record fixes what a computer is on disk, how it is created and served, and how it stops. The
objects it holds are [[ADR-0001-trees-are-values|ADR-0001]] and
[[ADR-0002-the-action|ADR-0002]]; the seats that call it are [[ADR-0003-the-seat|ADR-0003]]; the
user its actions run as is [[ADR-0005-the-sandbox#^c6|ADR-0005/C6]]. Source: `Computer.__init__`
in `src/khive_computer/computer.py`, `connect` in `db/__init__.py`, `Blobs` in `blobs.py`,
`build_server` in `server.py`, and `cli.py`.

## Definitions

- **computer**: one data directory and the server that serves it.
- **data directory**: `KC_ROOT`, or `--root`, defaulting to `./computer`. It holds `computer.db`,
  `blobs/`, `repos/`, `slots/`, and `caches/` once a named cache is used.
- **`computer.db`**: the SQLite store in WAL mode holding seats, refs and their history, actions,
  snapshots, events and repositories; its tables are `src/khive_computer/db/schema.sql`, applied by
  `db.connect` every time the computer opens.
- **`kc`**: the command line: `init`, `seat create`, `seat rotate`, `seat list` (each with `--in` for a
  deployment), `import`, `serve` (creates the first seat from `KC_SEAT_TOKEN`), `mcp-config`, `metrics`,
  `build` (an image with toolchains on the published one; with `-n`, a deployment and its first seat's
  token), `up`, `down`, `purge`, `prune` and `ps` (deployments in this machine's Docker).
- **drain**: on shutdown, the time `KC_DRAIN_SECONDS` (default 300) that calls in flight are given
  to finish before the listener closes.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | One server process owns a data directory at a time. | the draft; the store's lock is in-process ([[ADR-0001-trees-are-values\|ADR-0001]] A4) | two servers on one directory can both win a compare-and-swap; a lock file or a database transaction is needed |
| A2 | SQLite in WAL mode and immutable blobs on one disk are durable enough for one owner. | the draft; SPEC §7 | backups need more than a copy of the directory |
| A3 | A platform stopping the computer waits longer than the drain before it kills it. | the draft | calls in flight die with the machine regardless of the drain |
| A4 | Toolchains belong in images, not in run-time installs. | the draft: a machine that stops when idle loses its root filesystem | every cold start reinstalls them |

## Claims

### C1: Everything a computer knows is under its data directory _(enforced: mechanical)_ ^c1

- **Subject**: every write the computer makes.
- **Violated when**: the computer's state lives outside the data directory, or a copy of the
  directory does not reproduce the computer.

`Computer.__init__` creates the directory, `blobs/`, `slots/` and `repos/`, opens `computer.db` in
WAL mode with normal synchronization, and applies the schema. Named caches live under `caches/` in
the same directory ([[ADR-0002-the-action#^c8|ADR-0002/C8]]). The credential and the configuration
are environment variables, not state.

### C2: A blob that no longer matches its hash is refused on read _(enforced: mechanical)_ ^c2

- **Subject**: every blob read.
- **Violated when**: a blob whose bytes have changed on disk is returned.

`Blobs.get` recomputes the BLAKE3 hash of the bytes it reads and raises "blob corrupt on disk" on a
mismatch. A blob is written to a temporary file and renamed into place, so a reader never sees a
partial blob.

### C3: The computer is served as MCP over HTTP, a token per seat _(enforced: mechanical)_ ^c3

- **Subject**: `kc serve`.
- **Violated when**: a tool is reachable without a seat's token, or the server listens on another
  path than the one configured.

`kc serve` serves `build_server` over HTTP at `/mcp`, on `127.0.0.1:8810` unless `--host`, `--port`
or `--path` say otherwise. Eight tools, one per family ([[ADR-0007-one-tool-per-family|ADR-0007]]);
every call is an event
([[ADR-0003-the-seat#^c4|ADR-0003/C4]]). `kc mcp-config --token` prints a client configuration for
a seat.

### C4: Serving prepares the action user first _(enforced: mechanical)_ ^c4

- **Subject**: every `kc serve` with `KC_ACTION_USER` set.
- **Violated when**: the server accepts calls before the cache root belongs to the action user.

Before it serves, `kc serve` calls `prepare_action_user`, which hands the cache root to the action
user and writes a marker so a restart does not repeat the walk, and prints what it did on stderr.

### C5: Shutdown drains calls in flight _(enforced: mechanical)_ ^c5

- **Subject**: every stop of a serving computer.
- **Violated when**: on a termination signal the listener closes while a call shorter than the
  drain is still running.

`kc serve` passes `KC_DRAIN_SECONDS` as the HTTP server's graceful shutdown timeout. On a
termination signal the server stops accepting connections and waits up to that long for calls in
flight. A call longer than the drain dies with the process.

### C6: One owner per data directory _(enforced: by convention)_ ^c6

- **Subject**: every data directory.
- **Violated when**: two servers, or a server and a `kc` command that writes, act on one directory
  at the same time.

Nothing in the code detects a second process on the same directory (A1). The command line's own
writes (`seat create`, `seat rotate`, `import`) are expected while the server is stopped, or are
accepted as racing it.

### C7: The computer ships as a container image _(enforced: by convention)_ ^c7

- **Subject**: the published image.
- **Violated when**: the image lacks the server, git or a Python runtime, or its actions run as the
  server's user.

The image carries the server, git and a Python runtime; `KC_ROOT` points at a mounted volume
(`/data/computer`); the server listens on 8810; actions run in the `local` sandbox as an unprivileged
`KC_ACTION_USER`. Toolchains a project needs are layered on top as further images.

## Decisions

### D1: The data directory is the computer ^d1

Serves C1, C2. Moving or backing up the directory moves or backs up the computer. The computer makes
no durability promise beyond the disk it is on.

- **Landing evidence**: every test builds a `Computer` on a fresh temporary directory and needs
  nothing else.

### D2: MCP over HTTP as the only face ^d2

Serves C3, C4. Any MCP client drives the computer: a coding assistant with its native tools off, a
local model, a script. Nothing on the client side needs to persist.

- **Landing evidence**: `test_whoami_and_edit_exec_move` and `test_bad_token_is_refused` run the
  server over real loopback HTTP; `test_actions_run_as_the_configured_user` covers
  `prepare_action_user` and its marker.

### D3: A graceful drain on shutdown ^d3

Serves C5. A redeploy is a restart of the computer: calls shorter than the drain finish across it.

- **Landing evidence**: the `timeout_graceful_shutdown` setting in `cli.main`; no test sends a
  termination signal.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| A database server (Postgres) | a second process to run and back up for one owner; SPEC §7 |
| Several owners per computer | no authority model yet ([[ADR-0003-the-seat\|ADR-0003]]) |
| An agent loop inside the computer | the client owns the loop; the computer serves tools |
| Toolchains installed at run time | lost when an idle machine stops (A4) |
| Immediate shutdown | every redeploy would kill calls a short wait would finish |
| A web interface | the model is the interface; the surface is the tools |

## Consequences

- **S1**: `docker run -v kc_data:/data -p 8810:8810 <image>` is a working computer, and
  `docker exec <container> kc --root /data/computer seat create <name>` mints the first seat.
- **S2**: The data directory is the backup unit.
- **S3**: A redeploy is a restart: long calls are sequenced around it, or moved to per-action
  machines later. A platform's kill timeout has to be longer than the drain.
- **S4**: The free-space floor ([[ADR-0005-the-sandbox#^c7|ADR-0005/C7]]) reads the data
  directory's volume, because build caches grow until they fill it.
- **S5**: Memory, messaging and task stores are not part of the computer;
  a client holds them beside it.
