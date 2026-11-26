"""Steps 1 to 3: find copied files; find bug-fix commits; compare every fix with every copy.

Output in benchmark/<codebase>/:
  copy_pairs.jsonl   copied file pairs (all with at least 30 shared lines and a ratio of at least 20%; the analysis uses
                     the pairs at 30% or more)
  fix_status.jsonl   one row per (bug-fix commit, changed file, corresponding file in another driver), with the outcome
                     and line numbers of every hunk
  stats.json         the counts of each step

A bug-fix commit is a non-merge commit whose message contains "fix" not preceded by a letter (so "prefix" and "suffix"
do not count). A commit that takes another commit back is left out: git gives it a subject that starts with Revert "
followed by the subject of that commit, which often contains "fix" itself. The rule is loose on purpose: it lets in
commits that are not fixes, which the reading in step 3 sorts out, and it misses fixes that do not say "fix".

Every hunk of a bug-fix commit is looked up in every copy of the changed file (the rules are in hunks.py). A
(bug-fix commit, other driver) pair then gets one of four outcomes, the first in this order that fits any of its places:
  candidate    not changed yet: at the place of the change, the copy still has the code from before the fix
  same_commit  changed together: the commit also changed the copy
  has_fix      already fixed: at the place of the change, the copy has the code from after the fix
  diverged     neither: the two files have grown apart at this place

For every candidate the records also say whether the fixed side has rewritten the fixed code since (source_still_fixed)
and where the lines the fix adds appear elsewhere in the copy (added_elsewhere).
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import gitio, hunks, pairs, paths
from . import normalize as N
from .codebases import Codebase
from .common import log, read_json, read_jsonl, write_json, write_jsonl

FIX = re.compile(r"(?i)(?<![a-z])(?:bug|hot)?fix")  # fix not preceded by a letter (prefix, suffix do not count); bugfix, hotfix count
REVERT = re.compile(r'(?i)^\s*revert "')  # the subject git gives a commit that takes another commit back: Revert "..."
TIER = 0.3
SUCC_MAX_FILES = 100  # when finding where a file moved, lines found in more files than this are not used
SUCC_MIN_RATIO = 0.5


def out_dir(cb: Codebase | str) -> Path:
    return paths.OUT / (cb if isinstance(cb, str) else cb.name)


def merge_stats(cb: Codebase, part: dict) -> None:
    f = out_dir(cb) / "stats.json"
    cur = read_json(f) if f.exists() else {}
    cur.update(part)
    write_json(f, dict(sorted(cur.items())))  # top level sorted by name: the file is the same whichever step runs first


# ------------------------------------------------------------------ step 1


def step_pairs(cb: Codebase, files=None) -> dict:
    files = files or pairs.load_files(cb)
    ps, st = pairs.find_pairs(cb, files)
    write_jsonl(out_dir(cb) / "copy_pairs.jsonl", ps)
    stats = {
        "units": len({fl.f.unit for fl in files.values()}),
        "source_files": len(files),
        "participating_files": st["participating"],
        "pairs": {}, "pair_files": {}, "pairs_same_name": {}, "pairs_cross_repo": {},
    }
    for t in pairs.TIERS:
        sel = pairs.at_tier(ps, t)
        key = str(int(t * 100))
        stats["pairs"][key] = len(sel)
        stats["pair_files"][key] = len({(p["repo_a"], p["file_a"]) for p in sel} | {(p["repo_b"], p["file_b"]) for p in sel})
        stats["pairs_same_name"][key] = sum(1 for p in sel if p["same_name"])
        stats["pairs_cross_repo"][key] = sum(1 for p in sel if p["repo_a"] != p["repo_b"])
    merge_stats(cb, {"pairs": stats})
    log(f"[pairs] {cb.name}: {stats['units']} drivers, {stats['source_files']} source files, "
        f"copy pairs at 20%/30%/50% = {stats['pairs']['20']}/{stats['pairs']['30']}/{stats['pairs']['50']}")
    return stats


# ------------------------------------------------------------------ steps 2 and 3


class Successors:
    """Which file at the pinned commit a historical path corresponds to. If the path still exists, itself; if not (renamed
    or moved), found by content: the file sharing the most counted lines with the fixed version, at a ratio above half."""

    def __init__(self, cb: Codebase, files: dict[tuple[str, str], pairs.FileLines]):
        self.cb, self.files = cb, files
        self.cache: dict[tuple[str, str], tuple[str, str] | None] = {}
        self._index: dict[str, list[tuple[str, str]]] | None = None

    def get(self, repo: str, path: str) -> tuple[str, str] | None:
        if (repo, path) in self.files:
            return (repo, path)
        return self.cache.get((repo, path))

    def resolve(self, repo: str, path: str, blob: bytes | None) -> tuple[str, str] | None:
        key = (repo, path)
        if key in self.files:
            return key
        if key in self.cache:
            return self.cache[key]
        best = None
        if blob is not None:
            if self._index is None:
                idx: dict[str, list[tuple[str, str]]] = defaultdict(list)
                for k, fl in self.files.items():
                    for ln in fl.valid:
                        idx[ln].append(k)
                self._index = {ln: ks for ln, ks in idx.items() if len(ks) <= SUCC_MAX_FILES}
            norm = self.cb.normalizer(repo, path)
            valid = {s for s in (norm(ln) for ln in blob.decode("utf-8", "replace").split("\n")) if self.cb.valid_pair(s)}
            hits: Counter = Counter()
            for ln in valid:
                for k in self._index.get(ln, ()):
                    if k[0] == repo:
                        hits[k] += 1
            if hits:
                k, n = min(hits.items(), key=self._rank)
                if n >= pairs.MIN_SHARED and n >= SUCC_MIN_RATIO * min(len(valid), len(self.files[k].valid)):
                    best = k
        self.cache[key] = best
        return best

    @staticmethod
    def _rank(kv: tuple[tuple[str, str], int]):
        """Most shared lines first; on a tie (several nearly identical copies) the path that sorts first.
        So every run picks the same file. On a tie the pick is not necessarily the right file; see the notes in
        judgments/results.md."""
        k, n = kv
        return (-n, k)


class Judge:
    """How a hunk is judged: compared without comments, and by position (rules in hunks.py)."""

    def __init__(self, cb: Codebase, files: dict[tuple[str, str], pairs.FileLines]):
        self.cb, self.files = cb, files
        self._views: dict[tuple[str, str], tuple[list[str], hunks.View]] = {}

    def norm(self, repo: str, path: str) -> N.Norm:
        return N.code_only(self.cb.normalizer(repo, path))

    def view(self, key: tuple[str, str]) -> tuple[list[str], hunks.View]:
        """A file seen as code only: (every line with comments removed, the structure used for positional lookups)."""
        if key not in self._views:
            code = [N.norm_ws(N.strip_comments(s)) for s in self.files[key].lines]
            self._views[key] = (code, hunks.make_view(code, self.cb.valid_change))
        return self._views[key]

    def classify(self, h: hunks.Hunk, tgt: tuple[str, str], norm: N.Norm) -> hunks.Verdict:
        return hunks.classify(h, self.view(tgt)[1], norm, self.cb.valid_change, self.cb.strict_replace)

    def still_fixed(self, v: hunks.Verdict, src_now: tuple[str, str]) -> bool:
        return hunks.still_fixed(v, self.view(src_now)[1], self.cb.valid_change)

    def lines(self, tgt: tuple[str, str]) -> list[str]:
        return self.view(tgt)[0]


def collect_fixes(cb: Codebase) -> tuple[dict, list]:
    """Step 2: the bug-fix commits in the window. Returns (counts, bug-fix commits that changed a source file in scope)."""
    st: dict = {"commits_no_merges": 0, "fix_commits": 0, "revert_commits_left_out": 0}
    fixes: list[tuple[str, gitio.Commit, list[gitio.Change]]] = []
    for repo, pinned in sorted(cb.repos.items()):
        gitdir = gitio.git_dir(repo)
        roots = gitio.shallow_roots(gitdir)
        for c in gitio.log_commits(gitdir, pinned):
            st["commits_no_merges"] += 1
            if c.sha in roots:
                continue  # the history is cut off here: this commit has no parent and its change shows as adding everything
            if FIX.search(c.message) is None:
                continue
            scoped = [ch for ch in c.changes if cb.in_scope(repo, ch.path)]
            if not scoped:
                continue
            if REVERT.match(c.subject):  # taking a commit back is not a bug fix to carry over to the copies
                st["revert_commits_left_out"] += 1
                continue
            st["fix_commits"] += 1
            fixes.append((repo, c, scoped))
    return st, fixes


def step_status(cb: Codebase, files=None, tier: float = TIER) -> tuple[dict, list[dict]]:
    """Step 3. Returns (counts, one record per (commit, changed file, file on the other side)); the records are also
    written to fix_status.jsonl."""
    t0 = time.time()
    files = files or pairs.load_files(cb)
    ps = read_jsonl(out_dir(cb) / "copy_pairs.jsonl")
    partners = pairs.partner_index(ps, tier)
    succ = Successors(cb, files)
    st, fixes = collect_fixes(cb)

    # fetch file contents: for changed paths that still exist and have copies, or that no longer exist (to find by
    # content where they moved)
    need: dict[str, set[str]] = defaultdict(set)
    for repo, c, scoped in fixes:
        for ch in scoped:
            if ch.status != "M":
                continue
            key = (repo, ch.path)
            if (key in files and key in partners) or key not in files:
                need[repo] |= {ch.old, ch.new}
    fetched = 0
    for repo, oids in sorted(need.items()):
        fetched += gitio.fetch_blobs(gitio.git_dir(repo), oids)

    # paths that no longer exist: find the current file by content (using the newest version; the log is newest first)
    newest: dict[tuple[str, str], str] = {}
    for repo, c, scoped in fixes:
        for ch in scoped:
            if ch.status == "M" and (repo, ch.path) not in files:
                newest.setdefault((repo, ch.path), ch.new)
    by_repo: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (repo, path), oid in newest.items():
        by_repo[repo].append((path, oid))
    for repo, items in by_repo.items():
        blobs = gitio.cat_blobs(gitio.git_dir(repo), [o for _, o in items])
        for path, oid in items:
            succ.resolve(repo, path, blobs.get(oid))
    st["paths_gone"] = len(succ.cache)
    st["paths_gone_resolved"] = sum(1 for v in succ.cache.values() if v is not None)

    rows: list[dict] = []
    with_pair_commits: set[str] = set()
    judge = Judge(cb, files)
    for repo, c, scoped in fixes:
        gitdir = gitio.git_dir(repo)
        touched_now = {succ.get(repo, ch.path) for ch in scoped} - {None}
        for ch in scoped:
            if ch.status != "M":
                continue
            src_now = succ.get(repo, ch.path)
            if src_now is None or src_now not in partners:
                continue
            with_pair_commits.add(c.sha)
            hs = hunks.parse_diff(gitio.diff_blobs(gitdir, ch.old, ch.new))
            norm = judge.norm(repo, ch.path)
            for tgt in partners[src_now]:
                tfl = files[tgt]
                row = {
                    "codebase": cb.name, "repo": repo, "commit": c.sha, "date": c.commit_date[:10],
                    "subject": c.subject[:160], "source": ch.path, "blobs": [ch.old, ch.new],
                    "source_now": src_now[1],
                    "source_unit": files[src_now].f.unit,
                    "target_repo": tgt[0], "target_commit": cb.repos[tgt[0]], "target": tgt[1],
                    "target_unit": tfl.f.unit, "status": "skip", "hunks": [],
                }
                if tgt in touched_now:
                    row["status"] = "same_commit"
                    rows.append(row)
                    continue
                seen = set()
                for i, h in enumerate(hs):
                    v = judge.classify(h, tgt, norm)
                    if v.status == "skip":
                        continue
                    item = {"i": i, "old_start": h.old_start, "new_start": h.new_start, "status": v.status,
                            "kind": v.kind}
                    if v.status == "candidate":
                        item["source_still_fixed"] = judge.still_fixed(v, src_now)
                        item["dels"], item["adds"], item["ctx"] = v.dels, v.adds, v.ctx
                        item["target_lines"] = hunks.line_numbers(judge.lines(tgt), v.dels or v.ctx)
                        # the place is unchanged, but the copy may hold the added lines somewhere else
                        item["added_elsewhere"] = hunks.line_numbers(judge.lines(tgt), v.adds)
                    row["hunks"].append(item)
                    seen.add(v.status)
                cand = [x for x in row["hunks"] if x["status"] == "candidate"]
                if cand:
                    row["status"] = "candidate"
                    row["source_still_fixed"] = any(x["source_still_fixed"] for x in cand)
                elif "has_fix" in seen:
                    row["status"] = "has_fix"
                elif seen:
                    row["status"] = "diverged"
                rows.append(row)
    st["fix_touching_paired_files"] = len(with_pair_commits)
    st.update(summarize(rows))
    write_jsonl(out_dir(cb) / "fix_status.jsonl", rows)
    merge_stats(cb, {"status": st})
    log(f"[status] {cb.name}: {st['commits_no_merges']} non-merge commits, "
        f"{st['fix_commits']} bug-fix commits, {st['fix_touching_paired_files']} of them changed a file with a copy; "
        f"{st['checked_pairs']} pairs checked, {st['candidate_pairs']} candidates; "
        f"{fetched} file versions fetched ({time.time() - t0:.0f}s)")
    return st, rows


# precedence when one (commit, other driver) has several files
STATUSES = ("candidate", "same_commit", "has_fix", "diverged")


def unit_level(rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Combine the (commit, changed file, file on the other side) records into (commit, other driver).
    Precedence: candidate > same_commit > has_fix > diverged; a pair with only skipped hunks is not checked."""
    agg: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r["commit"], r["target_repo"], r["target_unit"])
        a = agg.setdefault(key, {"statuses": set(), "still_fixed": False, "rows": []})
        a["statuses"].add(r["status"])
        a["still_fixed"] |= bool(r.get("source_still_fixed"))
        a["rows"].append(r)
    for a in agg.values():
        s = a["statuses"]
        a["status"] = next((x for x in STATUSES if x in s), "skip")
    return agg


