# khive computer

**Workspace for AI agents.** The model is the interface; khive is the computer.

[![PyPI](https://img.shields.io/pypi/v/khive-computer)](https://pypi.org/project/khive-computer/)
[![CI](https://github.com/ohdearquant/khive-computer/actions/workflows/ci.yml/badge.svg)](https://github.com/ohdearquant/khive-computer/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A khive computer is the workspace an agent works in: its identity, its working trees, its builds and
its history, in one place, behind eight MCP tools. Any MCP client drives it: Claude Code with its native
tools off, Codex, a local model, a script. The client holds a seat's token; every call is a tool call;
nothing on the client side needs to persist.

- **Nothing is overwritten.** Files live in content-addressed trees. A write returns a new tree id;
  what existed is still there. Two agents can work on the same tree and neither sees the other's edits
  until a ref moves.
- **Publishing is a compare-and-swap.** A ref is the only mutable name. Moving one names the tree you
  started from and refuses if someone else got there first; every move is a history row with who and why.
- **Commands run in a sandbox and come back as a tree.** `exec.run` takes a tree and an argv, returns
  the exit code, output tails and the result tree. The same inputs are answered from cache.
- **Git stays at the edge.** Import a repository into a ref, export a tree as a commit, push. The
  computer holds the credential; no seat ever sees it.
- **One token is one seat.** Every call is attributed to a seat; rotate a seat and its old token stops
  matching on the next request.

## Quick start

```sh
uv tool install khive-computer
kc up box > mcp.json          # a computer in Docker: container, first seat, MCP client configuration
```

`mcp.json` is an MCP server entry any client takes. For Claude Code with its own tools off:

```sh
claude -p --tools="" --strict-mcp-config --mcp-config mcp.json --allowedTools "mcp__kc__*" \
  "call the kc tool seat with action whoami, then fs with action empty, then write hello.py into that tree and run it"
```

Projects that need Rust or Node build their image first; everything else is the same:

```sh
kc build -n box --rust --node   # the image with those toolchains, and the first seat's token (printed once)
kc up box > mcp.json
```

Behind `kc up`: the published image runs as container `kc-box`, port `8810` on `127.0.0.1`, with the
computer in volume `kc-box-data`. The first seat's token is minted on your machine (or taken from
`--token` / `KC_SEAT_TOKEN`) and handed to the server through its environment; the server stores a
hash and the client configuration carries the token. `kc down box` stops it and keeps the volume,
`kc purge box` removes everything, `kc ps` lists what is on record.

## The eight tools

The verb is the tool's `action` argument, a closed set per tool; written here as `tool.action`.

| tool | actions | what it is |
| --- | --- | --- |
| `seat` | `whoami`, `update` | the seat this token is; its identity, system prompt and loop state |
| `fs` | `read`, `list`, `glob`, `grep`, `write`, `edit`, `remove`, `diff`, `empty` | files in a tree; reads take a tree id or a ref name, writes return a new tree id |
| `ref` | `list`, `get`, `create`, `move`, `history` | named pointers to trees; `move` is compare-and-swap, every move is history |
| `branch` | `create`, `merge`, `discard` | a ref with a remembered base; `merge` is three-way and moves nothing on conflict |
| `exec` | `run`, `output` | a command on a tree in a sandbox, cached by its inputs; stored output as numbered lines |
| `git` | `import`, `export`, `push`, `log` | a repository into a ref; a tree as a commit on a branch; push with the computer's token |
| `snapshot` | `create`, `list`, `rollback` | every ref's tree at a moment, by name; rollback moves each ref back by compare-and-swap |
| `computer` | `metrics`, `events` | the computer about itself: call metrics over a window, the last events |

Signatures, shapes and invariants: [`docs/SPEC.md`](docs/SPEC.md). How to drive it well, from the
model's side: [`GUIDE.md`](GUIDE.md).

## A first session

`seat.whoami` says which seat you are. `fs.empty` gives an empty tree; `fs.write` and `fs.edit` return a
new tree id and change nothing that exists. `exec.run` runs a command on a tree and returns the exit
code, output tails and the result tree. `ref.create` names a tree; `ref.move` publishes, naming the tree
you started from, and refuses if the ref moved under you. `git.import` brings a repository in as a ref;
`git.export` commits a tree on a branch of it; `git.push` pushes with the computer's token.

## Running it

### In Docker, from the `kc` command line

| command | what it does |
| --- | --- |
| `kc build -n NAME [--rust] [--node] [--apt PKG] [--token T]` | fix the deployment: its image, its first seat's token (given, or minted and printed) |
| `kc up NAME [--port P] [--host H]` | start it, wait until it answers, print the MCP client configuration |
| `kc seat create NAME2 --in NAME` | mint another seat inside it; `seat rotate` and `seat list` take `--in` too |
| `kc down NAME` | stop and remove the container; the volume, which is the computer, stays |
| `kc purge NAME` | also remove the volume, the deployment's image and its record |
| `kc ps`, `kc prune` | what is on record and whether it runs; remove leftovers of deployments no longer on record |

The record lives in `~/.kc/NAME.json` (`KC_HOME`), mode 0600; it is the one place the token is in the
clear. `compose.yaml` is the same deployment for `docker compose up -d`, with `KC_SEAT_TOKEN` in `.env`.

### Without Docker

```sh
git clone https://github.com/ohdearquant/khive-computer && cd khive-computer
uv sync
uv run kc --root ./computer init
uv run kc --root ./computer seat create demo > seat.token      # printed once; the computer stores a hash
uv run kc --root ./computer import https://github.com/you/repo demo-repo
uv run kc --root ./computer serve                              # http://127.0.0.1:8810/mcp
uv run kc mcp-config --token "$(cat seat.token)" > mcp.json   # --url for a computer somewhere else
```

`./computer` is the computer: blobs, the SQLite database, action slots, caches and clones. `KC_ROOT`
replaces `--root`. Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and `git` on the PATH.

### Anywhere else

A host needs four things from the computer, and the image provides them: an anonymous `GET /healthz`
for readiness, a drain the host's stop grace has to clear, the first seat's token taken from
`KC_SEAT_TOKEN`, and the data directory as the deployment's identity. Volumes, every environment
variable, HTTPS, backups and updates: [`docs/DEPLOY.md`](docs/DEPLOY.md).

### The GitHub token

The computer, not the model, holds the git credential. Create a fine-grained personal access token
(GitHub: Settings, Developer settings, Fine-grained tokens) with repository access limited to the
repositories the computer will work on, permissions Contents read and write and Metadata read, and
put it in the environment of the process that runs `kc serve` as `GITHUB_TOKEN` (`kc up` passes it
along from your shell). git reads it through an in-process credential helper: it never appears in a
command line, a URL, a log or the computer's store, and no seat can read it. Without one, local
repositories and public `https` clones work and `git.push` to GitHub fails with an authentication error.

Commits the computer makes carry the committer `KC_GIT_IDENTITY` (`Name <email>`, default
`khive computer <kc@localhost>`); give it a bot account your forge knows. The author is whatever the
seat passes on `git.export`, or the seat's name at `localhost` when it passes nothing.

## Documentation

- [`GUIDE.md`](GUIDE.md): using a computer, for the model or the person behind it.
- [`docs/SPEC.md`](docs/SPEC.md): the surface and its invariants.
- [`docs/adr/INDEX.md`](docs/adr/INDEX.md): the decisions, one record per family, each pinned to tests.
- [`docs/DEPLOY.md`](docs/DEPLOY.md): running it for others.
- [`AGENTS.md`](AGENTS.md): changing the computer itself.

## Layout

`src/khive_computer/`: `blobs.py` (content store), `trees.py` (trees as values), `db/` (the SQLite
file: `schema.sql`, how it is opened, and every statement, one module per table), `computer.py` (the
data directory, the connection and the lock; calls `db/`), `fs.py` (path views and functional edits),
`execer.py` (actions, sandboxes, caches), `gitio.py` (import, export, push), `merge.py` (three-way
merge), `server.py` (MCP tools and auth), `image.py` (`kc build`), `local.py` (deployments: `kc build -n`,
`up`, `down`, `purge`, `prune`, `ps`), `cli.py`. `tests/` pytest, including the image end to end.

## License

MIT.
