---
adr: ADR-0001
status: proposed
liveness: operating (functional writes, content-addressed tree ids, the compare-and-swap move with its history row and rollback as a recorded move are pinned by `test_tree_is_a_value_and_writes_are_functional`, `test_ref_cas_and_history`, `test_snapshot_rollback` and `test_whoami_and_edit_exec_move`; the path-escape refusal and the single-match edit are enforced in code; materialization is eager)
date: "2026-10-14"
area: tree
kind: new
depends_on: []
tags:
  - adr
  - computer
---

# ADR-0001: Trees are values; a ref is the only mutable name

## Context

A model editing files through tools has no working copy to protect it. If a write mutates a
directory in place, two sessions on the same project race, an edit cannot be undone by name, and
nothing outside the session has a stable thing to point at. Git gives commits this property; the
computer needs the same property one level down, for every edit, without a commit per keystroke.

This record fixes the two objects every other record builds on: the tree, a directory held as a
value, and the ref, the only name in the computer that changes. Actions run on trees
([[ADR-0002-the-action|ADR-0002]]), git imports into and exports from them
([[ADR-0004-git-at-the-edge|ADR-0004]]), and every move of a ref is attributed to a seat
([[ADR-0003-the-seat|ADR-0003]]). Source: `src/khive_computer/blobs.py`, `trees.py`, `fs.py`,
`merge.py`, `db/refs.py` and `db/snapshots.py`, and the ref and snapshot methods of `computer.py`.

## Definitions

- **blob**: bytes stored under their BLAKE3 hash in `blobs/`; written once, never rewritten. A read
  recomputes the hash and refuses a blob whose bytes no longer match it.
- **tree**: a directory as a value: a sorted list of entries serialized canonically and stored as a
  blob. Its id is that blob's hash.
- **entry**: one name in a tree with a kind (`file`, `exec`, `symlink` or `dir`) and the id of the
  blob or subtree it holds.
- **tree id**: the hash that names a tree; identical content has the same id wherever it appears.
- **ref**: a named pointer to a tree id, with a revision number `rev` that starts at 0 and rises by
  one on every move.
- **ref history**: one row per create or move of a ref: name, `rev`, the tree it left, the tree it
  reached, the seat, a note and the time.
- **snapshot**: the set of (ref, tree, rev) triples at a moment, stored under a name that is taken
  once; no content is copied, and a rollback is a compare-and-swap move per recorded ref.
- **branch**: a ref created from another ref with `branch.create`; it remembers the base ref and the
  base tree for a later three-way merge.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | Laying a tree out in full when an action needs it costs less than the action itself. | the draft defers lazy materialization (SPEC §7) | a lazy, mounted view of the tree replaces the copy in `Trees.materialize` |
| A2 | BLAKE3 ids do not collide for the content a computer holds. | the hash's published security level | identical ids could name different trees and every claim below fails |
| A3 | A model carries tree ids between calls when the shapes are ones it already knows. | the server's instructions and the tool shapes (numbered-line read, single-match edit) | clients need a session layer that tracks the current tree for the model |
| A4 | One server process owns a computer's store. | the compare-and-swap holds an in-process lock around a read and an update ([[ADR-0006-the-computer\|ADR-0006]]) | the swap needs a database transaction that spans processes |

## Claims

### C1: A write returns a new tree id and changes nothing that exists _(enforced: mechanical)_ ^c1

- **Subject**: every `fs.write`, `fs.edit` and `fs.remove`, and every `Trees.write`, `Trees.remove`
  and `Trees.with_entry`.
- **Violated when**: a tree id reads different bytes after a write, or two trees with identical
  content receive different ids.

A write rebuilds the path from the changed entry up to the root and returns the new root's id. The
old id still reads the old content. Writing identical content twice yields one id, and removing the
last file of a directory prunes the empty directory. `fs.diff(a, b)` lists the paths added,
removed or modified between two ids, by blob id and kind.

### C2: A path cannot leave its tree _(enforced: mechanical)_ ^c2

- **Subject**: every path handed to a file tool.
- **Violated when**: a path containing `..` resolves to an entry, or an entry name containing `/`,
  `.` or `..` is stored.

`Trees._parts` refuses a `..` component with "path escapes the tree"; `Trees.put` refuses an empty,
duplicate or slashed entry name. Replacing the root itself is refused.

### C3: A ref moves only by compare-and-swap _(enforced: mechanical)_ ^c3

- **Subject**: every `ref.move` and every internal move (import, merge, rollback).
- **Violated when**: a ref moves while its current tree differs from the tree the caller expected,
  or a refusal does not name the tree it found.

`ref.move(name, expected_tree, new_tree)` reads the ref's tree and, if it is not `expected_tree`,
raises `RefConflict` carrying the tree found; the ref is left as it was. Over the tool surface the
refusal is an error result, and the event records it as a failed call.

