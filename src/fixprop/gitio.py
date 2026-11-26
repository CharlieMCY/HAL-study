"""Read-only git operations: fetch an upstream repository, list commits, fetch file contents on demand, diff two
versions of a file.

Each upstream repository is an ordinary clone under data/repos/<owner>/<repo>/ (registered as a git submodule here):
    git clone --no-checkout --filter=blob:none --single-branch --shallow-since=<start> <url> <directory>
    git checkout --detach <pinned commit>
Only the commits and trees since the start are fetched. File contents are fetched only for what the pinned commit needs;
other versions are fetched in bulk when they are used.
The history of such a clone is cut off at the start: the commit at the cut has no parent and its change shows up as
adding the whole repository, so it is excluded when looking for bug-fix commits (shallow_roots).

The start is written with a time, "2023-06-01 00:00:00 +0000". With a date only, git uses the current time of day as
the boundary, and two clones can differ by a few commits.

So that results do not depend on the machine's git settings, local operations (reading history, diffing) ignore the
system and user git configuration, and the diff algorithm and context lines are given on the command line. Network
operations (cloning, fetching file contents) still read the user's configuration, because proxy settings live there.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from . import paths

SINCE = "2023-06-01 00:00:00 +0000"
ZERO = "0" * 40

ENV_NET = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1"}
ENV = {**ENV_NET, "GIT_CONFIG_GLOBAL": os.devnull}  # local operations: ignore the user's git configuration
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false", "-c", "core.quotePath=false",
        "-c", "gc.auto=0"]


class GitError(RuntimeError):
    pass


def git_dir(repo: str) -> Path:
    """The git directory of an upstream repository (where the history is).
    Fetched with `python -m fixprop fetch`, it is the directory data/repos/<owner>/<repo>/.git;
    fetched with git submodule, .git is a file that says where the real directory is."""
    d = paths.repo_dir(repo) / ".git"
    if d.is_file():
        line = d.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("gitdir:"):
            return (d.parent / line[len("gitdir:"):].strip()).resolve()
    return d


def run(gitdir: Path | None, *args: str, input: bytes | None = None, check: bool = True,
        timeout: int = 7200, net: bool = False) -> bytes:
    """net=True: this operation may use the network (cloning, fetching contents), so it runs with the user's configuration."""
    cmd = ["git", *SAFE]
    if gitdir is not None:
        cmd += ["--git-dir", str(gitdir)]
    r = subprocess.run([*cmd, *args], env=ENV_NET if net else ENV, input=input, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:4])} ...: {r.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    return r.stdout


def text(gitdir: Path | None, *args: str, **kw) -> str:
    return run(gitdir, *args, **kw).decode("utf-8", "replace")


# ------------------------------------------------------------------ fetching history


def has_commit(gitdir: Path, sha: str) -> bool:
    """Is the commit available locally? Uses rev-list --missing=allow-any: a missing object is an error instead of an
    on-demand fetch from the remote."""
    r = subprocess.run(["git", *SAFE, "--git-dir", str(gitdir), "rev-list", "--no-walk", "--quiet",
                        "--missing=allow-any", sha], env=ENV, capture_output=True)
    return r.returncode == 0


def clone(repo: str, pinned: str, sparse: list[str] | None = None, since: str = SINCE) -> str:
    """Fetch an upstream repository into data/repos/<owner>/<repo>/ and check out the pinned commit. A repository already
    there is left alone. Returns how it was fetched.
    sparse: only check out files matching these paths (for very large repositories); without it, the whole tree.
    A repository with no commit since the start (untouched for years) cannot be cloned from the start; then only the
    pinned commit is fetched."""
    d = paths.repo_dir(repo)
    url = f"https://github.com/{repo}"
    mode = "kept"
    if not (d / ".git").exists():
        d.mkdir(parents=True, exist_ok=True)
        if any(d.iterdir()):
            raise GitError(f"{paths.rel(d)} already contains something that is not a git repository. Empty it first.")
        r = subprocess.run(
            ["git", *SAFE, "clone", "-q", "--no-checkout", "--filter=blob:none", "--single-branch",
             f"--shallow-since={since}", url, str(d)], env=ENV_NET, capture_output=True, timeout=7200)
        mode = "shallow-since"
        if r.returncode != 0:
            import shutil

            shutil.rmtree(d / ".git", ignore_errors=True)
            run(None, "init", "-q", str(d))
            g = d / ".git"
            run(g, "remote", "add", "origin", url)
            run(g, "config", "remote.origin.promisor", "true")
            run(g, "config", "remote.origin.partialclonefilter", "blob:none")
            run(g, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", pinned, net=True)
            mode = "single-commit"
    g = git_dir(repo)
    if mode != "kept":
        # this is data: do not let git repack it in the background (commands such as git status in this project would trigger it)
        run(g, "config", "gc.auto", "0")
    if not has_commit(g, pinned):
        # the pinned commit is not in the default branch's history since the start: fetch it on its own
        run(g, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", pinned, net=True)
        mode += "+pinned"
    if text(g, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).strip() != pinned or mode != "kept":
        if sparse:
            run(g, "config", "core.sparseCheckout", "true")
            (g / "info").mkdir(exist_ok=True)
            (g / "info" / "sparse-checkout").write_text("".join(p + "\n" for p in sparse), encoding="utf-8")
        # check out the files of the pinned commit; their contents are fetched from the remote in bulk now
        r = subprocess.run(["git", *SAFE, "-c", "advice.detachedHead=false", "-C", str(d), "checkout", "-q", "--detach",
                            pinned], env=ENV_NET, capture_output=True, timeout=7200)
        if r.returncode != 0:
            raise GitError(f"checkout {pinned[:12]}: {r.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    return mode


def head_commit(gitdir: Path) -> str:
    return text(gitdir, "rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).strip()


def window(gitdir: Path, tip: str) -> tuple[list[str], int]:
    """A fingerprint of the history: walking back from tip, at which commits the history is cut off, and how many
    non-merge commits can be reached. Only what is reachable from tip counts; later upstream commits do not matter."""
    reach = set(text(gitdir, "rev-list", tip).split())
    return sorted(shallow_roots(gitdir) & reach), count_commits(gitdir, tip)


def shallow_roots(gitdir: Path) -> set[str]:
    f = gitdir / "shallow"
    return set(f.read_text().split()) if f.exists() else set()


def count_commits(gitdir: Path, tip: str, no_merges: bool = True) -> int:
    args = ["rev-list", "--count"] + (["--no-merges"] if no_merges else []) + [tip]
    return int(text(gitdir, *args).strip())


# ------------------------------------------------------------------ listing commits


@dataclass
class Change:
    status: str  # A M D T
    path: str
    old: str
    new: str


@dataclass
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_date: str
    commit_date: str
    subject: str
    body: str
    changes: list[Change] = field(default_factory=list)

    @property
    def message(self) -> str:
        return self.subject + "\n" + self.body


_FMT = "%x01%H%x02%P%x02%aI%x02%cI%x02%s%x02%b%x03"


def log_commits(gitdir: Path, tip: str) -> Iterator[Commit]:
    """Every non-merge commit reachable from tip, each with the files it changed (no rename detection: a rename shows as
    a deletion and an addition). No path arguments: with paths, git simplifies history and can miss commits on side
    branches in repositories with merges."""
    proc = subprocess.Popen(
        ["git", *SAFE, "--git-dir", str(gitdir), "log", "--no-merges", "--no-renames", "--raw", "--no-abbrev",
         f"--format={_FMT}", tip],
        env=ENV, stdout=subprocess.PIPE)
    assert proc.stdout is not None
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            parts = buf.split(b"\x01")
            buf = parts.pop()  # the last part may be incomplete
            for p in parts:
                if p.strip():
                    yield _parse_commit(p)
        if buf.strip():
            yield _parse_commit(buf)
    finally:
        proc.stdout.close()
        proc.wait()


def _parse_commit(raw: bytes) -> Commit:
    head, _, tail = raw.partition(b"\x03")
    f = head.decode("utf-8", "replace").split("\x02")
    c = Commit(sha=f[0], parents=tuple(f[1].split()), author_date=f[2], commit_date=f[3], subject=f[4],
               body=f[5] if len(f) > 5 else "")
    for ln in tail.decode("utf-8", "replace").split("\n"):
        if not ln.startswith(":"):
            continue
        meta, _, path = ln.partition("\t")
        m = meta.split()
        if len(m) >= 5 and path:
            c.changes.append(Change(status=m[4][0], path=path, old=m[2], new=m[3]))
    return c


# ------------------------------------------------------------------ file contents


def ls_tree(gitdir: Path, commit: str, path: str = "") -> list[tuple[str, str, str, str]]:
    """Every file under path in commit: (mode, type, object ID, path). Uses only the tree; no file contents are fetched."""
    args = ["ls-tree", "-r", commit] + ([path] if path else [])
    out = []
    for ln in text(gitdir, *args).split("\n"):
        meta, _, p = ln.partition("\t")
        m = meta.split()
        if len(m) == 3 and p:
            out.append((m[0], m[1], m[2], p))
    return out


def present_objects(gitdir: Path) -> set[str]:
    """The object IDs already available locally (without triggering an on-demand fetch)."""
    out = text(gitdir, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)", "--unordered")
    return set(out.split())


def fetch_blobs(gitdir: Path, oids: Iterable[str], chunk: int = 4000) -> int:
    """Fetch file contents in bulk, only those not yet local. Returns how many were fetched."""
    want = sorted({o for o in oids if o and o != ZERO})
    if not want:
        return 0
    have = present_objects(gitdir)
    todo = [o for o in want if o not in have]
    for i in range(0, len(todo), chunk):
        part = "\n".join(todo[i : i + chunk]).encode() + b"\n"
        run(gitdir, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "origin", "--no-tags",
            "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin", input=part,
            net=True)
    return len(todo)


def cat_blobs(gitdir: Path, oids: Iterable[str]) -> dict[str, bytes]:
    """Read the contents of a batch of objects. Call fetch_blobs first; this does not use the network."""
    want = sorted({o for o in oids if o and o != ZERO})
    if not want:
        return {}
    data = run(gitdir, "cat-file", "--batch", input=("\n".join(want) + "\n").encode())
    out: dict[str, bytes] = {}
    pos = 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        out[header[0].decode()] = data[pos : pos + size]
        pos += size + 1
    return out


_DIFFS: dict[tuple[str, str, str, int], str] = {}


def diff_blobs(gitdir: Path, old: str, new: str, context: int = 3) -> str:
    """The unified diff of two versions of a file. Both objects must be local already.
    The algorithm and all options are fixed and do not depend on git settings. The same pair is used several times in
    one run, so the result is cached."""
    key = (str(gitdir), old, new, context)
    if key not in _DIFFS:
        r = subprocess.run(
            ["git", *SAFE, "--git-dir", str(gitdir), "diff", "--no-color", "--no-ext-diff", "--no-textconv",
             "--diff-algorithm=myers", "--indent-heuristic", "--inter-hunk-context=0", f"-U{context}", old, new],
            env=ENV, capture_output=True)
        if r.returncode not in (0, 1):
            raise GitError(r.stderr.decode("utf-8", "replace")[-300:])
        _DIFFS[key] = r.stdout.decode("utf-8", "replace")
    return _DIFFS[key]
