"""Fetching the data, and checking that the local data is the data the results were computed from.

  python -m fixprop fetch            fetch the 40 upstream repositories of data/repos.toml into data/repos/<owner>/<repo>/.
                                     Needs network, about 750 MB in total. Repositories already present are left alone.
                                     Also fetches every file version the analysis needs, so recomputing and checking
                                     work offline afterwards.
  python -m fixprop fetch --verify   check only: offline, changes nothing

Each upstream repository consists of two things: the files at the pinned commit (visible in the directory) and the
history since 2023-06-01 (in its .git). The three large repositories check out only the files the analysis reads:
arch/ in NuttX, drivers/ in Zephyr, and the drv_*.c files under bsp/ in RT-Thread.

The directories are registered as git submodules in this project, so GitHub shows each upstream repository and its
pinned commit. Fetch the data with the commands above, not with `git submodule update`: that fetches the whole history
and every file of each repository, several GB, with a different history range, and the check fails. .gitmodules sets
update = none, so `git clone --recursive` skips them.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

from . import gitio, paths
from .codebases import get, rtos_repos, rust_repos
from .common import read_json, write_json

# only files matching these paths are checked out; repositories not listed get the whole tree
SPARSE = {
    "apache/nuttx": ["/arch/"],
    "zephyrproject-rtos/zephyr": ["/drivers/"],
    "RT-Thread/rt-thread": ["/bsp/**/drv_*.c"],
}
# the directory of the pinned commit under which the check lists files
TREE_ROOT = {"apache/nuttx": "arch", "zephyrproject-rtos/zephyr": "drivers", "RT-Thread/rt-thread": "bsp"}
CODEBASE_OF = {"apache/nuttx": "nuttx", "zephyrproject-rtos/zephyr": "zephyr", "RT-Thread/rt-thread": "rtthread"}


def todo_list(which: str | None = None) -> list[tuple[str, str, str]]:
    todo = [(r, c, "rust") for r, c in sorted(rust_repos().items())] + \
           [(r, c, "rtos") for r, c in rtos_repos().items()]
    if which:
        todo = [t for t in todo if t[0] == which or t[2] == which]
    return todo


def _lock() -> dict:
    return read_json(paths.LOCK) if paths.LOCK.exists() else {}


def fingerprint(repo: str, commit: str) -> dict:
    cut, n = gitio.window(gitio.git_dir(repo), commit)
    return {"commit": commit, "cut_at": cut, "commits": n}


def _mark_submodule(repo: str) -> None:
    """When this project is a git repository, mark the submodule as in use (changes only this project's .git/config;
    no network)."""
    if (paths.ROOT / ".git").exists():
        gitio.run(None, "-C", str(paths.ROOT), "submodule", "init", "--", str(paths.repo_dir(repo).relative_to(paths.ROOT)),
                  check=False)


def run(which: str | None = None, prefetch: bool = True) -> int:
    lock = _lock()
    rc = 0
    for repo, commit, _kind in todo_list(which):
        try:
            mode = gitio.clone(repo, commit, SPARSE.get(repo))
            fp = fingerprint(repo, commit)
        except Exception as e:  # noqa: BLE001 - one failing repository does not stop the others
            print(f"[fetch] {repo}: failed: {str(e)[:300]}", file=sys.stderr, flush=True)
            rc = 1
            continue
        _mark_submodule(repo)
        if repo not in lock:
            lock[repo] = fp
            write_json(paths.LOCK, dict(sorted(lock.items())))
        note = "already present" if mode == "kept" else f"fetched ({mode})"
        print(f"[fetch] {repo}: {note}, {fp['commits']} non-merge commits", flush=True)
    problems = verify(which)
    if problems:
        print(f"[fetch] the fetched data differs from the record in {len(problems)} places:")
        for p in problems:
            print("   ", p)
        return 1
    if rc == 0 and prefetch and which is None:
        rc = _prefetch()
        if rc == 0:
            print("[fetch] The data is complete. `python -m fixprop check` and `all` now work offline.", flush=True)
    return rc


def _prefetch() -> int:
    """Fetch every file content the analysis needs: run the pipeline into a temporary directory and fetch each file
    version as it is used."""
    from . import pipeline

    print("[fetch] Fetching the file versions the analysis needs (runs the pipeline once, a few minutes) ...", flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="fixprop-prefetch."))
    out, paths.OUT = paths.OUT, tmp / "benchmark"
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            bad = pipeline.run_steps()
    except Exception as e:  # noqa: BLE001
        print(log.getvalue()[-2000:])
        print(f"[fetch] did not finish: {str(e)[:400]}", file=sys.stderr)
        return 1
    finally:
        paths.OUT = out
        shutil.rmtree(tmp, ignore_errors=True)
    if bad:
        print(log.getvalue()[-2000:])
        print(f"[fetch] the pipeline finished with {bad} problems, see above.", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------ checking


def git_blob_id(data: bytes) -> str:
    """The object ID git computes for a file content."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _gitlinks() -> dict[str, str] | None:
    """The submodules registered in this project: path -> commit. None when this project is not a git repository."""
    if not (paths.ROOT / ".git").exists():
        return None
    out = gitio.text(None, "-C", str(paths.ROOT), "ls-files", "--stage", "--", "data/repos", check=False)
    links = {}
    for ln in out.split("\n"):
        meta, _, path = ln.partition("\t")
        m = meta.split()
        if len(m) == 3 and m[0] == "160000":
            links[path] = m[1]
    return links


