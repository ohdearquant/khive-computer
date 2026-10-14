---
adr: ADR-0004
status: proposed
liveness: operating (import, export as one commit, re-import following the remote branch, the log, the author's shape and the credential kept out of argv are pinned by `test_git_import_export`, `test_import_fresh_clone_at_branch`, `test_export_author_shape` and `test_git_env_and_slug`; the push result as data is enforced in code with no test against a remote; that an export's base is a fresh import is a convention)
date: "2026-10-14"
area: git
kind: new
depends_on:
  - "[[ADR-0001-trees-are-values|ADR-0001]]"
  - "[[ADR-0003-the-seat|ADR-0003]]"
tags:
  - adr
  - computer
---

# ADR-0004: Git at the edge; the computer holds the credential

## Context

Projects live in git repositories on a hosting provider. Inside the computer, work is trees and refs
([[ADR-0001-trees-are-values|ADR-0001]]). The two have to meet without the model holding a
credential or a working copy.

This record fixes git as an import and export boundary: four verbs that bring a repository's content
in as a tree, write a tree back out as a commit, send the branch, and read its history. The seat that
asks is the one recorded ([[ADR-0003-the-seat#^c4|ADR-0003/C4]]); the computer is the one that
holds the credential and pushes. Source: `src/khive_computer/gitio.py` (`git_env`, `import_repo`,
`check_author`, `export`, `push`, `log`) and the four git tools in `server.py`.

## Definitions

- **clone**: the computer's own git repository for an imported project, under `repos/<name>` in the
  data directory, registered in the `repos` table with its source.
- **import**: `git.import(src, name, ref)`: clone or fetch `src`, check out `ref` detached, read the
  working copy into a tree, and bind the ref `name` to it.
- **export**: `git.export(name, tree, branch, message, start_point, author)`: lay a tree over the
  clone's working copy and commit it on `branch`.
- **push**: `git.push(name, branch, remote)`: send `branch` to the remote with the computer's
  credential.
- **credential**: the token in the computer's `GITHUB_TOKEN` environment variable, handed to git by
  a credential helper.
- **author**: the commit author, `Name <email>`, named per export; the committer is the computer's
  configured identity, `KC_GIT_IDENTITY`.

## Assumptions

| #  | statement | source | if false |
| -- | --------- | ------ | -------- |
| A1 | One credential per computer is enough while one owner runs it. | the draft; [[ADR-0003-the-seat\|ADR-0003]] A1 | per-seat credentials held by the computer, or short-lived tokens minted per push |
| A2 | A remote branch is a better export base than any ref inside the computer. | the draft: an export from a rolled-back base once produced a branch the remote could not merge | exports would need a base check against the remote before committing |
| A3 | The clone is the slow part of an import, and callers can wait for it. | the draft | import needs a background job and a status verb |
| A4 | The import ignore list names no path a project tracks on purpose. | `DEFAULT_IGNORE` in `trees.py` | an export removes a tracked path the import never read (S5) |

## Claims

### C1: An import binds a ref to the tree of the requested branch _(enforced: mechanical)_ ^c1

- **Subject**: every `git.import`.
- **Violated when**: the ref's tree differs from the requested branch's content on the remote, or a
  second import of the same name reads a stale local branch.

A first import clones; a later one fetches with prune. A ref that exists on the remote is checked out
as `origin/<ref>`, so a stale local branch never wins, and a bare branch name is never handed to
checkout. The result names the ref, the tree, the commit and the time spent reading the tree, which
excludes the clone.

### C2: An import moves an existing ref by compare-and-swap _(enforced: mechanical)_ ^c2

- **Subject**: every import into a name that already has a ref.
- **Violated when**: an import overwrites a ref without a history row.

`import_repo` reads the ref's current tree and, if the new tree differs, moves it through
`ref.move` with the note `import <commit>`; a first import creates the ref with the same note
([[ADR-0001-trees-are-values#^c4|ADR-0001/C4]]).

### C3: An export writes a tree as one commit on a branch _(enforced: mechanical)_ ^c3

- **Subject**: every `git.export`.
- **Violated when**: an export leaves the branch's tree different from the exported tree, or
  writes more than one commit.

A branch that exists in the clone is checked out and continued; otherwise it starts from
`start_point` or the clone's current commit. Everything in the working copy except `.git` is removed,
the tree is laid out, and all changes are staged. No change returns `changed` 0 with the note
"nothing to commit" and writes no commit.

### C4: The author is named per export; the committer is the computer _(enforced: mechanical)_ ^c4

- **Subject**: every commit an export writes.
- **Violated when**: a commit is written with an author not of the form `Name <email>`, or with a
  committer other than the configured identity.

`check_author` accepts exactly one name and one angle-bracketed address and refuses anything else.
With no author given, the author is the seat's name at `localhost`. The committer is
`KC_GIT_IDENTITY` (`Name <email>`, default `khive computer <kc@localhost>`), checked by the same rule.

### C5: The seat never sees the credential _(enforced: mechanical)_ ^c5

- **Subject**: every git subprocess and every tool result.
- **Violated when**: the token appears in a git argument, a URL, the store or a tool result.

`git_env` sets a credential helper through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and
`GIT_CONFIG_VALUE_0`; the helper reads `GITHUB_TOKEN` from the environment when git asks. The value
in the git configuration names the variable, not its content. No tool reads or returns the
environment.

### C6: A refused push is data _(enforced: mechanical)_ ^c6

- **Subject**: every `git.push`.
- **Violated when**: a refused push raises instead of returning, or returns `ok` true.

`gitio.push` runs `git push --porcelain -u <remote> <branch>:<branch>` and returns `ok` as whether
git exited 0, with the last 2000 characters of its output. Callers check `ok`.

### C7: The log reads the clone's current branch _(enforced: mechanical)_ ^c7

- **Subject**: every `git.log`.
- **Violated when**: the log reads a history other than the named clone's checked-out commit.

`gitio.log` returns the clone's current branch and its last `n` commits as hash, author, date and
subject. After an export that is the exported branch; after an import the clone is detached and the
branch reads `HEAD`.

### C8: An export's base is a fresh import, never a rolled-back ref _(enforced: by convention)_ ^c8

- **Subject**: every export a seat intends to push.
- **Violated when**: an export is committed on a base the remote no longer has, because the base
  came from a ref rolled back inside the computer.

The code takes the base from the clone, never from a ref, so a rollback inside the computer does not
change it. The base is fresh only if the caller imports before exporting; a branch already exported
once continues from its previous export.

## Decisions

### D1: Git is an adapter over trees, one clone per repository ^d1

Serves C1, C2, C3, C7. The computer keeps a clone per imported name; import reads a commit's tree
into the blob store and export writes a tree back. Inside the computer there is no working copy a
seat can reach.

- **Landing evidence**: `test_git_import_export` (import, export, log, and re-import at the exported
  branch yielding the exported tree), `test_import_fresh_clone_at_branch`.

### D2: The credential lives in the computer's environment ^d2

Serves C4, C5, C6. A credential helper in the environment of each git subprocess, never a token in a
remote URL.

- **Landing evidence**: `test_git_env_and_slug`, `test_export_author_shape`.

### D3: The surface stops at push and log ^d3

Serves C6, C7. The git verbs are `git.import`, `git.export`, `git.push` and `git.log`.

- **Landing evidence**: the four tools in `build_server`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| The model holds a credential | a token in a prompt or a client file the model writes is a token anyone can read back |
| A working copy per seat | a second, mutable copy of what trees already hold, with none of their properties |
| Export from the ref's history | a rolled-back ref is a statement about the computer, not the remote |
| Pull requests, reviews and releases as computer verbs | workflow belongs above the computer, built on these verbs |
| A credential per seat now | deferred until a computer has more than one owner (A1) |

## Consequences

- **S1**: A model can take a repository in, change it as tree values, run actions on it and push
  the result with no local checkout and no shell.
- **S2**: One credential per computer; the pusher is the computer. Who asked is in the event log.
- **S3**: A refused push returns `ok` false with git's output; a caller that ignores `ok` believes a
  push that did not happen.
- **S4**: An import of a large repository is bounded by the clone, and the import's own timing does
  not include it.
- **S5**: Paths the import's ignore list skips (`target`, `node_modules`, `.venv` and the like) are
  absent from the tree, so an export of a repository that tracks such a path removes it from the
  commit.
- **S6**: `src` may be a URL or a path the server can read; under full authority
  ([[ADR-0003-the-seat#^c5|ADR-0003/C5]]) a seat can import any repository the computer's host can
  reach.
