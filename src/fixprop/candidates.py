"""Step 4: list every candidate (a copy that still has the pre-fix code) and give each one a fixed ID.

Every candidate is read by a model (see packets.py and reader.py), so each one needs a name that does not change. IDs:
  candidates are sorted by (commit, repository of the copy, driver of the copy) and numbered <codebase>-001, -002, ...
  in that order. A candidate that already has a judgment in judgments/<codebase>/labels.jsonl keeps the judgment's ID,
  so regenerating the list half-way through the reading does not renumber anything.

This module builds one record per candidate: which commit fixed what, which driver holds the copy, which hunks were
picked out, and which lines of the copy still have the pre-fix code. Two more things are recorded for later reading
and counting: whether the fixed side has rewritten the fixed code since the fix (source_still_fixed), and which other
candidates come from the same bug-fix commit (same_fix). labels.py merges the judgments into these records and writes
benchmark/<codebase>/candidates.jsonl.
"""

from __future__ import annotations

from collections import defaultdict

from . import paths
from .census import unit_level
from .codebases import Codebase
from .common import read_jsonl

MAX_FILES, MAX_HUNKS = 4, 6  # at most this many files per record, and this many hunks per file

Key = tuple[str, str, str]  # (bug-fix commit, repository of the copy, driver of the copy)


def commit_url(repo: str, sha: str) -> str:
    return f"https://github.com/{repo}/commit/{sha}"


def blob_url(repo: str, commit: str, path: str, lines: list[int]) -> str:
    anchor = f"#L{lines[0]}" if lines else ""
    return f"https://github.com/{repo}/blob/{commit}/{path}{anchor}"


def key_of(rec: dict) -> Key:
    return (rec["commit"], rec["target_repo"], rec["target_unit"])


def id_order(sid: str) -> tuple[int, int]:
    """Sort key of an ID: candidates (<codebase>-001) before sampled diverged pairs (<codebase>-D01), each by number."""
    tail = sid.rsplit("-", 1)[1]
    return (1, int(tail[1:])) if tail.startswith("D") else (0, int(tail))


def load_labels(name: str) -> dict[Key, dict]:
    """judgments/<codebase>/labels.jsonl: (commit, repository, driver) -> judgment."""
    return {key_of(x): x for x in read_jsonl(paths.JUDGMENTS / name / "labels.jsonl")}


def numbered(name: str, agg: dict) -> list[tuple[str, Key, bool]]:
    """Every candidate in this codebase as (ID, key, has been read), sorted by (commit, repository, driver).

    The n-th candidate in sorted order gets <codebase>-<n>, unless it already has a judgment, which keeps its ID.
    If that number is already used by another judgment (the candidate list has changed), the next free number is used."""
    labels = load_labels(name)
    taken = {x["id"] for x in labels.values()}
    out, n = [], 0
    for key in sorted(k for k, a in agg.items() if a["status"] == "candidate"):
        n += 1
        if key in labels:
            out.append((labels[key]["id"], key, bool(labels[key].get("label"))))
            continue
        m = n
        while f"{name}-{m:03d}" in taken:
            m += 1
        sid = f"{name}-{m:03d}"
        taken.add(sid)
        out.append((sid, key, False))
    return out


def build(cb: Codebase, sid: str, key: Key, a: dict) -> dict:
    """The record of one candidate. a is this candidate's entry from census.unit_level."""
    sha, trepo, tunit = key
    rows = [r for r in a["rows"] if r["status"] == "candidate"]
    r0 = rows[0]
    repo = r0["repo"]
    rec = {
        "id": sid, "codebase": cb.name, "repo": repo, "commit": sha, "commit_url": commit_url(repo, sha),
        "date": r0["date"], "subject": r0["subject"], "target_repo": trepo, "target_commit": r0["target_commit"],
        "target_unit": tunit, "source_still_fixed": a["still_fixed"], "files": [],
    }
    for r in rows[:MAX_FILES]:
        cand = [h for h in r["hunks"] if h["status"] == "candidate"]
        all_lines = sorted({n for h in cand for n in h["target_lines"]})
        rec["files"].append({
            "source": r["source"], "source_now": r["source_now"], "target": r["target"],
            "target_url": blob_url(trepo, r["target_commit"], r["target"], all_lines), "target_lines": all_lines,
            "hunks": [{"kind": h["kind"], "old_start": h["old_start"], "source_still_fixed": h["source_still_fixed"],
                       "removed": h["dels"][:6], "added": h["adds"][:6], "context": h["ctx"],
                       "target_lines": h["target_lines"], "added_elsewhere": h["added_elsewhere"]} for h in cand[:MAX_HUNKS]],
        })
    return rec


def run(cb: Codebase, rows: list[dict]) -> list[dict]:
    """rows: the records returned by census.step_status. Returns one record per candidate, before judgments are merged."""
    agg = unit_level(rows)
    recs = [build(cb, sid, key, agg[key]) for sid, key, _ in numbered(cb.name, agg)]
    by_commit: dict[str, list[str]] = defaultdict(list)
    for r in recs:
        by_commit[r["commit"]].append(r["id"])
    for r in recs:  # one fix often reaches several copies: name the others, so they can be read and counted together
        r["same_fix"] = [i for i in by_commit[r["commit"]] if i != r["id"]]
    return recs
