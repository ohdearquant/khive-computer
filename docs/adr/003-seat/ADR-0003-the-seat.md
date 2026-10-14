---
adr: ADR-0003
status: proposed
liveness: operating (the refused bad token, the seat's own mirror and every tool call landing as an event under its seat are pinned by `test_bad_token_is_refused` and `test_whoami_and_edit_exec_move`; rotation (the old token refused at once, the seat unchanged) is pinned by `test_docker_round_trip` against a real container; the hashed token, the CLI-only minting and the closed set of updatable fields are enforced in code without a dedicated test; one token per role and the sub-agent rule are conventions)
date: "2026-10-14"
area: seat
kind: new
depends_on: []
tags:
  - adr
  - computer
---

# ADR-0003: The seat: one token, full authority, every call an event

## Context

A full authority model (principals, capabilities, attenuated tokens for sub-agents, quotas, an
egress proxy) is the right end state for a computer several parties share. It is also most of the
work, and this version's question is whether the model-facing surface is right, not whether the
security model is.

This record fixes the smallest identity that answers "who did this": the seat, its one bearer
token, and the event every call leaves. It defers everything that would limit what a seat may do.
Git acts are attributed through this record ([[ADR-0004-git-at-the-edge|ADR-0004]]), and the
computer that mints seats is [[ADR-0006-the-computer|ADR-0006]]. Source: the seat and event methods
of `src/khive_computer/computer.py` over `db/seats.py` and `db/events.py`, `SeatVerifier` and the
`family` dispatcher in `server.py`, and the `seat` commands in `cli.py`.

## Definitions

- **seat**: an identity that acts on the computer: a unique name, an identity text, a system prompt,
  a loop state and exactly one bearer token.
- **token**: a random string with the prefix `kc_`, shown once when minted; the store keeps only its
  SHA-256 hash.
- **`seat.whoami`**: the tool that returns the calling seat's name, identity text, system prompt, loop
  state, creation time and the computer's name.
- **event**: one row per tool call: time, seat, tool name, the call's arguments as JSON truncated to
  4000 characters, success, duration, bytes in and out, and the error text of a failed call.
- **`computer.metrics`**: calls, errors, bytes and per-tool latency percentiles over a time window, read from
  the events.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | One owner runs a computer, so every seat may be trusted with all of it. | the draft; SPEC §7 (no multi-tenancy) | a capability gate and principals are needed before a second party gets a seat |
| A2 | A token kept in a file the client reads stays out of prompts and logs. | the draft | a leaked token is the computer (S1); rotation is the only remedy |
| A3 | The event log is enough audit for one owner. | the draft | an authorization layer with its own audit replaces it |
| A4 | Recording arguments up to 4000 characters is acceptable for the content seats write. | the `Computer.event` truncation | events record a digest of the arguments instead of the arguments |

## Claims

### C1: A seat is minted only by the computer's own command line _(enforced: mechanical)_ ^c1

- **Subject**: every seat in the store.
- **Violated when**: a tool call creates a seat or mints a token.

`kc seat create <name>` calls `Computer.seat_create`, which prints the token once; `kc seat rotate
<name>` replaces the stored hash and prints a new token. `build_server` registers no tool that
creates a seat or returns a token.

### C2: The token is never returned by a tool and never stored in the clear _(enforced: mechanical)_ ^c2

- **Subject**: every tool result and every seat row.
- **Violated when**: a tool result carries a token, or the `seats` table holds one in plain text.

The store keeps `token_hash`, the SHA-256 of the token, and looks a seat up by hashing the presented
token. `Computer.whoami` builds its result from the name, the three texts and the creation time
only.

### C3: A call without a valid token is refused _(enforced: mechanical)_ ^c3

- **Subject**: every request to the MCP endpoint.
- **Violated when**: a tool runs for a request whose bearer token matches no seat.

`SeatVerifier.verify_token` returns nothing for an unknown token, and the transport refuses the
request before any tool runs. A tool that finds no access token raises `PermissionError`.

### C4: Every tool call is an event attributed to its seat _(enforced: mechanical)_ ^c4

- **Subject**: every call dispatched through `family` ([[ADR-0007-one-tool-per-family|ADR-0007]]).
- **Violated when**: a tool call, ok or failed, leaves no event, or its event names a seat other
  than the one whose token made it.

The dispatcher resolves the seat from the access token, times the call, and writes the event on success
and on failure, the failure with its exception type and message. `computer.events` and `computer.metrics` read
the table. Commands run through `kc` on the host are not tool calls and leave no event; an import
from the command line records the actor `cli` on its ref history row.

### C5: Every seat has full authority _(enforced: mechanical)_ ^c5

- **Subject**: every seat and every tool.
- **Violated when**: a tool refuses a seat for lack of a permission.

`SeatVerifier` grants every seat the one scope `computer`, and no tool checks a scope or a seat
name before acting. There is no capability gate, no principal hierarchy and no quota.

### C6: A seat changes only its own texts _(enforced: mechanical)_ ^c6

- **Subject**: every `seat.update`.
- **Violated when**: a seat changes its name, its token, or another seat's row through a tool.

`seat.update` acts on the calling seat and accepts only `identity`, `system_prompt` and
`loop_state`; any other field is refused with "not updatable".

### C7: One token per role _(enforced: by convention)_ ^c7

- **Subject**: every deployment that runs more than one session.
- **Violated when**: two sessions that must not share authority (one writes, one judges) share a
  token.

Two such sessions get two seats, and a workflow that cares which seat did what reads the event log.
Nothing in the computer can tell two sessions on one token apart.

### C8: A sub-agent works under its parent's token _(enforced: by convention)_ ^c8

- **Subject**: every client that starts sub-agents.
- **Violated when**: a sub-agent is handed a token minted for it, expecting narrower authority.

Attenuated tokens are deferred, so a sub-agent's calls are its parent seat's calls.

## Decisions

### D1: A bearer token per seat on the MCP transport ^d1

Serves C1, C2, C3. `kc mcp-config --token` prints a client configuration with the token in an
`Authorization: Bearer` header; the server verifies it per request through `SeatVerifier`.

- **Landing evidence**: `test_bad_token_is_refused`; `test_whoami_and_edit_exec_move` serves a seat
  over real HTTP and reads its identity back through `seat.whoami`.

### D2: The event log is the audit surface ^d2

Serves C4. One dispatcher runs every action, so no verb can be added without its event.

- **Landing evidence**: `test_whoami_and_edit_exec_move` reads `computer.metrics` (calls and one error, the
  refused move) and `computer.events` (every event under the test's seat).

### D3: No authorization layer in this version ^d3

Serves C5. This version measures the surface; the authority model is deferred until a second party
needs a seat.

- **Landing evidence**: the single scope in `SeatVerifier.verify_token`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Principals, capabilities and attenuated tokens now | most of the work, and not this version's question |
| A seat-minting tool | a seat with a token could mint seats; minting stays with whoever holds the data directory |
| Tokens stored in the clear | a copy of the database would be every seat |
| Anonymous calls for reads | an unattributed read is a hole in the only audit surface |
| Authority checks per tool by seat name | a list of names in code is a capability model without the design |

## Consequences

- **S1**: A leaked token is the computer. Tokens live in files the client reads, never in prompts or
  logs; `kc seat rotate` replaces a leaked one.
- **S2**: The event log, not an authorization layer, is the audit surface. It holds call arguments up
  to 4000 characters, so file content written through a tool appears in it.
- **S3**: Per-seat git identity is deferred: the computer pushes with one credential and the commit
  author is set per export ([[ADR-0004-git-at-the-edge#^c4|ADR-0004/C4]]).
- **S4**: The authority model is to be revisited before a computer has more than one owner.