def candidates(rows: list[dict]) -> set[tuple[str, str, str]]:
    """The candidates as (commit, repository of the other driver, other driver)."""
    return {k for k, a in unit_level(rows).items() if a["status"] == "candidate"}


def summarize(rows: list[dict]) -> dict:
    agg = unit_level(rows)
    checked = {k: a for k, a in agg.items() if a["status"] != "skip"}
    dist = Counter(a["status"] for a in checked.values())
    cand = {k: a for k, a in checked.items() if a["status"] == "candidate"}
    hunk_dist: Counter = Counter()
    kinds: Counter = Counter()
    for r in rows:
        if r["status"] == "same_commit":
            continue
        for h in r["hunks"]:
            hunk_dist[h["status"]] += 1
            if h["status"] == "candidate":
                kinds[h["kind"]] += 1
    return {
        "checked_pairs": len(checked),
        "pair_status": {k: dist.get(k, 0) for k in ("same_commit", "has_fix", "candidate", "diverged")},
        "hunk_status": dict(hunk_dist),
        "candidate_hunks_by_kind": dict(kinds),
        "candidate_commits": len({k[0] for k in cand}),
        "candidate_pairs": len(cand),
        "candidate_pairs_still_fixed": sum(1 for a in cand.values() if a["still_fixed"]),
        "candidate_pairs_not_still_fixed": sum(1 for a in cand.values() if not a["still_fixed"]),
        "candidate_commits_still_fixed": len({k[0] for k, a in cand.items() if a["still_fixed"]}),
        "candidate_by_target_unit": dict(Counter(k[2] for k in cand).most_common(15)),
    }
