"""Trees as values. A tree is an immutable, canonical list of entries stored as a blob;
its id is the content id of that serialization. A path is a view into one tree id.
Every write returns a NEW tree id and changes nothing that exists."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .blobs import Blobs

DEFAULT_IGNORE = (
    "target",
    "node_modules",
    ".venv",
    "__pycache__",
    ".git",
    ".DS_Store",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
)

# kinds: file | exec | symlink | dir


def gitignored(path: Path) -> set[str]:
    """Paths under `path` that git ignores: relative, posix, an ignored directory as `name/`.
    A directory that is a repository answers through its own git (tracked files stay whatever the
    ignore files say); any other directory, such as a tree laid out from the store, answers through a
    throwaway git dir. Only the directory's own ignore files count, never the server user's global
    excludes."""
    path = Path(path)
    tmp = None
    cmd = ["git"]
    if (path / ".git").exists():
        cmd += ["-C", str(path)]
    else:
        tmp = tempfile.mkdtemp(prefix="kc-gitignore-")
        subprocess.run(["git", "init", "-q", "--bare", tmp], check=True, capture_output=True)
        cmd += ["--git-dir", tmp, "--work-tree", str(path)]
    cmd += [
        "-c",
        "core.excludesFile=/dev/null",
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, check=False)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if p.returncode != 0:
        raise RuntimeError(f"git ls-files: {p.stderr.decode('utf-8', 'replace').strip()}")
    return {x for x in p.stdout.decode("utf-8", "surrogateescape").split("\0") if x}


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    cid: str  # blob id (file/exec/symlink target bytes) or tree id (dir)

    def to_json(self) -> list:
        return [self.name, self.kind, self.cid]


EMPTY_TREE_ENTRIES: tuple[Entry, ...] = ()


class Trees:
    def __init__(self, blobs: Blobs):
        self.blobs = blobs

    # ---- canonical object ----
    def put(self, entries: list[Entry] | tuple[Entry, ...]) -> str:
        seen = set()
        for e in entries:
            if e.name in seen or "/" in e.name or e.name in ("", ".", ".."):
                raise ValueError(f"bad entry name: {e.name!r}")
            seen.add(e.name)
        canon = sorted(entries, key=lambda e: e.name)
        payload = json.dumps(
            {"tree": [e.to_json() for e in canon]},
            separators=(",", ":"),
            sort_keys=True,
        )
        return self.blobs.put(payload.encode())

    def get(self, tree_id: str) -> list[Entry]:
        obj = json.loads(self.blobs.get(tree_id))
        if "tree" not in obj:
            raise ValueError(f"not a tree object: {tree_id}")
        return [Entry(n, k, c) for n, k, c in obj["tree"]]

    def empty(self) -> str:
        return self.put([])

    # ---- import from / export to a directory ----
    def from_dir(self, path: Path, ignore: tuple[str, ...] = DEFAULT_IGNORE, gitignore: bool = False) -> str:
        """Store a directory as a tree. `ignore` names are skipped at every level; with `gitignore`
        every path the directory's own .gitignore files ignore is skipped too (see `gitignored`)."""
        path = Path(path)
        skip = gitignored(path) if gitignore else frozenset()
        return self._from_dir(path, ignore, skip, "")

    def _from_dir(self, path: Path, ignore: tuple[str, ...], skip: frozenset[str] | set[str], rel: str) -> str:
        entries: list[Entry] = []
        for name in sorted(os.listdir(path)):
            if name in ignore:
                continue
            p = path / name
            r = rel + name
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                if r in skip:
                    continue
                entries.append(Entry(name, "symlink", self.blobs.put(os.readlink(p).encode())))
            elif stat.S_ISDIR(st.st_mode):
                if r + "/" in skip or r in skip:
                    continue
                entries.append(Entry(name, "dir", self._from_dir(p, ignore, skip, r + "/")))
            elif stat.S_ISREG(st.st_mode):
                if r in skip:
                    continue
                kind = "exec" if st.st_mode & stat.S_IXUSR else "file"
                entries.append(Entry(name, kind, self.blobs.put(p.read_bytes())))
            # sockets, fifos etc. are skipped
        return self.put(entries)

    def materialize(self, tree_id: str, dest: Path) -> int:
        """Lay the tree out under dest (created). Returns entry count."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        n = 0
        for e in self.get(tree_id):
            target = dest / e.name
            if e.kind == "dir":
                n += self.materialize(e.cid, target)
            elif e.kind == "symlink":
                os.symlink(self.blobs.get(e.cid).decode(), target)
            else:
                # shutil.copyfile clones on APFS when the platform supports it
                shutil.copyfile(self.blobs.path(e.cid), target)
                if e.kind == "exec":
                    os.chmod(target, 0o755)
                else:
                    os.chmod(target, 0o644)
            n += 1
        return n

    # ---- path views ----
    @staticmethod
    def _parts(path: str) -> list[str]:
        p = PurePosixPath(path.strip("/"))
        parts = [x for x in p.parts if x not in ("", ".")]
        if any(x == ".." for x in parts):
            raise ValueError("path escapes the tree")
        return parts

    def lookup(self, tree_id: str, path: str) -> Entry | None:
        parts = self._parts(path)
        if not parts:
            return Entry("", "dir", tree_id)
        cur = tree_id
        for i, part in enumerate(parts):
            found = None
            for e in self.get(cur):
                if e.name == part:
                    found = e
                    break
            if found is None:
                return None
            if i == len(parts) - 1:
                return found
            if found.kind != "dir":
                return None
            cur = found.cid
        return None

    def read_bytes(self, tree_id: str, path: str) -> bytes:
        e = self.lookup(tree_id, path)
        if e is None:
            raise FileNotFoundError(path)
        if e.kind == "dir":
            raise IsADirectoryError(path)
        return self.blobs.get(e.cid)

    def walk(self, tree_id: str, prefix: str = ""):
        """Yield (path, Entry) for every entry, depth-first, sorted."""
        for e in self.get(tree_id):
            p = f"{prefix}{e.name}"
            yield p, e
            if e.kind == "dir":
                yield from self.walk(e.cid, p + "/")

    def glob(self, tree_id: str, pattern: str) -> list[str]:
        pattern = pattern.strip("/")
        out = []
        for p, e in self.walk(tree_id):
            if e.kind == "dir":
                continue
            if fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], pattern):
                out.append(p)
        return out

    # ---- functional writes ----
    def with_entry(self, tree_id: str, path: str, entry: Entry | None) -> str:
        """Return a new tree id with `path` set to entry (or removed if None).
        Intermediate directories are created."""
        parts = self._parts(path)
        if not parts:
            raise ValueError("cannot replace the root")
        return self._with(tree_id, parts, entry)

    def _with(self, tree_id: str, parts: list[str], entry: Entry | None) -> str:
        entries = [e for e in self.get(tree_id) if e.name != parts[0]]
        existing = next((e for e in self.get(tree_id) if e.name == parts[0]), None)
        if len(parts) == 1:
            if entry is not None:
                entries.append(Entry(parts[0], entry.kind, entry.cid))
        else:
            child = existing.cid if existing and existing.kind == "dir" else self.empty()
            new_child = self._with(child, parts[1:], entry)
            if self.get(new_child) or entry is not None:
                entries.append(Entry(parts[0], "dir", new_child))
        return self.put(entries)

    def write(self, tree_id: str, path: str, data: bytes, exec: bool | None = None) -> str:
        prev = self.lookup(tree_id, path)
        kind = "exec" if (exec if exec is not None else (prev is not None and prev.kind == "exec")) else "file"
        return self.with_entry(tree_id, path, Entry("", kind, self.blobs.put(data)))

    def remove(self, tree_id: str, path: str) -> str:
        if self.lookup(tree_id, path) is None:
            raise FileNotFoundError(path)
        return self.with_entry(tree_id, path, None)

    # ---- diff ----
    def diff(self, a: str, b: str) -> list[dict]:
        """Paths that differ between two trees: added / removed / modified."""
        fa = {p: e for p, e in self.walk(a) if e.kind != "dir"}
        fb = {p: e for p, e in self.walk(b) if e.kind != "dir"}
        out = []
        for p in sorted(set(fa) | set(fb)):
            if p not in fa:
                out.append({"path": p, "change": "added"})
            elif p not in fb:
                out.append({"path": p, "change": "removed"})
            elif fa[p].cid != fb[p].cid or fa[p].kind != fb[p].kind:
                out.append({"path": p, "change": "modified"})
        return out
