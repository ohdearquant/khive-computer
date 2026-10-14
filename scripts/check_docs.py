"""Documents must match the code. Run by pre-commit and CI; exit 1 on the first class of defect found.

Checks: every `family.action` written in a document is a registered action and every registered
action is documented somewhere; every `KC_*` variable in the docs is read by the code and vice versa;
every `test_*` name cited in an ADR exists; every `[[ADR-...]]` wikilink resolves to a file; the
passages AGENTS.md and GUIDE.md quote from each other are present verbatim in the other file; no
retired tool name (`fs_read`, `ref_move`, ...) survives in a document."""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "GUIDE.md", ROOT / "AGENTS.md", *ROOT.glob("docs/**/*.md")]
FAMILY_RE = re.compile(r"\b(seat|fs|ref|branch|exec|git|snapshot|computer)\.([a-z_]+)\(")
RETIRED = re.compile(
    r"\b(fs_(read|list|glob|grep|write|edit|remove)|tree_(empty|diff)|ref_(list|get|create|move)|"
    r"branch_(create|merge|discard)|exec_(run|output)|git_(import|export|push|log)|"
    r"snapshot_(create|list|rollback)|seat_update|events_recent|verdict_\w+|git_pr_\w+|git_issue_\w+)\b"
)


def registered() -> dict[str, set[str]]:
    sys.path.insert(0, str(ROOT / "src"))
    from khive_computer.computer import Computer
    from khive_computer.server import build_server

    mcp = build_server(Computer(Path(tempfile.mkdtemp()) / "c"))

    async def go():
        return {t.name: set(t.parameters["properties"]["action"]["enum"]) for t in await mcp.list_tools()}

    return asyncio.run(go())


def main() -> int:
    problems: list[str] = []
    texts = {p: p.read_text() for p in DOCS if p.exists()}
    reg = registered()
    mentioned: set[tuple[str, str]] = set()
    for p, s in texts.items():
        for m in FAMILY_RE.finditer(s):
            mentioned.add((m.group(1), m.group(2)))
            if m.group(2) not in reg.get(m.group(1), set()):
                problems.append(f"{p.relative_to(ROOT)}: `{m.group(1)}.{m.group(2)}` is not a registered action")
        for m in RETIRED.finditer(s):
            if p.name == "ADR-0007-one-tool-per-family.md" or "ref_history" in m.group(0):
                continue
            problems.append(f"{p.relative_to(ROOT)}: retired name `{m.group(0)}`")
    for fam, acts in reg.items():
        for a in acts:
            if (fam, a) not in mentioned:
                problems.append(f"`{fam}.{a}` is registered but no document names it")

    src = "".join(p.read_text() for p in ROOT.glob("src/khive_computer/*.py"))
    env_code = set(re.findall(r"\bKC_[A-Z_]+\b", src))
    env_docs = {v for s in texts.values() for v in re.findall(r"\bKC_[A-Z_]+\b", s)}
    env_docs |= set(
        re.findall(r"\bKC_[A-Z_]+\b", (ROOT / "Dockerfile").read_text() + (ROOT / "compose.yaml").read_text())
    )
    for v in sorted(env_docs - env_code):
        problems.append(f"docs name `{v}` but no code reads it")
    for v in sorted(env_code - env_docs):
        problems.append(f"code reads `{v}` but no document names it")

    tests = "".join(p.read_text() for p in ROOT.glob("tests/*.py"))
    for p, s in texts.items():
        if "docs/adr" not in str(p):
            continue
        for name in set(re.findall(r"\btest_[a-z0-9_]+\b", s)):
            if name == "test_cache":  # a substring of `.pytest_cache`
                continue
            if not re.search(rf"^def {name}\(", tests, re.M):
                problems.append(f"{p.relative_to(ROOT)}: cites `{name}`, which is not a test")
        for link in set(re.findall(r"\[\[(ADR-\d{4}-[a-z0-9-]+)", s)):
            if not list(ROOT.glob(f"docs/adr/*/{link}.md")):
                problems.append(f"{p.relative_to(ROOT)}: wikilink `{link}` resolves to no file")

    for a, b in (("AGENTS.md", "GUIDE.md"), ("GUIDE.md", "AGENTS.md")):
        quoting, quoted = texts[ROOT / a], texts[ROOT / b]
        marker = f"> From `{b}`:"
        if marker not in quoting:
            problems.append(f"{a}: no `{marker}` block")
            continue
        for m in re.finditer(re.escape(marker) + r"\s*\n((?:> .*\n)+)", quoting):
            passage = " ".join(line[2:].strip() for line in m.group(1).splitlines()).strip().strip('"')
            if " ".join(quoted.split()).find(passage) < 0:
                problems.append(f"{a}: quotes a passage that is not in {b} verbatim: {passage[:60]!r}")

    for line in problems:
        print(line)
    print(f"check_docs: {len(problems)} problem(s)" if problems else "check_docs: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
