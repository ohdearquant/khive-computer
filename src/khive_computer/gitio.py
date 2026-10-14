"""git is an import/export adapter over trees. The computer keeps one clone per repository
under repos/<name>; import reads a commit's tree into the blob store and binds a ref to it;
export writes a tree back as a commit on a branch; push uses the computer's own credentials,
never the client's."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .computer import Computer

HELPER_VALUE = '!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
DEFAULT_IDENTITY = "khive computer <kc@localhost>"
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/"),
}


def git_env() -> dict[str, str]:
    """Environment for every git subprocess. With GITHUB_TOKEN set, git authenticates to GitHub as the
    computer through an in-process credential helper; the token is read from the environment by the
    helper and never appears in argv, a URL or the store."""
    env = {**os.environ, **GIT_ENV}
    if os.environ.get("GITHUB_TOKEN"):
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": HELPER_VALUE,
            }
        )
    return env


def github_slug(src: str) -> str:
    """owner/repo from an https or ssh GitHub URL."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", src.strip())
    if not m:
        raise ValueError(f"not a GitHub repository URL: {src}")
    return m.group(1)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=git_env(),
        check=False,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def import_repo(
    c: Computer, src: str, name: str, ref: str = "HEAD", actor: str | None = None, gitignore: bool = False
) -> dict:
    """Clone (or reuse) src under repos/<name>, check out `ref`, store its tree, bind ref <name>."""
    repo = c.repos_dir / name
    if not (repo / ".git").exists():
        if repo.exists():
            shutil.rmtree(repo)
        p = subprocess.run(
            ["git", "clone", "--quiet", src, str(repo)],
            capture_output=True,
            text=True,
            env=git_env(),
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError(f"git clone: {p.stderr.strip()}")
        c.repo_put(name, repo, src)
    else:
        # an existing clone follows the remote
        _git(repo, "fetch", "--quiet", "--prune", "origin")
    # origin/<ref> wins over a stale local branch, and a bare branch name is never handed to checkout:
    # git would guess a new local branch from it, which --detach refuses
    target = "origin/HEAD" if ref == "HEAD" else ref
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{ref}", check=False).strip():
        target = f"origin/{ref}"
    if target != "HEAD":
        _git(repo, "checkout", "--quiet", "--detach", target)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    t0 = time.time()
    tree = c.trees.from_dir(repo, gitignore=gitignore)
    ms = int((time.time() - t0) * 1000)
    try:
        c.ref_get(name)
        existing = c.ref_get(name)
        if existing["tree"] != tree:
            c.ref_move(name, existing["tree"], tree, actor, note=f"import {sha[:12]}")
    except KeyError:
        c.ref_create(name, tree, actor, note=f"import {sha[:12]}")
    return {"ref": name, "tree": tree, "commit": sha, "import_ms": ms}


def check_author(author: str) -> str:
    """`Name <email>`, one angle pair, no second address."""
    if not re.fullmatch(r"[^<>]+ <[^<>@\s]+@[^<>@\s]+>", author):
        raise ValueError(f"author must be 'Name <email>', got {author!r}")
    return author


def export(
    c: Computer,
    name: str,
    ref_or_tree: str,
    branch: str,
    message: str,
    actor: str | None = None,
    start_point: str | None = None,
    author: str | None = None,
) -> dict:
    """Materialize a tree over the clone's working copy and commit it on `branch`. `author` is the
    commit author as `Name <email>`; when none is given the seat that asked is named with a localhost
    address. The committer is the computer's identity, `KC_GIT_IDENTITY` (`Name <email>`, default
    `khive computer <kc@localhost>`); set it to an identity your forge knows, such as a bot account's
    noreply address. The seat that asked is the actor in the event log either way."""
    tree = c.resolve_tree(ref_or_tree)
    repo = Path(c.repo_get(name)["path"])
    # a branch that exists continues; otherwise it starts from start_point or HEAD
    exists = (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        _git(repo, "checkout", "--quiet", branch)
    else:
        _git(repo, "checkout", "--quiet", "-b", branch, start_point or "HEAD")
    # replace the working copy with the tree (keep .git)
    for entry in os.listdir(repo):
        if entry == ".git":
            continue
        p = repo / entry
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    c.trees.materialize(tree, repo)
    _git(repo, "add", "--all", "--", ".")
    status = _git(repo, "status", "--porcelain").strip()
    if not status:
        return {
            "repo": name,
            "branch": branch,
            "commit": _git(repo, "rev-parse", "HEAD").strip(),
            "changed": 0,
            "note": "nothing to commit",
        }
    author = check_author(author or f"{actor or 'kc'} <{actor or 'kc'}@localhost>")
    committer = check_author(os.environ.get("KC_GIT_IDENTITY") or DEFAULT_IDENTITY)
    cname, cemail = committer[: committer.rindex(" <")], committer[committer.rindex(" <") + 2 : -1]
    _git(
        repo,
        "-c",
        f"user.name={cname}",
        "-c",
        f"user.email={cemail}",
        "commit",
        "--quiet",
        "--author",
        author,
        "-m",
        message,
    )
    sha = _git(repo, "rev-parse", "HEAD").strip()
    return {
        "repo": name,
        "branch": branch,
        "commit": sha,
        "changed": len(status.splitlines()),
        "tree": tree,
    }


def push(c: Computer, name: str, branch: str, remote: str = "origin") -> dict:
    repo = Path(c.repo_get(name)["path"])
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "push",
            "--porcelain",
            "-u",
            remote,
            f"{branch}:{branch}",
        ],
        capture_output=True,
        text=True,
        env=git_env(),
        check=False,
    )
    return {
        "repo": name,
        "branch": branch,
        "remote": remote,
        "ok": out.returncode == 0,
        "output": (out.stdout + out.stderr).strip()[-2000:],
    }


def log(c: Computer, name: str, n: int = 10) -> dict:
    repo = Path(c.repo_get(name)["path"])
    out = _git(repo, "log", f"-{n}", "--format=%H %an %ad %s", "--date=short")
    return {
        "repo": name,
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "log": out.strip().splitlines(),
    }
