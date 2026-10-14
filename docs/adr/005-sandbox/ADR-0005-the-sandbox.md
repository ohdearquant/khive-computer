---
adr: ADR-0005
status: proposed
liveness: partial (the local sandbox's closed environment, its timeout, the configured action user and the free-space floor are pinned by `test_exec_runs_in_a_slot_and_captures_the_tree`, `test_actions_run_as_the_configured_user` and `test_actions_refuse_when_the_volume_is_below_the_free_floor`; the `docker` and `container` sandboxes and their closed network are enforced in code that no test runs; the local sandbox does not close the network, and actions run as the server's user unless `KC_ACTION_USER` is set)
date: "2026-10-14"
area: sandbox
kind: new
depends_on:
  - "[[ADR-0002-the-action|ADR-0002]]"
tags:
  - adr
  - computer
---

# ADR-0005: The sandbox and the action user

## Context

An action is an `argv` a model chose ([[ADR-0002-the-action|ADR-0002]]). It must not reach the
computer's own store, the network, or other work on the computer, and it should not run with the
server's privileges. The computer runs on a laptop, inside a container image, and on a cloud machine
without a nested container runtime, so one isolation mechanism does not fit every host.

This record fixes the three sandboxes an action can choose, what each closes, the user an action
runs as, and the free-space floor checked before a slot is cut. It also states plainly what the
local sandbox does not close. Source: `src/khive_computer/execer.py` (`run`, `action_user`,
`prepare_action_user`, `check_free_space`) and the `serve` command in `cli.py`.

## Definitions

- **sandbox**: how an action is isolated, one of `local`, `docker` or `container`, chosen per action
  and part of its key.
- **closed environment**: the variables a local action receives: `PATH` (a fixed default unless the
  caller passes one), `HOME` and `TMPDIR` inside its slot, `LANG`, `KC_SLOT`, `KC_TREE`,
  `CARGO_HOME` and `RUSTUP_HOME` when the host has them, and the caller's `env`.
- **action user**: the system user named by `KC_ACTION_USER`; local actions run as its uid and gid.
- **`network`**: the action flag that opens egress in the container sandboxes; off by default and
  part of the key.
- **free-space floor**: `KC_MIN_FREE_GB`, default 3, the free space the data directory's volume must
  have before an action starts.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | Where no nested runtime exists, a separate user is the isolation available. | the draft | the local sandbox needs a namespace or VM layer of its own |
| A2 | Toolchains on the server's `PATH` are the ones actions should use. | `DEFAULT_PATH` puts the server's own `bin` first | actions need images that carry their toolchains |
| A3 | A full volume causes failures that do not name their cause. | the draft and the floor's own message | the floor can go; the failures already say "disk full" |
| A4 | Actions on one computer seldom run heavy work at the same time. | the draft | a per-action memory cap is needed; a red seen under load is rerun alone before it is believed |

## Claims

### C1: An action names one of three sandboxes _(enforced: mechanical)_ ^c1

- **Subject**: every `exec.run`.
- **Violated when**: an action runs under a sandbox name other than `local`, `docker` or `container`.

`execer.run` refuses any other name before a key is computed. An image string, when given, is
recorded with the sandbox on the action row.

### C2: A local action gets a closed environment and a private temporary directory _(enforced: mechanical)_ ^c2

- **Subject**: every action in the `local` sandbox.
- **Violated when**: a local action inherits a server variable outside the closed set, or its `HOME`
  or `TMPDIR` lies outside its slot.

The process starts with the closed environment only, in its own session, with its working directory
inside the laid-out tree. A `cwd` that resolves outside the tree is refused and the slot removed.

### C3: An action is killed at its timeout _(enforced: mechanical)_ ^c3

- **Subject**: every action that runs.
- **Violated when**: an action outlives its timeout, or a child it started survives the kill.