def verify(which: str | None = None) -> list[str]:
    """Check the local data and return the problems found (an empty list means none). Offline; changes nothing.

    Four things per repository: the pinned commit exists and is the one checked out; the submodule commit registered in
    this project matches data/repos.toml; where the history is cut off and how many commits it has match
    data/repos.lock.json; every source file the analysis reads is there, its object ID matches the pinned commit's
    tree, and there are no extra ones.
    """
    problems: list[str] = []
    lock = _lock()
    links = _gitlinks()
    local: dict[str, set[str]] = {}  # repository -> the source files the analysis reads
    cbs = {}
    for name in ("rust", "nuttx", "zephyr", "rtthread"):
        cbs[name] = cb = get(name)
        for f in cb.files():
            local.setdefault(f.repo, set()).add(f.path)
    for repo, commit, kind in todo_list(which):
        root = paths.repo_dir(repo)
        if not (root / ".git").exists():
            problems.append(f"{repo}: not fetched yet. Run python -m fixprop fetch")
            continue
        g = gitio.git_dir(repo)
        if not gitio.has_commit(g, commit):
            problems.append(f"{repo}: the history does not contain the pinned commit {commit[:12]}")
            continue
        if gitio.head_commit(g) != commit:
            problems.append(f"{repo}: the checkout is not the pinned commit {commit[:12]}")
        if links is not None and links.get(paths.rel(root)) != commit:
            problems.append(f"{repo}: the registered submodule commit is {str(links.get(paths.rel(root)))[:12]}, "
                            f"data/repos.toml says {commit[:12]}")
        rec = lock.get(repo)
        if rec is None or rec.get("commit") != commit:
            problems.append(f"{repo}: data/repos.lock.json has no record for this commit")
        else:
            fp = fingerprint(repo, commit)
            if fp != rec:
                problems.append(f"{repo}: the history differs from the record (now cut off at {fp['cut_at']}, "
                                f"{fp['commits']} commits; recorded {rec['cut_at']}, {rec['commits']}). "
                                f"If this directory was fetched with git submodule update, delete it and run "
                                f"python -m fixprop fetch")
        cb = cbs["rust" if kind == "rust" else CODEBASE_OF[repo]]
        tree = {path: (mode, oid) for mode, typ, oid, path in gitio.ls_tree(g, commit, TREE_ROOT.get(repo, ""))
                if typ == "blob"}
        want = {p for p, (mode, _) in tree.items() if mode != "120000" and cb.in_tree_scope(repo, p, tree)}
        have = local.get(repo, set())
        for p in sorted(want - have)[:5]:
            problems.append(f"{repo}: missing source file {p}")
        for p in sorted(have - want)[:5]:
            problems.append(f"{repo}: extra source file not in the pinned commit: {p}")
        bad = [p for p in sorted(have & want) if git_blob_id((root / p).read_bytes()) != tree[p][1]]
        for p in bad[:5]:
            problems.append(f"{repo}: source file differs from the pinned commit: {p}")
        n_bad = len(want - have) + len(have - want) + len(bad)
        if n_bad > 5:
            problems.append(f"{repo}: only some are listed above; {n_bad} source files do not match in total")
    return problems


def run_verify(which: str | None = None) -> int:
    problems = verify(which)
    n = len(todo_list(which))
    if problems:
        print(f"[data] checked {n} repositories, {len(problems)} problems:")
        for p in problems:
            print("   ", p)
        return 1
    print(f"[data] checked {n} repositories: all at their pinned commits, histories match the record, and every source "
          f"file the analysis reads matches the pinned commit.")
    return 0
