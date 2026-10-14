# khive computer: specification (v0.1)

This document states what the computer is, the objects it holds, the tools it exposes, and the
invariants a client can rely on. Decisions and their reasons are in `adr/`.

## 1. Objects

**Computer.** One data directory (`KC_ROOT`): a SQLite store plus an immutable blob store. One owner
per computer. Everything below lives inside it.

**Seat.** An identity that acts. A seat has a name, an identity text, a system prompt and a loop
state, and exactly one bearer token. Every call is attributed to the seat whose token made it.
`seat.whoami` returns the seat; `seat.update` changes its texts. The token is never returned by
any tool.

**Blob.** Bytes addressed by their BLAKE3 hash. Immutable.

**Tree.** A directory as a value: a content-addressed map of paths to blobs and modes. A tree id
names content wherever that content appears. Any operation that "changes" a tree returns a new tree
id; the old one is untouched. `fs.empty()` is the tree with nothing in it.

**Ref.** The only mutable name in the computer. A ref points at a tree id and carries a history of
moves: who, when, from which tree, to which tree.

**Action.** One run of `argv` on a tree inside a sandbox, with an optional working directory,
environment, timeout, image, named caches and network flag. An action has a key derived from its
inputs, and a result: exit code, output tails, the result tree, the changed paths and timing.

**Snapshot.** Where every ref points at one moment, recorded under a name: one map of ref to
(tree, rev), nothing copied (trees are values and blobs are never deleted). `snapshot.rollback`
moves each recorded ref back to its recorded tree by compare-and-swap, one history row per ref that
differs; a ref discarded since is created again, a ref created since is left alone. A name is taken
once: `snapshot.create` refuses a name that exists. For one ref, `ref.history` and `ref.move` do the
same thing; a snapshot is that for every ref at once, under one name.

**Event.** One row per tool call: seat, tool, the arguments as JSON (cut at 4,000 characters), whether
it succeeded, duration, bytes in and out, and the error text if any. `computer.metrics` and `computer.events`
read them.

## 2. Tools (eight, one per family)

Eight MCP tools, one per family: `seat`, `fs`, `ref`, `branch`, `exec`, `git`, `snapshot`, `computer`.
The verb is the tool's `action` argument, a closed set per family; the other fields are the union of
the family's actions, and a call is bound to the chosen action's own signature or refused naming it.
This document writes a verb as `family.action`: `fs.read` is the tool `fs` with `action="read"`. Each
tool's description lists its actions with their signatures, and every executed call lands as an event
under that same `family.action` string (ADR-0007).

Signatures as the server reports them (`?` marks an optional field):

- `seat`: `seat.whoami()`, `seat.update(loop_state?, identity?, system_prompt?)`.
- `fs`: `fs.read(tree, path, offset?, limit?)` (numbered lines), `fs.list(tree, path?)`,
  `fs.glob(tree, pattern, limit?)`, `fs.grep(tree, pattern, path?, limit?, ignore_case?)`,
  `fs.write(tree, path, content, executable?)`, `fs.edit(tree, path, old, new, replace_all?)` (exactly
  one match unless `replace_all`), `fs.remove(tree, path)`, `fs.diff(a, b)`, `fs.empty()`. `tree` and
  `a`, `b` take a tree id or a ref name.
- `ref`: `ref.list()`, `ref.get(name)`, `ref.create(name, tree, note?)`,
  `ref.move(name, expected_tree, new_tree, note?)`, `ref.history(name, limit?)`.
- `branch`: `branch.create(name, from_ref, note?)`, `branch.merge(into, from_ref, note?)` (three-way,
  conflicts reported, nothing moved on conflict), `branch.discard(name)`.
- `exec`: `exec.run(tree, argv, cwd?, env?, timeout?, sandbox?, image?, fresh?, caches?, network?,
  gitignore?)`,
  `exec.output(blob, offset?, limit?)` (a stored stdout or stderr blob as numbered lines).
- `git`: `git.import(src, name, ref?, gitignore?)`, `git.export(name, tree, branch, message, start_point?, author?)`,
  `git.push(name, branch, remote?)`, `git.log(name, n?)`.
- `snapshot`: `snapshot.create(name, note?)`, `snapshot.list()`, `snapshot.rollback(name)`.
- `computer`: `computer.metrics(window_s?)`, `computer.events(n?)`.

Shapes follow what a model already knows: a read with offset and limit returning numbered lines; an
edit that matches one exact old string and refuses otherwise.

## 3. Invariants

