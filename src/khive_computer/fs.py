"""Path views over a tree id, in the shapes a model already knows: a numbered read with
offset and limit, an edit that matches one exact old string, glob, grep. Writes return a new
tree id; the input tree is untouched."""

from __future__ import annotations

import re

from .computer import Computer

_BINARY_SNIFF = 8000


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def read(c: Computer, ref_or_tree: str, path: str, offset: int = 1, limit: int = 200) -> dict:
    tree = c.resolve_tree(ref_or_tree)
    data = c.trees.read_bytes(tree, path)
    if _is_binary(data):
        return {"tree": tree, "path": path, "binary": True, "size": len(data)}
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    offset = max(1, offset)
    chunk = lines[offset - 1 : offset - 1 + max(1, limit)]
    numbered = "\n".join(f"{offset + i:6d}\t{line}" for i, line in enumerate(chunk))
    return {
        "tree": tree,
        "path": path,
        "offset": offset,
        "returned": len(chunk),
        "total_lines": len(lines),
        "content": numbered,
    }


def list_dir(c: Computer, ref_or_tree: str, path: str = "") -> dict:
    tree = c.resolve_tree(ref_or_tree)
    e = c.trees.lookup(tree, path)
    if e is None:
        raise FileNotFoundError(path)
    if e.kind != "dir":
        return {
            "tree": tree,
            "path": path,
            "entries": [{"name": e.name, "kind": e.kind, "size": c.blobs.size(e.cid)}],
        }
    entries = []
    for child in c.trees.get(e.cid):
        item = {"name": child.name, "kind": child.kind}
        if child.kind != "dir":
            item["size"] = c.blobs.size(child.cid)
        entries.append(item)
    return {"tree": tree, "path": path, "entries": entries}


def glob(c: Computer, ref_or_tree: str, pattern: str, limit: int = 500) -> dict:
    tree = c.resolve_tree(ref_or_tree)
    hits = c.trees.glob(tree, pattern)
    return {"tree": tree, "pattern": pattern, "count": len(hits), "paths": hits[:limit]}


def grep(
    c: Computer,
    ref_or_tree: str,
    pattern: str,
    path: str = "",
    limit: int = 200,
    ignore_case: bool = False,
) -> dict:
    tree = c.resolve_tree(ref_or_tree)
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    start = c.trees.lookup(tree, path) if path else None
    if path and start is None:
        raise FileNotFoundError(path)
    hits = []
    prefix = path.strip("/") + "/" if path.strip("/") else ""
    if start is not None and start.kind != "dir":
        items = [(path.strip("/"), start)]
    else:
        root = start.cid if start is not None else tree
        items = [(prefix + p, e) for p, e in c.trees.walk(root)]
    for p, e in items:
        if e.kind == "dir":
            continue
        data = c.blobs.get(e.cid)
        if _is_binary(data):
            continue
        for i, line in enumerate(data.decode("utf-8", errors="replace").split("\n"), 1):
            if rx.search(line):
                hits.append({"path": p, "line": i, "text": line[:400]})
                if len(hits) >= limit:
                    return {
                        "tree": tree,
                        "pattern": pattern,
                        "truncated": True,
                        "hits": hits,
                    }
    return {"tree": tree, "pattern": pattern, "truncated": False, "hits": hits}


def write(c: Computer, tree: str, path: str, content: str, executable: bool | None = None) -> dict:
    tree = c.resolve_tree(tree)
    new = c.trees.write(tree, path, content.encode(), exec=executable)
    return {"tree": new, "base": tree, "path": path, "bytes": len(content.encode())}


def edit(c: Computer, tree: str, path: str, old: str, new: str, replace_all: bool = False) -> dict:
    tree = c.resolve_tree(tree)
    data = c.trees.read_bytes(tree, path)
    if _is_binary(data):
        raise ValueError(f"binary file: {path}")
    text = data.decode("utf-8")
    n = text.count(old)
    if n == 0:
        raise ValueError(f"old string not found in {path}")
    if n > 1 and not replace_all:
        raise ValueError(f"old string occurs {n} times in {path}; pass replace_all or make it unique")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    new_tree = c.trees.write(tree, path, updated.encode())
    return {
        "tree": new_tree,
        "base": tree,
        "path": path,
        "replacements": n if replace_all else 1,
    }


def remove(c: Computer, tree: str, path: str) -> dict:
    tree = c.resolve_tree(tree)
    return {"tree": c.trees.remove(tree, path), "base": tree, "path": path}


def diff(c: Computer, a: str, b: str) -> dict:
    ta, tb = c.resolve_tree(a), c.resolve_tree(b)
    changes = c.trees.diff(ta, tb)
    return {"a": ta, "b": tb, "count": len(changes), "changes": changes}
