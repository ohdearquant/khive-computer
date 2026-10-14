---
adr: ADR-0002
status: proposed
liveness: operating (the key over seven inputs, the cache hit, the uncached timeout, the result tree with its changed paths, the removed slot and named caches kept out of the tree are pinned by `test_exec_runs_in_a_slot_and_captures_the_tree`, `test_caches_persist_across_actions_and_stay_out_of_the_tree` and `test_whoami_and_edit_exec_move`; the key carries no format version, and two actions sharing a named cache are not serialized)
date: "2026-10-14"
area: action
kind: new
depends_on:
  - "[[ADR-0001-trees-are-values|ADR-0001]]"
tags:
  - adr
  - computer
---

# ADR-0002: The action, keyed by its inputs and answered from a result cache

## Context

Lint, format, build and test are the same commands run many times on nearly the same content. A
model re-runs them freely because it cannot see that nothing changed. On a shared machine every
re-run costs minutes and contends with other work.

Because a tree is a value ([[ADR-0001-trees-are-values#^c1|ADR-0001/C1]]), the content an action
runs on has a name. This record fixes what an action is, which of its inputs make up its key, what
it returns, and when a result is served again without a run. The sandbox the action runs in is
[[ADR-0005-the-sandbox|ADR-0005]]. Source: `src/khive_computer/execer.py` and the `exec.run` and
`exec.output` tools in `server.py`.

## Definitions

- **action**: one run of `argv` on a tree inside a sandbox, through `exec.run(tree, argv, cwd, env,
  timeout, sandbox, image, fresh, caches, network)`.
- **action key**: the BLAKE3 hash of a canonical JSON object holding the tree id, `argv`, `cwd`,
  `env`, `sandbox`, `image` and `network`.
- **result**: what an action returns: the key, whether it was served from cache, how it ended
  (`ended_by`), the exit code, the time in milliseconds, the input and output tree ids, the changed
  paths with their count, the stdout and stderr blob ids, and the last 120 lines of each stream.
- **result cache**: the `actions` table, one row per key, holding the result of the last run that
  ended by itself.
- **slot**: a fresh directory under `slots/` holding the laid-out tree, a private home and a private
  temporary directory for one action; removed when the action ends.
- **named cache**: a persistent directory under `caches/<name>` mounted into an action at a path the
  caller names.
- **`fresh`**: the flag that forces a run under a key already answered.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | What an action can observe is fixed by its tree, argv, cwd, env, sandbox, image and network. | the draft and SPEC §3 invariant 3 | a cached result answers for a run that would differ; the missing input joins the key |
| A2 | A named cache changes how fast an action runs, never what it computes. | the draft | a poisoned cache yields wrong results under a valid key; `fresh=true` with a cleared cache is the remedy |
| A3 | A non-zero exit on an unchanged tree is usually deterministic. | the draft, which leaves caching failures as an open question | an environmental failure is served again until `fresh=true` |
| A4 | A 120-line tail of each stream is enough for a model to act on. | the default `TAIL_LINES` | the model reads the full stream with `exec.output` |

## Claims

### C1: The key is a function of seven inputs and nothing else _(enforced: mechanical)_ ^c1

- **Subject**: every `execer.action_key`.
- **Violated when**: two actions with identical tree, argv, cwd, env, sandbox, image and network
  receive different keys, or two that differ in one of them receive the same key.

`action_key` serializes the seven inputs with sorted keys and hashes the result with BLAKE3. The
`network` flag is in the key: the same action with the network open has a different key. Named
caches, the timeout, `fresh` and the calling seat are not in the key.

### C2: A key already answered is served without a run _(enforced: mechanical)_ ^c2

- **Subject**: every `exec.run` without `fresh`.
- **Violated when**: an action whose key has a cached result starts a process, or a cached answer
  differs from the stored row.

`execer.run` computes the key, looks it up, and on a hit returns the stored result with `cached`
true and the changed paths recomputed from the two tree ids. The free-space check and the slot come
after the lookup, so a hit costs neither. `fresh=true` skips the lookup and overwrites the row.

### C3: Only a result the process produced is cached _(enforced: mechanical)_ ^c3

- **Subject**: every action that ends.
- **Violated when**: a timed-out action or one whose program could not be started answers a later
  call.

An action that exceeds its timeout has its process group killed and returns `ended_by` `timeout`
with exit code -9; one whose program does not exist returns `ended_by` `spawn_failed` with exit
code 127. Neither is written to the cache. Only `ended_by` `process` writes the row.

### C4: A failure is cached like a success _(enforced: mechanical)_ ^c4

- **Subject**: every action whose process exits by itself.
- **Violated when**: a non-zero exit is run again under the same key without `fresh`.

`execer.run` writes the row for every process that ended by itself, whatever its exit code. A
deterministic failure is an honest answer; an environmental one is served again until a caller
passes `fresh=true` (A3).

### C5: The result names content, not a place _(enforced: mechanical)_ ^c5

- **Subject**: every result.
- **Violated when**: a result lacks the output tree id, or the changed paths disagree with
  `fs.diff` of the input and output trees.

After the process ends, the slot is read back into a tree with `Trees.from_dir`, and the changed
paths are the diff of input and output. The list is capped at 200 entries; `changed_count` is the
full count. Both streams are stored as blobs; `exec.output` reads either as numbered lines.

### C6: Each action gets a fresh slot that is removed afterwards _(enforced: mechanical)_ ^c6

- **Subject**: every action that runs.
- **Violated when**: an action sees a file another action left in a slot, or a slot survives its
  action.

The slot is a new directory named by a random id. The tree is laid out under it, the action runs,
the output tree is captured, and the slot is removed.

### C7: Tool caches and named caches stay out of the result tree _(enforced: mechanical)_ ^c7

- **Subject**: every output tree.
- **Violated when**: a path named in the ignore list, or a named cache's mount point, appears in
  an output tree.

`Trees.from_dir` skips `target`, `node_modules`, `.venv`, `__pycache__`, `.git`, `.DS_Store`,
`.ruff_cache`, `.pytest_cache` and `.mypy_cache`. A named cache's mount point is removed before
capture, so its contents never enter the tree. With `gitignore` the capture also skips every path
the tree's own `.gitignore` files ignore, read through git itself (`Trees.gitignored`); the option
is part of the action key, so a run with it and a run without it are two actions. Pinned by
`test_gitignore_option_drops_ignored_paths`.

### C8: A named cache persists across actions _(enforced: mechanical)_ ^c8

- **Subject**: every `caches` argument.
- **Violated when**: a second action does not see what a first wrote into the same named cache, or
  a cache name outside `[A-Za-z0-9._-]{1,64}` is accepted.

The cache lives under `caches/<name>` in the computer's data directory and is mounted into each
action that names it. In the local sandbox the mount path must stay inside the work tree.

### C9: A change to the key format is a cache miss _(enforced: by convention)_ ^c9

- **Subject**: every edit to `action_key`.
- **Violated when**: a key format change is made without being stated as one.

Any change to the serialized object changes every key, so old rows are never served again. The key
carries no version field today; making the format explicit with a version is the stated intent.

## Decisions

### D1: `exec.run` is the one action verb ^d1

Serves C1 to C8. `execer.run` resolves a ref or tree id, validates the sandbox, argv and caches,
computes the key, consults the cache, and only then checks free space and cuts a slot.

- **Landing evidence**: `test_exec_runs_in_a_slot_and_captures_the_tree` (cache hit, timeout not
  cached, slot removed, result tree), `test_whoami_and_edit_exec_move` (the action over HTTP).

### D2: Named caches are mounted, not keyed ^d2

Serves C7, C8. A cache changes speed, not meaning (A2), so it stays out of the key; it is mounted
per action and excluded from capture.

- **Landing evidence**: `test_caches_persist_across_actions_and_stay_out_of_the_tree`, which also
  asserts that the `network` flag changes the key.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| No cache; every call runs | a model re-runs checks it cannot see are unchanged, and each costs minutes |
| Key on the tree and argv only | an action with a different env or image would answer for another |
| Named caches in the key | a warm cache and a cold cache would never share a result, which is the point of the cache |
| Cache only exit code 0 | considered and left open; a deterministic failure is as useful an answer as a success |
| Return output by place (a path on disk) | a place changes; a tree id is checkable later |

## Consequences

- **S1**: A re-run of lint or tests on an unchanged tree is free; a changed file is one rebuild with
  a warm named cache.
- **S2**: An action key is a fact about a specific tree, checkable later. Workflows built above the
  computer can rest on keys without the computer knowing the workflow.
- **S3**: An environmental failure poisons its key until a caller passes `fresh=true`.
- **S4**: Two actions sharing a named cache at the same time can interfere; the caller serializes
  them. A per-cache work directory held by one action at a time is the queued fix.
- **S5**: A cached row records the seat that ran it, not the seat that was served; the event log
  records both calls.