1. A tree id is a value. Nothing rewrites a tree; every edit produces a new id.
2. A ref moves only by compare-and-swap. `ref.move` names the tree it expects to find and refuses
   if the ref has moved, naming the tree it found.
3. An action key is a function of the action's inputs: tree, argv, cwd, env, image, sandbox,
   network. Named caches are not part of the key (they affect speed, not meaning). The same key is
   answered from the result cache; `fresh=true` forces a run. A change to the key format is a cache
   miss, by design.
4. An action gets the tree laid out in a slot, a closed environment, a private `TMPDIR` and a
   timeout. With `KC_ACTION_USER` set, local actions run as that user, never as the server's; unset,
   they run as the server's user. The `docker` and `container` sandboxes close the network unless
   `network=true`; the `local` sandbox does not close it (the flag only changes the key).
5. Tool caches (`target`, `.pytest_cache`, `.ruff_cache`, …) are excluded from a result tree.
6. The computer holds the git credential. A seat imports, exports and pushes through the computer's
   verbs; it never receives the token. The commit author is the one named on the export; the
   committer is the computer's configured identity (`KC_GIT_IDENTITY`); the pusher is the computer.
7. Every call is an event attributed to the seat on the token. There are no anonymous tool calls;
   `GET /healthz` is the one anonymous route: it answers `{"ok": true}` when the server is up and
   carries nothing about the computer.
8. One token is one seat with full authority over the computer. v0.1 has no capability gate, no
   quotas and no principals; see [ADR-0003](adr/003-seat/ADR-0003-the-seat.md) for what that buys and
   what it costs.

## 4. Sandboxes

- `local`: a slot directory on the computer's host, closed environment, timeout, tree capture.
  Used inside a container or on a cloud machine where no nested runtime exists.
- `docker`: the action runs in a container (`--network none` by default) with the slot and the
  named caches mounted. The docker client and the action are different processes: the client keeps
  the host's `DOCKER_HOST`; only the work inside the container gets the closed environment.
- `container`: the same shape on Apple's container CLI.

Named caches are persistent directories mounted into an action and excluded from the result tree
(a cargo registry, a build target). Two actions sharing a cache name at the same time may interfere;
v0.1 leaves serialization to the caller.

## 5. Git at the edge

`git.import` clones (or fetches) a repository into the computer and sets a ref to the tree of the
requested branch. `git.export` writes a tree as one commit on a branch of the clone: a branch that
exists continues, a new one starts from `start_point` or the clone's current commit. The base never
comes from a ref inside the computer, so a rollback there is a statement about the computer, not
about the remote; import first when the base must be the remote's current branch. `git.push` uses
the computer's credential and records the act as an event of the asking seat. Anything above push
(pull requests, reviews, releases) is a use case built on these verbs, not part of the computer.

## 6. Operation

`kc init` creates a computer; `kc seat create <name>` mints a seat and prints its token once;
`kc serve` exposes MCP over HTTP (`/mcp`), bearer token per seat. Shutdown drains in-flight calls
for `KC_DRAIN_SECONDS` before the listener closes. `KC_MIN_FREE_GB` refuses an action when the data
disk has less than that free, before a slot is cut. `KC_ACTION_USER` names the user actions run as.
`KC_SEAT_TOKEN` (name `KC_SEAT_NAME`, default `operator`) is the first seat: `kc serve` creates it or re-keys it to
that token before listening, so a host hands the seat in through its environment and the computer stores a hash.
`kc build -n NAME [--rust --node --apt <pkg>]` fixes a deployment's image and first-seat token (minted when not
given, printed); `kc up NAME` runs it in Docker here and prints the client configuration; `kc down`, `kc purge`,
`kc prune`, `kc ps` manage it. `kc build` without `-n` builds only the image.

Whatever runs the process (Docker on a laptop, a cloud machine, a sandbox provider) is the computer's
host. Deploying, waking and sleeping a computer are the host's work and live outside this repository;
the computer owes a host four things, recorded in [ADR-0008](adr/008-host/ADR-0008-what-a-host-gets.md):
readiness as the answer to `GET /healthz`, a drain on SIGTERM that the host's stop grace must exceed,
seats minted with `kc seat create` on the data directory, and the data directory as the identity of
the deployment (the same directory under new compute is the same computer).

## 7. Non-goals for v0.1

No agent loop inside the computer: the client owns the loop. No multi-tenancy: one computer, one
owner. No memory, messaging or task store: those are other services a client can hold beside this
one. No review or release workflow: use cases come after the computer. No Postgres, no UI, no
OAuth. Durability is SQLite WAL plus immutable blobs on one disk; back up the data directory.
