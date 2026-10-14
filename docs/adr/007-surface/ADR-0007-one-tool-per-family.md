---
adr: ADR-0007
status: proposed
liveness: operating (exactly eight tools, the closed action enum per family, binding to the chosen action's signature with a refusal that names it, the per-action lines in each tool description and the `family.action` event verb are pinned by `test_eight_families_dispatch_by_action` and `test_whoami_and_edit_exec_move`; the written spelling `family.action` in documents is a convention)
date: "2026-10-14"
area: surface
kind: new
depends_on:
  - "[[ADR-0003-the-seat|ADR-0003]]"
tags:
  - adr
  - computer
---

# ADR-0007: One tool per family; the action names the verb

## Context

The first cut of the server registered one MCP tool per verb: thirty tools for files, refs,
branches, actions, git, snapshots, the seat and the computer's own metrics. Every tool a server
exposes is text the client carries on every turn, and the surface will grow: more file views, more
git verbs, more ways to observe an action. At one tool per verb the list reaches fifty to a hundred
entries, and a model picking among a hundred names does worse than a model picking among eight.

This record fixes the shape of the tool surface: eight tools, one per family of verbs, with the verb
chosen by an `action` argument. The verbs themselves, their arguments and what they do are recorded
in the family's own record ([[ADR-0001-trees-are-values|ADR-0001]] for `fs`, `ref` and `branch`,
[[ADR-0002-the-action|ADR-0002]] for `exec`, [[ADR-0004-git-at-the-edge|ADR-0004]] for `git`).
Every call is an event under its seat ([[ADR-0003-the-seat|ADR-0003]]); this record fixes the verb
string that event carries. Source: `src/khive_computer/server.py`, `build_server` and `family`.

## Definitions

- **family**: one MCP tool whose name is a noun: `seat`, `fs`, `ref`, `branch`, `exec`, `git`,
  `snapshot`, `computer`. The eight names are the tuple `FAMILIES`.
- **action**: the verb inside a family, the value of the tool's `action` argument; a closed set per
  family, carried in the schema as an enum.
- **handler**: the function that implements one action, with its own signature. The family's
  schema is built from the handlers; the handlers are not tools.
- **written form**: `family.action` (for example `branch.merge`) is how a verb is written in these
  documents and recorded in the event log. On the wire the call is the tool `branch` with
  `action="merge"`: MCP clients restrict tool names to letters, digits, `_` and `-`.
- **union schema**: the tool's parameters are `action` plus every parameter of every handler in the
  family, each optional in the schema. A handler's own signature decides which of them a given
  action takes.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | A model picks an action from a short enum at least as reliably as it picks one tool from a list of thirty or more. | the shape of other large tool surfaces that expose one entry per domain with a verb argument | split the largest families (`fs`) back into several tools |
| A2 | A client accepts a schema whose fields are all optional and reads the description to learn which fields an action takes. | MCP tool schemas are plain JSON Schema; optional properties are ordinary | move to a single `args` object per call and lose field-level schema |
| A3 | Two handlers in one family that share a parameter name mean the same thing by it. | the handlers today: `tree`, `path`, `name`, `note`, `limit` are used alike | the union collapses the annotation to `Any` and the description carries the distinction |
| A4 | Eight tools stay under every client's comfortable tool budget. | eight is a small list for any current client | fold `computer` into `seat` |

## Claims

### C1: The server exposes exactly eight tools _(enforced: mechanical)_ ^c1

- **Subject**: the tool list a client receives.
- **Violated when**: a verb is registered as its own tool, or a ninth family appears without a
  record amending this one.

`build_server` registers one tool per name in `FAMILIES` and nothing else. The test lists the tools
over HTTP and compares the names with the tuple.

### C2: The action is a closed set per family _(enforced: mechanical)_ ^c2

- **Subject**: the `action` argument of every family tool.
- **Violated when**: a value outside the family's handlers reaches a handler, or the schema shows
  `action` as a free string.

The parameter is typed `Literal[...]` over the handler names, so the schema carries the enum and the
server's argument validation refuses any other value before dispatch, naming the allowed values.

### C3: A call is bound to the chosen action's signature or refused naming it _(enforced: mechanical)_ ^c3

