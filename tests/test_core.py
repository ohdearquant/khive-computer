import getpass
import os
import pwd
import subprocess

import pytest

from khive_computer import execer, fs, gitio, image, local
from khive_computer.computer import Computer, RefConflict
from khive_computer.merge import three_way


@pytest.fixture
def c(tmp_path):
    return Computer(tmp_path / "computer")


def seed(c: Computer) -> str:
    t = c.trees.empty()
    t = c.trees.write(t, "README.md", b"hello\nworld\n")
    t = c.trees.write(t, "src/app.py", b"print('one')\n")
    t = c.trees.write(t, "bin/run.sh", b"#!/bin/sh\necho ran\n", exec=True)
    return t


def test_tree_is_a_value_and_writes_are_functional(c):
    t1 = seed(c)
    t2 = c.trees.write(t1, "src/app.py", b"print('two')\n")
    assert t1 != t2
    assert c.trees.read_bytes(t1, "src/app.py") == b"print('one')\n"
    assert c.trees.read_bytes(t2, "src/app.py") == b"print('two')\n"
    # identical content stored twice is one object
    assert c.trees.write(t1, "src/app.py", b"print('two')\n") == t2
    assert [d["path"] for d in c.trees.diff(t1, t2)] == ["src/app.py"]
    t3 = c.trees.remove(t2, "src/app.py")
    assert c.trees.lookup(t3, "src") is None  # empty dir pruned
    assert c.trees.lookup(t3, "README.md") is not None


def test_fs_views(c):
    t = seed(c)
    r = fs.read(c, t, "README.md", offset=2, limit=5)
    assert r["content"] == "     2\tworld" and r["total_lines"] == 2
    assert [e["name"] for e in fs.list_dir(c, t, "")["entries"]] == [
        "README.md",
        "bin",
        "src",
    ]
    assert fs.glob(c, t, "*.py")["paths"] == ["src/app.py"]
    assert fs.grep(c, t, "one")["hits"][0]["path"] == "src/app.py"
    e = fs.edit(c, t, "src/app.py", "one", "three")
    assert c.trees.read_bytes(e["tree"], "src/app.py") == b"print('three')\n"
    with pytest.raises(ValueError):
        fs.edit(c, t, "README.md", "nope", "x")


def test_ref_cas_and_history(c):
    t1 = seed(c)
    t2 = c.trees.write(t1, "a", b"1")
    c.ref_create("main", t1, "alice")
    c.ref_move("main", t1, t2, "alice", "first")
    with pytest.raises(RefConflict) as ei:
        c.ref_move("main", t1, t2, "bob")
    assert ei.value.found == t2
    assert c.ref_get("main")["rev"] == 1
    assert [h["rev"] for h in c.ref_history("main")] == [1, 0]
    assert c.resolve_tree("main") == t2


def test_snapshot_rollback(c):
    t1 = seed(c)
    c.ref_create("main", t1, "a")
    c.snapshot_create("before", "a")
    t2 = c.trees.write(t1, "README.md", b"broken")
    c.ref_move("main", t1, t2, "a")
    c.ref_create("scratch", t2, "a")  # born after the snapshot: rollback leaves it alone
    out = c.snapshot_rollback("before", "a")
    assert out["moved"] == ["main"] and c.ref_get("main")["tree"] == t1
    assert c.ref_get("main")["rev"] == 2  # rollback is a move, not an erasure
    assert c.ref_get("scratch")["tree"] == t2
    with pytest.raises(ValueError, match="snapshot exists"):
        c.snapshot_create("before", "a")  # a bookmark is never silently moved
    c.ref_delete("scratch")
    c.snapshot_create("with-scratch", "a")


def test_spawn_failures_are_results_not_errors(c):
    t = c.trees.write(seed(c), "notexec.sh", b"#!/bin/sh\necho never\n")  # no exec bit
    missing = execer.attach_tails(c, execer.run(c, t, ["/no/such/binary"], actor="a"))
    assert (missing["ended_by"], missing["exit_code"]) == ("spawn_failed", 127)
    denied = execer.attach_tails(c, execer.run(c, t, ["./notexec.sh"], actor="a"))
    assert (denied["ended_by"], denied["exit_code"]) == ("spawn_failed", 126)
    assert not missing["cached"] and not denied["cached"]
    assert "/root/" not in execer.DEFAULT_PATH or os.path.isdir("/root/.cargo/bin")  # nothing on PATH on a guess