### C4: Every move is a history row _(enforced: mechanical)_ ^c4

- **Subject**: every create and move of a ref.
- **Violated when**: a ref's `rev` changes without a row in its history, or a row lacks the seat or
  the trees on either side.

A create writes row `rev` 0 with no old tree. A move increments `rev` and writes the old tree, the
new tree, the seat and the note in the same locked section as the update. `ref.history` reads the
rows newest first.

### C5: A rollback is a move, never an erasure _(enforced: mechanical)_ ^c5

- **Subject**: every `snapshot.rollback`.
- **Violated when**: a rollback rewrites or deletes history, or moves a ref without a row.

A snapshot stores each ref's tree and rev. Rolling back moves each ref whose tree differs by
compare-and-swap, with the note `rollback:<name>`, and recreates a ref that was deleted since. The
rev keeps rising: a ref rolled back after one move stands at rev 2.

### C6: An edit names one exact match or refuses _(enforced: mechanical)_ ^c6

- **Subject**: every `fs.edit`.
- **Violated when**: an edit applies with zero matches, or with more than one match when
  `replace_all` is not set.

`fs.edit` counts the old string: none refuses with "old string not found"; more than one refuses with
the count unless `replace_all` is set. A binary file refuses. `fs.read` returns numbered lines with
an offset and a limit, and reports a binary file by size instead of content.

### C7: A merge moves nothing when it conflicts _(enforced: mechanical)_ ^c7

- **Subject**: every `branch.merge`.
- **Violated when**: a merge with a conflicted path moves the target ref.

`three_way` compares base, ours and theirs path by path; a path changed on both sides is merged as
text by `git merge-file`, and one it cannot merge, or one deleted on a side and changed on the other,
is a conflict. `branch.merge` returns the conflicted paths and moves nothing; a clean merge moves the
target by compare-and-swap.

### C8: Discarding a ref loses no content _(enforced: by convention)_ ^c8

- **Subject**: every `branch.discard`.
- **Violated when**: a tree reachable from a discarded ref can no longer be read by its id.

`branch.discard` deletes the ref row only. No code in the computer deletes a blob, so every tree
the ref ever pointed at stays readable by id. Nothing collects unreferenced blobs either; that is the
cost of this claim.

## Decisions

### D1: Trees and blobs in one content-addressed store ^d1

Serves C1, C2. `Blobs` stores bytes under their BLAKE3 hash with an atomic rename on first write.
`Trees` serializes a sorted entry list as compact JSON and stores it as a blob, so a tree id is a
blob id. Paths are views computed by walking from the root id.

- **Landing evidence**: `test_tree_is_a_value_and_writes_are_functional`, `test_fs_views`.

### D2: Refs, history and snapshots in the SQLite store ^d2

Serves C3, C4, C5. The `refs` table holds the current tree and rev; `ref_history` holds one row per
rev, keyed by name and rev. `Computer.ref.move` performs the compare and the update under one lock.
Snapshots are JSON maps of ref to tree and rev.

- **Landing evidence**: `test_ref_cas_and_history`, `test_snapshot_rollback`, and the second
  `ref.move` refused over HTTP in `test_whoami_and_edit_exec_move`.

### D3: Familiar shapes for the file tools ^d3

Serves C6. A read with an offset and a limit returning numbered lines, and an edit that replaces one
exact string, are shapes a model already uses, so it carries tree ids without special training.

- **Landing evidence**: `test_fs_views`.

### D4: Branches are refs with a remembered base ^d4

Serves C7. `branch.create` records the source ref and its tree as the base; `branch.merge` uses that
base for the three-way merge, falling back to the target's tree when none is recorded.

- **Landing evidence**: `test_three_way_merge`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Edit a directory in place | two sessions race, and an edit cannot be undone or pointed at by name |
| A git commit per edit | a commit per keystroke is slow, and history fills with noise a model made while thinking |
| Mutable trees with locks | a lock held by a stalled session blocks every other; a value never blocks |
| Lazy materialization from the start | eager copy is simpler and suffices for the trees measured so far (A1) |
| Last writer wins on refs | a lost update is silent; a refused swap names the tree that won |

## Consequences

- **S1**: A session works on a tree for as long as it likes and publishes by one move; a lost race
  is a refusal naming the winner, not a corrupted directory.
- **S2**: A result or a diff can name content (a tree id) instead of a place, so a later reader can
  check exactly what was run or published.
- **S3**: Clients carry tree ids between calls. Any tool that takes a tree also takes a ref name and
  resolves it to the ref's current tree.
- **S4**: Materialization is eager: a whole tree is copied out when an action needs it. Lazy
  materialization is deferred.
- **S5**: The store only grows. Discarded refs and superseded trees keep their blobs until a
  collector exists, which this record does not provide.
- **S6**: `ref_delete` writes no history row; a discarded branch's earlier rows remain readable
  under its name.