The timeout defaults to 600 seconds. At expiry the whole process group is killed, and the result
reports `ended_by` `timeout` ([[ADR-0002-the-action#^c3|ADR-0002/C3]]).

### C4: The container sandboxes close the network unless asked _(enforced: mechanical)_ ^c4

- **Subject**: every action in the `docker` or `container` sandbox.
- **Violated when**: such an action without `network=true` runs without `--network none`.

`execer.run` builds `docker run` or `container run` with `--rm`, the slot mounted at `/work`, named
caches mounted, the caller's `env` passed with `-e`, and `--network none` unless `network` is true.
The default image is `python:3.12-slim`. The client process keeps the host's `DOCKER_HOST`,
`DOCKER_CONFIG` and `DOCKER_CONTEXT`; only the work inside the container gets the closed environment.

### C5: The local sandbox does not close the network _(enforced: mechanical)_ ^c5

- **Subject**: every action in the `local` sandbox.
- **Violated when**: a reader takes `network=false` on a local action as a guarantee of no egress.

In the local sandbox the `network` flag changes the key and nothing else: the process has the
host's network. Closing it is the host's job (a container the computer itself runs in, or a firewall
on the action user).

### C6: Local actions run as the action user when one is configured _(enforced: mechanical)_ ^c6

- **Subject**: every local action.
- **Violated when**: with `KC_ACTION_USER` set, a local action runs with another uid, or keeps the
  server's supplementary groups when the server runs as root.

`action_user` resolves the name to a uid and gid; the slot and the action's named caches are handed
to that user, and the process starts as it. When the server is root, supplementary groups are
dropped. At `kc serve`, `prepare_action_user` hands the cache root over once and leaves a marker so
the walk is not repeated. With `KC_ACTION_USER` unset, local actions run as the server's user.
Container actions run as the container's own user.

### C7: An action below the free-space floor is refused before a slot is cut _(enforced: mechanical)_ ^c7

- **Subject**: every action that is not a cache hit.
- **Violated when**: an action starts while the volume has less free space than the floor, or the
  refusal does not name both numbers.

`check_free_space` reads the volume holding the data directory and raises with the free space and
the floor. It runs after the cache lookup and before the slot is created.

### C8: A missing program is a result, not an error _(enforced: mechanical)_ ^c8

- **Subject**: every action whose `argv[0]` cannot be started.
- **Violated when**: a missing program raises out of `exec.run`.

The result carries exit code 127, the error text as stderr and `ended_by` `spawn_failed`, and is not
cached.

## Decisions

### D1: Three sandboxes behind one action verb ^d1

Serves C1, C2, C4, C5. `local` for hosts without a nested runtime, `docker` where a daemon exists,
`container` for Apple's container command line. The sandbox is chosen per action and is in its key.

- **Landing evidence**: `test_exec_runs_in_a_slot_and_captures_the_tree` (local: environment,
  timeout, missing slot afterwards). No test runs the `docker` or `container` branch.

### D2: Actions run as a separate user, not as the server ^d2

Serves C6. The ownership semantics a project's tests rely on (read-only files, process directories
owned by another user) hold only when the action is not the server's user.

- **Landing evidence**: `test_actions_run_as_the_configured_user`.

### D3: A free-space floor before every action ^d3

Serves C7. Build caches grow until they fill a volume; refusing up front names the cause.

- **Landing evidence**: `test_actions_refuse_when_the_volume_is_below_the_free_floor`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Containers only | no nested runtime on the cloud machines and images the computer runs in |
| Actions as the server's user by default everywhere | an action could write the store; ownership-sensitive tests go red under root |
| An egress proxy | deferred with the authority model ([[ADR-0003-the-seat\|ADR-0003]]) |
| A per-action memory cap | not built; contention is handled by rerunning a load-only red alone (A4) |
| Inheriting the server's environment | an action's result would depend on variables nobody passed |

## Consequences

- **S1**: `cargo fetch` with `network=true` into a named registry cache, then everything offline, is
  a workable hermetic shape in the container sandboxes.
- **S2**: On a laptop with `KC_ACTION_USER` unset, local actions run as the person running the
  server, with that person's network. The isolation claims of this record hold in full only for
  the container sandboxes, or for local actions with an action user on a host that limits egress.
- **S3**: A bind-mounted slot on macOS is not a native filesystem; tests that exercise SQLite WAL
  read-only semantics have been seen to fail there and nowhere else. A slot inside the VM's
  filesystem is the queued fix.
- **S4**: There is no per-action memory cap; concurrent heavy actions contend.
- **S5**: In the local sandbox a named cache is a symbolic link from the work tree to
  `caches/<name>` in the data directory. A link is not a boundary: a parent path through it reaches
  the other caches, and without an action user a local action can reach the whole data directory by
  absolute path.
