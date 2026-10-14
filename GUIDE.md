# Using a khive computer

This guide is for the model, or the person behind it, driving a computer through its eight tools.
Standing one up is in `README.md` and `docs/DEPLOY.md`; what every tool takes is `docs/SPEC.md`
section 2; changing the computer itself is `AGENTS.md`.

> From `AGENTS.md`:
> "A claim marked mechanical names the test that pins it; a change that breaks the claim breaks
> the test, and a change that moves the surface updates SPEC.md, the family's record and the tests
> in the same commit."

So what this guide describes is what the tests hold; when the guide and the computer disagree, one
of them has a bug, and `docs/adr/INDEX.md` says which claim to look at.

## What you are talking to

- A **seat** is you: one bearer token, a name, an identity text and a loop state you may write back.
  Every call you make is recorded under your seat.
- A **tree** is a directory as a value, named by its hash. Reading never changes it; writing returns a
  new tree and leaves the old one where it was.
- A **ref** is the only name that moves. It points at a tree. A ref moves only by compare-and-swap:
  you name the tree you started from, and if someone moved the ref first the move refuses and tells
  you the tree it found. Read again, redo your edit on that tree, move again.
- An **action** is a command run on a tree in a sandbox. Its result (exit code, output, the tree it
  produced) is cached under the action's inputs, so the same command on the same tree is answered
  without running.
- **Git** sits at the edge. A repository is imported into a ref; a tree is exported as a commit; the
  computer pushes with its own credential. You never hold a token for the remote.

Eight tools: `seat`, `fs`, `ref`, `branch`, `exec`, `git`, `snapshot`, `computer`. The verb is the
`action` argument. This guide writes `fs.read` for the tool `fs` with `action="read"`.

## A session

Start by asking who you are:

```json
{"tool": "seat", "arguments": {"action": "whoami"}}
```

The answer carries your seat name, identity, system prompt and loop state, and the computer's name.

Look at a project. Any argument called `tree` takes a ref name or a tree id:

```json
{"tool": "fs", "arguments": {"action": "list", "tree": "app", "path": ""}}
{"tool": "fs", "arguments": {"action": "read", "tree": "app", "path": "app.py", "offset": 1, "limit": 200}}
```

Change a file. The result has a new `tree`; the ref `app` has not moved:

```json
{"tool": "fs", "arguments": {"action": "edit", "tree": "app", "path": "app.py", "old": "hi", "new": "hello"}}
```

Run something on the new tree and read the result:

```json
{"tool": "exec", "arguments": {"action": "run", "tree": "<new tree>", "argv": ["python3", "app.py"]}}
```

`exit_code`, `stdout_tail` and `stderr_tail` (the last 120 lines), `tree_out` (the tree the command
left behind), `changed` and `changed_count`, `cached` (true when the answer came from the cache),
`ended_by` (`process`, `timeout` or `spawn_failed`). Long output: `exec.output` with the `stdout` or
`stderr` blob id reads it as numbered lines.

Publish by moving the ref, naming the tree you started from:

```json
{"tool": "ref", "arguments": {"action": "move", "name": "app", "expected_tree": "<the tree app pointed at>", "new_tree": "<new tree>"}}
```

Export and push:

```json
{"tool": "git", "arguments": {"action": "export", "name": "app", "tree": "app", "branch": "feat", "message": "say hello", "author": "Your Name <you@example.com>"}}
{"tool": "git", "arguments": {"action": "push", "name": "app", "branch": "feat"}}
```

`git.push` returns `ok` and git's own output. A refused push is data, not an error: read `ok`.

## Patterns

- **Work on a branch.** `branch.create(name, from_ref)` makes a ref at the same tree that remembers its
  base. Move it as you work; `branch.merge(into, from_ref)` merges three-way and moves `into` only when
  nothing conflicts; on conflict it lists the paths and moves nothing. `branch.discard(name)` drops the
  ref and loses no content.
- **Snapshot before something risky.** `snapshot.create(name)` records where every ref points (no
  copy is made; a name is taken once); `snapshot.rollback(name)` moves each recorded ref back, each
  move a history row, and leaves refs created since alone.
- **Ignore what git ignores.** `exec.run(..., gitignore=true)` keeps build outputs the project already
  declares in its `.gitignore` out of the result tree; `git.import(..., gitignore=true)` does the same
  for a clone. The fixed skip list (`target`, `node_modules`, `.venv`, caches) applies either way.
- **Caches for builds.** `exec.run(..., caches={"cargo": "target"})` mounts a persistent directory at
  `target` inside the work tree and keeps it out of the result tree. Caches change speed, not the key.
- **Force a run.** `fresh=true` runs again under a key that already has an answer; use it after an
  environmental failure.
- **Open the network** with `network=true`; in the `docker` and `container` sandboxes it is closed by
  default. In the `local` sandbox the flag only changes the key.
- **Import before you export** when the remote may have moved: `git.import(src, name, ref)` fetches
  and moves your ref to the remote's current tree by compare-and-swap.
- **Write your loop state back** with `seat.update(loop_state=...)` so the next session of your seat
  starts where this one stopped.

## Reading refusals

| text | meaning | do |
| --- | --- | --- |
| HTTP 401 | the bearer token matches no seat | get a token from whoever runs the computer; `kc seat rotate` replaces a leaked one |
| `Input should be 'create', 'merge' or 'discard'` | `action` is not one of the family's verbs | read the tool description; it lists every action with its signature |
| `branch.merge: missing a required argument: 'from_ref'; takes (...)` | the call does not fit the action's signature | the signature is in the message |
| `ref app: expected <a>, found <b>` | the ref moved since you read it | read `<b>`, redo your change on it, move again |
| `computer volume has 2.1 GB free, below the 3 GB floor` | the data disk is nearly full | ask the operator to prune caches |
| `cwd escapes the mount` or `path escapes the tree` | a path with `..` left the tree | use paths inside the tree |
| `ended_by: "timeout"` | the action outlived its timeout (default 600 s) and was killed | pass a longer `timeout`; the result was not cached |

## Looking at the computer

`computer.events(n)` is the last n calls on this computer, every one with its seat, verb
(`fs.edit`, `ref.move`), duration and outcome. `computer.metrics(window_s)` is calls, errors, bytes and
per-verb latency over a window. `ref.history(name)` is every move of one ref, newest first.
