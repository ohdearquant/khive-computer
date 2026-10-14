"""Three-way merge of trees, file by file. Text conflicts are resolved with `git merge-file`;
a conflicted path is reported and the merge moves nothing."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .computer import Computer
from .trees import Entry


def three_way(c: Computer, base: str, ours: str, theirs: str) -> dict:
    t = c.trees
    fb = {p: e for p, e in t.walk(base) if e.kind != "dir"}
    fo = {p: e for p, e in t.walk(ours) if e.kind != "dir"}
    ft = {p: e for p, e in t.walk(theirs) if e.kind != "dir"}
    result = ours
    conflicts: list[str] = []
    applied: list[dict] = []
    for p in sorted(set(fb) | set(fo) | set(ft)):
        b, o, th = fb.get(p), fo.get(p), ft.get(p)
        if o == th:
            continue  # same on both sides (incl. both absent)
        if th == b:
            continue  # theirs unchanged; ours wins
        if o == b:
            # ours unchanged; take theirs (modify, add or remove)
            result = t.with_entry(result, p, Entry("", th.kind, th.cid) if th else None)
            applied.append({"path": p, "change": "theirs"})
            continue
        # both changed
        if o is None or th is None:
            conflicts.append(p)
            continue
        merged = _merge_text(c, b.cid if b else None, o.cid, th.cid)
        if merged is None:
            conflicts.append(p)
            continue
        result = t.with_entry(result, p, Entry("", o.kind, c.blobs.put(merged)))
        applied.append({"path": p, "change": "merged"})
    return {
        "base": base,
        "ours": ours,
        "theirs": theirs,
        "tree": result,
        "applied": applied,
        "conflicts": conflicts,
    }


def _merge_text(c: Computer, base: str | None, ours: str, theirs: str) -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "base").write_bytes(c.blobs.get(base) if base else b"")
        (d / "ours").write_bytes(c.blobs.get(ours))
        (d / "theirs").write_bytes(c.blobs.get(theirs))
        p = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                str(d / "ours"),
                str(d / "base"),
                str(d / "theirs"),
            ],
            capture_output=True,
            check=False,
        )
        if p.returncode == 0:
            return p.stdout
        return None  # >0 = number of conflicts, <0 = error
