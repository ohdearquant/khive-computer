---
adr: ADR-0008
status: proposed
liveness: operating (the anonymous `/healthz` answer and the token still required on `/mcp` are pinned by `test_healthz_is_anonymous_and_says_nothing`; the first seat created or re-keyed from `KC_SEAT_TOKEN` is pinned by `test_seat_ensure_bootstraps_and_rekeys` and, on a real container, by `test_kc_build_up_down_purge_without_the_docker_cli`; readiness read from `/healthz` and the same computer returning on the same volume under a new container are pinned by `test_docker_round_trip`; the drain is the server setting of ADR-0006; the host's stop grace is a convention)
date: "2026-10-14"
area: host
kind: new
depends_on:
  - "[[ADR-0003-the-seat|ADR-0003]]"
  - "[[ADR-0006-the-computer|ADR-0006]]"
tags:
  - adr
  - computer
---

# ADR-0008: What a host gets from the computer; deployment stays outside

## Context

A computer runs under something that starts and stops its process: Docker on a laptop, a machine at
a cloud provider, a sandbox service that pauses what is idle. Those hosts differ in what persists, how
long a process lives, how a port is reached and what a stop means. A stopped computer cannot receive
its own wake request, so the code that deploys, wakes and sleeps a computer cannot live inside it.

This record fixes the boundary. The computer ships no provider code: no deployment registry, no
driver, no sleep policy. It ships four things a host reads and relies on, so that any host can run it
the same way: an anonymous readiness route, a drain the host's stop grace has to clear, a first seat
whose token the host hands in through the environment, and the data directory as the deployment's
identity. The computer itself is [[ADR-0006-the-computer|ADR-0006]]; the seat whose token a client
holds is [[ADR-0003-the-seat|ADR-0003]]. Source: `healthz` in `build_server`
(`src/khive_computer/server.py`), the `KC_SEAT_TOKEN` read in `kc serve` and `seat_ensure` in
`computer.py`, `kc build -n` and `kc up` in `local.py`, `Dockerfile` and `compose.yaml`.

## Definitions

- **host**: whatever starts and stops the server process and keeps its data directory: a container
  runtime, a cloud machine, a sandbox provider, a person with a shell.
- **running**: the host's word: the process or container exists and has been started.
- **ready**: the computer's word: `GET /healthz` answers `{"ok": true}`.
- **connected**: a client's word: an MCP initialize and `seat.whoami` succeeded with a seat's token.
- **drain**: `KC_DRAIN_SECONDS` (default 300): how long calls in flight may finish after SIGTERM before
  the listener closes ([[ADR-0006-the-computer#^c5|ADR-0006/C5]]).
- **stop grace**: how long the host waits after SIGTERM before it kills the process.
- **first seat**: the seat named `KC_SEAT_NAME` (default `operator`) whose token is `KC_SEAT_TOKEN`.
  `kc serve` creates it, or re-keys it, before the listener opens.
- **deployment identity**: the data directory. The same directory under new compute is the same
  computer.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | A host can poll an HTTP route on the computer's port before it hands a client the URL. | every container runtime and cloud provider exposes a port and a way to probe it | the host waits a fixed time and hands out URLs that may not answer yet |
| A2 | A host sends SIGTERM and waits a configurable grace before killing. | Docker `stop -t`, Compose `stop_grace_period`, cloud machine kill timeouts | calls in flight die with the process whatever the drain says |
| A3 | A host can set an environment variable on the process it starts, and keep it secret. | `docker run -e`, Compose `.env`, every provider's secrets | the first seat is minted with `kc seat create` on the data directory instead (one SQLite row; the command exists for every seat after the first) |
| A4 | A host keeps the data directory across restarts, image updates and moves. | a named volume, a persistent disk | every restart is a new, empty computer; nothing here can tell |

## Claims

### C1: `GET /healthz` is the one anonymous route and says nothing about the computer _(enforced: mechanical)_ ^c1

- **Subject**: every request without a seat's token.
- **Violated when**: a tool answers without a token, or `/healthz` returns a name, a count, a schema
  or any content of the computer.

`healthz` is registered as a custom route beside the MCP path and returns the constant `{"ok": true}`.
The MCP path keeps its token verifier. The test reads `/healthz` without a token and gets exactly that
body, then posts to `/mcp` without a token and gets 401.

### C2: Ready is the computer's answer, not the host's state _(enforced: mechanical)_ ^c2

- **Subject**: a host deciding when to hand a client the URL.
- **Violated when**: a host reports a container's `running` as the server being up, or a client is
  handed a URL before `/healthz` has answered.

The three words in Definitions are three facts with three sources. A host that conflates the first
two hands out URLs that refuse connections for the seconds between process start and listener open;
one that conflates the last two reports a healthy server to a client whose token was rotated.
`test_docker_round_trip` polls `/healthz` after `docker run` returns and connects only once it answers.

### C3: The host's stop grace exceeds the drain _(enforced: by convention)_ ^c3

- **Subject**: every host configuration that stops a serving computer.
- **Violated when**: the host kills the process before `KC_DRAIN_SECONDS` have passed since SIGTERM.

The image sets the drain to 300 and `compose.yaml` sets `stop_grace_period: 330s`. The drain is the
computer's promise to its callers ([[ADR-0006-the-computer#^c5|ADR-0006/C5]]); the grace is the
host's part of keeping it.

### C4: The first seat's token comes in through the environment; the computer stores its hash _(enforced: mechanical)_ ^c4

- **Subject**: the first seat of a deployment, and how any seat's token reaches the computer.
- **Violated when**: a token is baked into an image, written into a template, read from anywhere but
  the environment or a `kc seat create` output, or stored by the computer in the clear.

`kc serve` reads `KC_SEAT_TOKEN` before the listener opens and calls `seat_ensure`: the seat named
`KC_SEAT_NAME` is created with that token's hash, or re-keyed to it when it exists, so changing the
variable and restarting is a rotation. The token itself is chosen outside the computer: by the host's
secret store, by `kc build -n`, which mints one when none is given and keeps it in the deployment
record on the operator's own machine, or by a person. Every seat after the first is `kc seat create`
on the data directory, printed once ([[ADR-0003-the-seat#^c2|ADR-0003/C2]]). The unit test boots a
seat from a token, re-keys it and sees the old token stop matching; the container test runs `kc build
-n`, `kc up`, and connects with the printed token.

### C5: The data directory is the deployment's identity _(enforced: mechanical)_ ^c5

- **Subject**: every restart, image update, machine move and recreation of the compute.
- **Violated when**: a host attaches new, empty storage to a name that had a computer, or deletes
  storage as part of stopping.

[[ADR-0006-the-computer#^d1|ADR-0006/D1]] makes the directory the computer. A host that keeps the
directory keeps the seats, refs, trees and history; one that loses it has made a new computer, and
nothing in the computer can say so. `test_docker_round_trip` stops a container, removes it, starts a
new one on the same volume and finds the seat, the ref and its history where they were.

## Decisions

### D1: One anonymous health route ^d1

Serves C1, C2. A constant answer on a fixed path, outside the token verifier, so a host that holds no
seat can tell whether the server is up, and nobody learns anything else from asking.

- **Landing evidence**: `test_healthz_is_anonymous_and_says_nothing`.

### D2: Deployment, wake and sleep live outside this repository ^d2

Serves C2, C3, C5. The computer ships the image, the drain and the four facts above; a host built on
them (a Compose file, a cloud deployment, a provider driver) is a separate thing with its own release
cycle, and the computer does not depend on which one runs it. `kc up` is not a host: it is the Docker
on the operator's own machine driven from the `kc` command line, the same four facts read by a script
instead of a person.

- **Landing evidence**: no provider client in `pyproject.toml`; `compose.yaml` and `local.py` are the
  only host configurations in the tree and both read the four facts (the token variable, the volume,
  the port, the stop grace).

### D3: Bootstrap through the environment ^d3

Serves C4. Every host already has a way to hand a process a secret; none of them wants to run a
command on the storage before the process starts, or a shell inside the container after it. The host
sets `KC_SEAT_TOKEN`, the computer does the rest, and the person running it on a laptop never types
a docker command: `kc build -n NAME` fixes the deployment and prints the token, `kc up NAME` starts
it and prints the client configuration.

- **Landing evidence**: `test_seat_ensure_bootstraps_and_rekeys`;
  `test_kc_build_up_down_purge_without_the_docker_cli` runs the deployment verbs against a real
  container, connects with the token `kc build -n` printed, and finds the token in neither the
  container's argument list nor the command's own messages.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Provider drivers inside this repository | a stopped computer cannot run its own control plane, and each provider's lifecycle is a deployment product with its own release cycle, not part of the computer |
| Health behind the token | a host would need a seat's token to learn whether the server is up, and a rotated token would read as a dead server |
| Health that reports seats, refs or counts | a reachable port would leak the computer's shape to anyone who can connect |
| An idle timer inside the computer that exits the process | a process exit does not stop a billed machine, and a quiet port is not an idle computer: a ten-minute action makes no requests |
| A token baked into the image or a provider template | every deployment from that image would share a seat, and image layers are readable by anyone who can pull them |
| The first seat minted by a one-shot `kc seat create` on the volume before the server starts | it needs a shell on the host's storage: a docker command for a person, an exec for a cloud host; the environment is the channel every host has, and the token can be chosen by the host's own secret store |
| The server minting the first seat itself and printing the token in its log | a token in a log is a token in every log shipper; and a host that cannot read the log cannot hand the token to a client |

## Consequences

- **S1**: Any host that can run a container, keep a volume, set one environment variable, send SIGTERM
  with a grace and probe a port can run a computer; `docs/DEPLOY.md` says how in four bullets.
- **S2**: `/healthz` is reachable by anyone who can reach the port. It says one thing, so that is the
  whole exposure; the MCP path is unchanged.
- **S3**: A host that stops with a grace shorter than the drain breaks a promise the computer made;
  the computer cannot detect it.
- **S4**: Nothing in this repository knows where a computer is deployed or whether it is asleep. A
  client that wants a stable URL across sleeps needs a host that provides one.
- **S5**: The first seat's token exists in the clear wherever the host keeps its secrets (`.env`, a
  provider's secret store, `$KC_HOME` for `kc up`); the computer holds only its hash. Rotating it is
  changing the variable and restarting.