def test_exec_runs_in_a_slot_and_captures_the_tree(c):
    t = seed(c)
    r = execer.run(
        c,
        t,
        ["sh", "-c", "echo out; echo err >&2; echo made > made.txt; cat README.md"],
        actor="a",
    )
    r = execer.attach_tails(c, r)
    assert r["exit_code"] == 0 and "out" in r["stdout_tail"] and "err" in r["stderr_tail"]
    assert r["changed"] == [{"path": "made.txt", "change": "added"}]
    assert c.trees.read_bytes(r["tree_out"], "made.txt") == b"made\n"
    assert not any(c.slots.iterdir())  # slot removed
    r2 = execer.run(
        c,
        t,
        ["sh", "-c", "echo out; echo err >&2; echo made > made.txt; cat README.md"],
        actor="a",
    )
    assert r2["cached"] and r2["tree_out"] == r["tree_out"]
    r3 = execer.run(c, t, ["bin/run.sh"], actor="a")
    assert r3["exit_code"] == 0
    r4 = execer.run(c, t, ["sh", "-c", "sleep 5"], timeout=1, actor="a")
    assert r4["ended_by"] == "timeout" and c.action_get(r4["key"]) is None
    r5 = execer.run(
        c,
        t,
        ["sh", "-c", "echo $HOME; env | grep -c SECRET || true"],
        env={"X": "1"},
        actor="a",
    )
    r5 = execer.attach_tails(c, r5)
    assert "slots" in r5["stdout_tail"]


def test_git_import_export(c, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "main"], check=True)
    (src / "f.txt").write_text("v1\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(src),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "add",
            "f.txt",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(src),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "v1",
        ],
        check=True,
    )
    imp = gitio.import_repo(c, str(src), "demo", actor="a")
    assert c.trees.read_bytes(imp["tree"], "f.txt") == b"v1\n"
    t2 = c.trees.write(imp["tree"], "f.txt", b"v2\n")
    ex = gitio.export(c, "demo", t2, "feature", "bump to v2", actor="a")
    assert ex["changed"] == 1
    log = gitio.log(c, "demo")
    assert log["branch"] == "feature" and "bump to v2" in log["log"][0]
    # re-import at the new branch yields the exported tree
    imp2 = gitio.import_repo(c, str(src), "demo", ref="feature", actor="a")
    assert imp2["tree"] == t2


def test_three_way_merge(c):
    base = seed(c)
    ours = c.trees.write(base, "README.md", b"hello\nworld\nours\n")
    theirs = c.trees.write(base, "src/app.py", b"print('theirs')\n")
    m = three_way(c, base, ours, theirs)
    assert m["conflicts"] == []
    assert c.trees.read_bytes(m["tree"], "README.md") == b"hello\nworld\nours\n"
    assert c.trees.read_bytes(m["tree"], "src/app.py") == b"print('theirs')\n"
    conflict = c.trees.write(base, "README.md", b"hello\nworld\ntheirs\n")
    m2 = three_way(c, base, ours, conflict)
    assert m2["conflicts"] == ["README.md"]