- **Subject**: every call to a family tool with a valid action.
- **Violated when**: a handler runs with an argument it does not take or without one it requires,
  or a refusal does not say which action and which signature it was checked against.

Fields left `None` are dropped; the rest are bound to the handler's signature with
`inspect.Signature.bind`. A binding error is returned as `family.action: <reason>; takes (<signature>)`.

### C4: Every executed call is an event under `family.action` _(enforced: mechanical)_ ^c4

- **Subject**: every call whose handler ran, succeeded or failed.
- **Violated when**: an executed call leaves no event, or its event carries a verb other than
  `family.action`.

The dispatcher records the event after the handler returns or raises, with the bound arguments, the
outcome, the latency and the sizes, under the verb string `f"{family}.{action}"`. `computer.metrics`
groups by that string.

### C5: A call refused before execution is not an event _(enforced: mechanical)_ ^c5

- **Subject**: calls refused by argument validation (C2) or binding (C3).
- **Violated when**: a refused call appears in `computer.events`.

Validation and binding happen before the actor is read and before the handler runs; nothing was done
on the computer, so nothing is recorded. The test refuses a `branch.merge` without `from_ref` and reads
the newest event, which is the call before it.

### C6: Each tool's description lists its actions with their signatures _(enforced: mechanical)_ ^c6

- **Subject**: the description of every family tool.
- **Violated when**: an action is callable but absent from the description, or its listed
  signature differs from the handler's.

The description is generated from the handlers: one line per action, `- <action>(<signature>): <first
paragraph of the handler's docstring>`. The test looks for the `merge` line of `branch`.

### C7: Documents write a verb as `family.action` _(enforced: by convention)_ ^c7

- **Subject**: `SPEC.md`, `README.md` and every record in this directory.
- **Violated when**: a document names a verb by a tool name that does not exist (`fs_read`) or by
  the wire form (`fs` with `action="read"`) where the written form is meant.

## Decisions

### D1: One tool per family, built from the handlers ^d1

Serves C1, C2, C3, C6. `family(name, summary, actions)` takes the handlers, builds the union of their
parameters, sets the tool's signature and annotations to `action: Literal[...]` plus the optional
union, generates the description, and registers the one tool. Adding a verb is adding a handler to
the family's table; no new tool appears.

- **Landing evidence**: `test_eight_families_dispatch_by_action`.

### D2: The event verb is `family.action` ^d2

Serves C4, C5. The dispatcher writes the event; the handlers do not. The verb string is the written
form, so the event log and the documents name the same thing.

- **Landing evidence**: `test_whoami_and_edit_exec_move` (events `fs.edit` and `computer.metrics`),
  `test_eight_families_dispatch_by_action` (no event for a refused call).

### D3: Documents use the written form ^d3

Serves C7. A reader of a record or the spec sees `ref.move`; a client sends `ref` with
`action="move"`. The spec's tool section says so once.

- **Landing evidence**: by inspection of `docs/SPEC.md` section 2.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| One tool per verb | thirty tools today and a hundred later; the list is carried on every turn and the choice degrades with its length |
| One tool for everything, taking a command string in a small language | one entry, but no schema at all: the client learns the arguments only from prose, and every call is parsed twice |
| One tool per family taking `action` and a free `args` object | fewer schema fields, but the client gets no type or name for any argument |
| Tool names with dots (`branch.merge` as the tool) | MCP clients restrict tool names to letters, digits, `_` and `-` |
| Grouping by object kind only (`tree`, `ref`) with every verb inside | `fs` would hold reads, writes, globbing, diffs and git imports; the families follow what a model does, not what the store holds |

## Consequences

- **S1**: A family's schema shows every field of every action in it. A client sees `old` and `new` on
  `fs` even when it is reading; the description says which action takes which.
- **S2**: A verb that fits an existing family costs no new tool; a verb that fits none costs a record
  amending C1.
- **S3**: A parameter name shared by two handlers with different types collapses to `Any` in the
  schema (A3). No such pair exists today; a new one is a reason to rename the parameter.
- **S4**: The event log groups by `family.action`; a metrics reader built on the old per-verb names
  reads the new strings.
- **S5**: The wire form and the written form differ by one character. Documents say `branch.merge`;
  a client sends `branch` with `action="merge"`.