def test_git_env_and_slug(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert "GIT_CONFIG_COUNT" not in gitio.git_env()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    env = gitio.git_env()
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper" and "ghp_x" not in env["GIT_CONFIG_VALUE_0"]
    assert gitio.github_slug("https://github.com/o/r.git") == "o/r"
    assert gitio.github_slug("git@github.com:o/r") == "o/r"
    with pytest.raises(ValueError):
        gitio.github_slug("https://gitlab.com/o/r")


def test_caches_persist_across_actions_and_stay_out_of_the_tree(c):
    t = seed(c)
    r1 = execer.run(c, t, ["sh", "-c", "echo warm > cache/x; ls"], caches={"demo": "cache"}, actor="a", fresh=True)
    r1 = execer.attach_tails(c, r1)
    assert r1["exit_code"] == 0 and (c.root / "caches" / "demo" / "x").read_text() == "warm\n"
    assert c.trees.lookup(r1["tree_out"], "cache") is None  # mount point not captured
    r2 = execer.run(c, t, ["sh", "-c", "cat cache/x"], caches={"demo": "cache"}, actor="a", fresh=True)
    r2 = execer.attach_tails(c, r2)
    assert r2["stdout_tail"] == "warm\n"  # second action sees the first one's cache
    with pytest.raises(ValueError):
        execer.run(c, t, ["true"], caches={"demo": "/abs"}, actor="a")
    assert execer.action_key(t, ["x"], ".", {}, "local", None, True) != execer.action_key(
        t, ["x"], ".", {}, "local", None
    )


def test_export_author_shape():
    assert gitio.check_author("Ada Example <ada@example.com>") == "Ada Example <ada@example.com>"
    with pytest.raises(ValueError):
        gitio.check_author("nobody")
    with pytest.raises(ValueError):
        gitio.check_author("A <b@c> <d@e>")


def test_import_fresh_clone_at_branch(tmp_path):
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
    }

    def run(*a):
        return subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True, env=env)

    run("init", "--quiet", "-b", "main")
    (src / "a.txt").write_text("main\n")
    run("add", "a.txt")
    run("commit", "--quiet", "-m", "one")
    run("checkout", "--quiet", "-b", "feat")
    (src / "a.txt").write_text("feat\n")
    run("commit", "--quiet", "-am", "two")
    feat = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "feat"], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()
    run("checkout", "--quiet", "main")
    c = Computer(tmp_path / "computer")
    r = gitio.import_repo(c, str(src), "repo-feat", ref="feat")
    assert r["commit"] == feat
    # a second import of the same clone follows the remote branch
    r2 = gitio.import_repo(c, str(src), "repo-feat", ref="feat")
    assert r2["commit"] == feat


def test_gitignore_option_drops_ignored_paths(c, tmp_path):
    d = tmp_path / "proj"
    for sub in ("src", "dist", "sub"):
        (d / sub).mkdir(parents=True)
    (d / ".gitignore").write_text("dist/\n*.log\n.env\n")
    (d / "sub" / ".gitignore").write_text("keep.tmp\n")
    for f in ("src/a.py", "dist/b.js", "run.log", ".env", "sub/keep.tmp", "sub/ok.txt"):
        (d / f).write_text("x\n")

    def files(t):
        return sorted(p for p, e in c.trees.walk(t) if e.kind != "dir")

    t_all = c.trees.from_dir(d)
    assert "dist/b.js" in files(t_all) and ".env" in files(t_all) and "sub/keep.tmp" in files(t_all)
    t_gi = c.trees.from_dir(d, gitignore=True)
    assert files(t_gi) == [".gitignore", "src/a.py", "sub/.gitignore", "sub/ok.txt"]
    # through an action: a build output the project declares ignored never enters the result tree
    argv = ["sh", "-c", "mkdir -p dist && echo built > dist/out.js && echo kept > note.txt"]
    r = execer.run(c, t_gi, argv, actor="a", gitignore=True)
    assert [x["path"] for x in r["changed"]] == ["note.txt"]
    r2 = execer.run(c, t_gi, argv, actor="a")  # a different key: the option is part of the action
    assert not r2["cached"] and "dist/out.js" in [x["path"] for x in r2["changed"]]


def test_actions_run_as_the_configured_user(c, monkeypatch):
    me = getpass.getuser()
    monkeypatch.setenv("KC_ACTION_USER", me)
    assert execer.action_user() == (pwd.getpwnam(me).pw_uid, pwd.getpwnam(me).pw_gid)
    info = execer.prepare_action_user(c)
    assert info["uid"] == os.getuid() and (c.root / "caches" / ".owner").read_text() == str(os.getuid())
    assert execer.prepare_action_user(c)["chowned"] == []  # the marker short-circuits the walk
    r = execer.attach_tails(c, execer.run(c, seed(c), ["id", "-u"], actor="a", caches={"k": "cache"}))
    assert r["exit_code"] == 0 and r["stdout_tail"].strip() == str(os.getuid())
    assert (c.root / "caches" / "k").stat().st_uid == os.getuid()
    monkeypatch.delenv("KC_ACTION_USER")
    assert execer.action_user() is None


def test_actions_refuse_when_the_volume_is_below_the_free_floor(c, monkeypatch):
    t = seed(c)
    monkeypatch.setenv("KC_MIN_FREE_GB", "99999")  # no disk is this empty
    with pytest.raises(RuntimeError, match="below the 99999 GB floor"):
        execer.run(c, t, ["sh", "-c", "true"], actor="a")
    assert not any(c.slots.iterdir())  # refused before a slot was cut
    monkeypatch.setenv("KC_MIN_FREE_GB", "0")
    assert execer.run(c, t, ["sh", "-c", "true"], actor="a")["exit_code"] == 0


def test_kc_build_generates_the_toolchain_dockerfile():
    plain = image.dockerfile()
    assert plain.splitlines()[1:] == [f"FROM {image.DEFAULT_BASE}", "USER root"]
    full = image.dockerfile(
        base="khive-computer:dev", rust=True, node=True, apt=["libssl-dev", "build-essential", "libssl-dev"]
    )
    assert full.startswith("# generated by `kc build`") and "FROM khive-computer:dev\n" in full
    assert "install -y --no-install-recommends build-essential libssl-dev" in full  # sorted, deduplicated
    assert "deb.nodesource.com/setup_22.x" in full and "nodejs" in full
    assert (
        "ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo" in full and "chown -R kc:kc /opt/rustup /opt/cargo" in full
    )
    assert 'ENV PATH="/opt/cargo/bin:$PATH"' in full
    assert "WITH_" not in full and "ARG " not in full


def test_kc_up_docker_run_passes_the_token_by_name_only():
    argv = local.run_argv("box", "img:1", 8810, "127.0.0.1", ["KC_SEAT_TOKEN", "KC_SEAT_NAME", "GITHUB_TOKEN"])
    assert argv[:2] == ["docker", "run"] and argv[-1] == "img:1"
    assert "kc-box" in argv and "127.0.0.1:8810:8810" in argv and "kc-box-data:/data" in argv
    assert argv[argv.index("-e") + 1] == "KC_SEAT_TOKEN"  # the name; docker reads the value from the environment
    assert not any("=" in a or a.startswith("kc_") for a in argv)


def test_kc_build_records_the_deployment_and_its_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KC_HOME", str(tmp_path / "kc"))
    monkeypatch.delenv("KC_SEAT_TOKEN", raising=False)
    rec = local.build("box")  # no toolchains: no docker call, the published image
    assert rec["image"] == image.DEFAULT_BASE and rec["seat"] == "operator"
    assert rec["token"].startswith("kc_") and len(rec["token"]) == 51
    assert (tmp_path / "kc" / "box.json").stat().st_mode & 0o777 == 0o600
    assert local.build("box")["token"] == rec["token"]  # on record: kept
    assert local.build("box", token="kc_given")["token"] == "kc_given"  # given: used
    monkeypatch.setenv("KC_SEAT_TOKEN", "kc_from_env")
    assert local.build("box")["token"] == "kc_from_env"  # the environment beats the record
    assert local.build("box", token="kc_flag")["token"] == "kc_flag"  # the flag beats the environment
    assert local.load("nothing") is None


def test_seat_ensure_bootstraps_and_rekeys(tmp_path):
    c = Computer(tmp_path / "c")
    c.seat_ensure("operator", "kc_first")
    assert c.seat_by_token("kc_first")["name"] == "operator"
    created = c.seat_get("operator")["created_at"]
    c.seat_ensure("operator", "kc_second")  # the same seat, re-keyed
    assert c.seat_by_token("kc_first") is None and c.seat_by_token("kc_second")["name"] == "operator"
    assert c.seat_get("operator")["created_at"] == created and len(c.seat_list()) == 1
