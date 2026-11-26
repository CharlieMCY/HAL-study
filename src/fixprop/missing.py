"""Step 6: list the pairs flagged as missed fixes after reading the code; count them and estimate the total.

A missed fix: a bug was fixed in one driver, and another driver with a copy of the code has the same problem and has not
been fixed as of the pinned commit. The pairs here are flagged by Claude Code; the pairs confirmed by human review are in
benchmark/final.jsonl (see review.py).

Output
  benchmark/missing.jsonl   one record per flagged pair: what was fixed, where the copy is, the hunks, the copy's current
                            code, whether the patch ports as is, impact, evidence, reader
  benchmark/summary.json    the counts of every step; the flagged pairs; the progress of human review

Unpropagated fixes come in two parts, counted differently:
  candidates      every one is read. When all are read, the flagged count is an exact count.
                  While some are unread, the share flagged among those read is scaled up to all of them, with a 95%
                  Wilson interval.
  diverged pairs  too many to read. They are put into three groups and 3 or 4 per group are read; each group's share is
                  scaled up to the group. The sample is small, so this only shows the order of magnitude, without an
                  interval. Pairs whose changed function cannot be identified are not sampled and not estimated.
"""

from __future__ import annotations

import math
from collections import Counter

from . import gitio, hunks, paths
from .candidates import blob_url
from .census import out_dir, unit_level
from .codebases import ORDER, Codebase, get
from .common import log, read_json, read_jsonl, write_json, write_jsonl
from .diverged import ALLOC
from .labels import IMPACT, LABELS, PORT

MAX_FILES, MAX_CHANGES = 4, 8  # at most this many files per record, and this many hunks per file


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - h) / d), min(1.0, (c + h) / d)


def _split(recs: list[dict], field: str, values: tuple[str, ...], scale: float) -> dict:
    c = Counter(r.get(field) for r in recs)
    return {v: {"read": c[v], "est": c[v] * scale} for v in values}


# ------------------------------------------------------------------ counts and estimates


def count_candidates(name: str) -> dict:
    """The candidates: how many were read, how many got each verdict, how many were flagged."""
    recs = read_jsonl(out_dir(name) / "candidates.jsonl")
    read = [r for r in recs if r.get("label")]
    real = [r for r in read if r["label"] == "missing"]
    total, n, k = len(recs), len(read), len(real)
    out = {
        "candidates": total, "read": n, "unread": total - n,
        "labels": {lab: sum(1 for r in read if r["label"] == lab) for lab in LABELS},
        "missing": k,
    }
    if n == 0:
        out.update(est=None, lo=None, hi=None, by_port={}, by_impact={})
        return out
    if n >= total:  # all read: the flagged count is exact
        scale, lo, hi = 1.0, k, k
    else:
        scale = total / n
        wl, wh = wilson(k, n)
        lo, hi = max(k, round(wl * total)), min(total - (n - k), round(wh * total))
    out.update(est=k * scale, lo=lo, hi=hi,
               by_port=_split(real, "port", PORT, scale), by_impact=_split(real, "impact", IMPACT, scale))
    return out


def estimate_diverged(name: str) -> dict:
    """The diverged pairs: how many of the sampled ones were flagged, and the estimated total."""
    d = out_dir(name)
    pop = Counter(r["bucket"] for r in read_jsonl(d / "diverged.jsonl"))
    labs = [r for r in read_jsonl(d / "diverged_sample.jsonl") if r.get("label")]
    est, port, impact, buckets = 0.0, Counter(), Counter(), {}
    for b in ALLOC:
        rs = [r for r in labs if r["bucket"] == b]
        real = [r for r in rs if r["label"] == "same_bug"]
        scale = pop[b] / len(rs) if rs else 0.0
        buckets[b] = {"total": pop[b], "read": len(rs), "same_bug": len(real), "est": len(real) * scale}
        est += len(real) * scale
        for r in real:
            port[r.get("port")] += scale
            impact[r.get("impact")] += scale
    real_all = [r for r in labs if r["label"] == "same_bug"]
    return {
        "diverged": sum(pop.values()), "not_covered": pop["unknown"], "read": len(labs), "same_bug": len(real_all),
        "est": est, "buckets": buckets,
        "by_port": {v: {"read": sum(1 for r in real_all if r.get("port") == v), "est": port[v]} for v in PORT},
        "by_impact": {v: {"read": sum(1 for r in real_all if r.get("impact") == v), "est": impact[v]} for v in IMPACT},
    }


# ------------------------------------------------------------------ the list of flagged pairs


def _excerpt(lines: list[str], at: list[int], radius: int = 4, max_blocks: int = 3) -> str:
    """The lines of a file around the lines in at, with line numbers; the lines in at are marked with >."""
    out, last = [], -10
    for n in at[:max_blocks]:
        lo, hi = max(1, n - radius), min(len(lines), n + radius)
        if lo <= last:
            lo = last + 1
        if lo > hi:
            continue
        if out:
            out.append("   ...")
        out += [f"{i:5}{'>' if i in at else ' '} {lines[i - 1]}" for i in range(lo, hi + 1)]
        last = hi
    return "\n".join(out)


def _record(cb: Codebase, r: dict, agg: dict, kind: str) -> dict:
    """kind: candidate = picked out by script and flagged after reading; diverged = found while reading a sampled
    diverged pair."""
    key = (r["commit"], r["target_repo"], r["target_unit"])
    a = agg.get(key, {"rows": [], "still_fixed": None})
    gitdir = gitio.git_dir(r["repo"])
    places = []
    for row in [x for x in a["rows"] if x["status"] == kind][:MAX_FILES]:
        hs = hunks.parse_diff(gitio.diff_blobs(gitdir, row["blobs"][0], row["blobs"][1]))
        tlines = (cb.root(row["target_repo"]) / row["target"]).read_text(encoding="utf-8", errors="replace").split("\n")
        sel = [h for h in row["hunks"] if h["status"] == kind
               and (kind != "candidate" or cb.count_delete_only or h["kind"] != "delete")]
        changes = []
        for h in sel[:MAX_CHANGES]:
            ch = {"kind": h["kind"], "source_line": h["old_start"], "diff": hs[h["i"]].text()}
            if kind == "candidate":
                ch["target_lines"] = h["target_lines"]
                ch["target_now"] = _excerpt(tlines, h["target_lines"])
            changes.append(ch)
        at = sorted({n for h in sel for n in h.get("target_lines", [])})
        places.append({"source": row["source"], "source_now": row["source_now"], "target": row["target"],
                       "target_url": blob_url(row["target_repo"], row["target_commit"], row["target"], at),
                       "changes": changes, "changes_not_listed": max(0, len(sel) - MAX_CHANGES)})
    pinned = cb.repos[r["target_repo"]]
    w = r.get("where") or {}
    rec = {
        "id": r["id"], "codebase": cb.name, "found_in": kind,
        "title": r.get("title", ""), "summary": r.get("summary", ""), "port": r.get("port"), "impact": r.get("impact"),
        "confidence": r.get("confidence"),
        "reader": r.get("reader"),
        "fix": {"repo": r["repo"], "commit": r["commit"], "url": r["commit_url"], "date": r["date"], "subject": r["subject"]},
        "copy": {"repo": r["target_repo"], "unit": r["target_unit"], "commit": pinned,
                 "file": w.get("file"), "line": w.get("line"),
                 "url": blob_url(r["target_repo"], pinned, w["file"], [w["line"]]) if w.get("file") else None},
        "source_still_fixed": a["still_fixed"] if kind == "candidate" else None,
        "places": places, "evidence": r["evidence"], "evidence_refs": r["evidence_refs"],
    }
    for k in ("impact_note", "same_problem_as", "same_problem_note", "same_problem_by"):
        if r.get(k):
            rec[k] = r[k]
    return rec


def distinct(found: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop the pairs that repeat a problem already counted from another fix. Returns (distinct problems, problems).
    same_problem_as must point at another flagged pair that is not itself a repeat."""
    ids = {r["id"]: r for r in found}
    problems = []
    for r in found:
        other = r.get("same_problem_as")
        if not other:
            continue
        if other not in ids:
            problems.append(f"{r['id']}: same_problem_as points at {other}, which is not a flagged pair")
        elif ids[other].get("same_problem_as"):
            problems.append(f"{r['id']}: same_problem_as points at {other}, which is itself a repeat; point at the first one")
        elif ids[other]["copy"]["repo"] != r["copy"]["repo"] or ids[other]["copy"]["unit"] != r["copy"]["unit"]:
            problems.append(f"{r['id']}: same_problem_as points at {other}, which is in a different driver")
    return [r for r in found if not r.get("same_problem_as")], problems


def collect(cb: Codebase) -> list[dict]:
    """One full record per flagged pair in this codebase."""
    d = out_dir(cb)
    agg = unit_level(read_jsonl(d / "fix_status.jsonl"))
    out = [_record(cb, r, agg, "candidate") for r in read_jsonl(d / "candidates.jsonl") if r.get("label") == "missing"]
    out += [_record(cb, r, agg, "diverged") for r in read_jsonl(d / "diverged_sample.jsonl") if r.get("label") == "same_bug"]
    return out


# ------------------------------------------------------------------ summary


def _add(a: dict, b: dict) -> dict:
    return {v: {"read": a.get(v, {}).get("read", 0) + b.get(v, {}).get("read", 0),
                "est": a.get(v, {}).get("est", 0.0) + b.get(v, {}).get("est", 0.0)} for v in b}


def _rounded(x):
    """Estimates are scaled fractions; they are written with two decimals."""
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v) for v in x]
    return x


NOTES = ("Every count is of (bug-fix commit, other driver) pairs. confirmed: the pairs Claude Code flagged as missed fixes "
         "after reading the code; human_review: the progress and outcome of human review. candidates: every candidate is "
         "read, so once all are read, missing is an exact count (est, lo and hi equal it). diverged: only a sample is read; "
         "est is the sample scaled up to all diverged pairs. read: the number read. The method is described at the top of "
         "src/fixprop/missing.py.")


def summarize(found: list[dict], human: dict | None = None) -> dict:
    funnel, cand, div = {}, {}, {}
    for name in ORDER:
        st = read_json(out_dir(name) / "stats.json")
        s, p = st["status"], st["pairs"]
        funnel[name] = {
            "repos": len(get(name).repos),
            "drivers": p["units"], "source_files": p["source_files"], "copied_file_pairs": p["pairs"]["30"],
            "commits": s["commits_no_merges"], "fix_commits": s["fix_commits"],
            "fix_commits_on_copied_files": s["fix_touching_paired_files"],
            "checked": s["checked_pairs"], **s["pair_status"],
            "candidate_source_still_fixed": s["candidate_pairs_still_fixed"],
        }
        cand[name], div[name] = count_candidates(name), estimate_diverged(name)
    funnel["total"] = {k: sum(funnel[n][k] for n in ORDER) for k in funnel[ORDER[0]]}

    def total(parts: dict, skip: tuple[str, ...]) -> dict:
        first = parts[ORDER[0]]
        t: dict = {}
        for k, v in first.items():
            if k in skip:
                continue
            if isinstance(v, dict) and k in ("by_port", "by_impact"):
                acc: dict = {}
                for n in ORDER:
                    acc = _add(acc, parts[n][k]) if parts[n][k] else acc
                t[k] = acc
            elif isinstance(v, dict):
                t[k] = {kk: sum(parts[n][k].get(kk, 0) for n in ORDER) for kk in v}
            else:
                t[k] = sum(parts[n][k] or 0 for n in ORDER)
        return t

    ct, dt = total(cand, ()), total(div, ("buckets",))
    by = lambda f, vals: {v: sum(1 for r in found if r.get(f) == v) for v in vals}  # noqa: E731
    uniq, _ = distinct(found)
    return {
        "notes": NOTES,
        "funnel": funnel,
        "confirmed": {
            "total": len(found), "from_candidates": sum(1 for r in found if r["found_in"] == "candidate"),
            "from_diverged": sum(1 for r in found if r["found_in"] == "diverged"),
            "by_codebase": {n: sum(1 for r in found if r["codebase"] == n) for n in ORDER},
            "by_port": by("port", PORT), "by_impact": by("impact", IMPACT),
            "by_confidence": by("confidence", ("high", "medium", "low")),
            "distinct": {
                "total": len(uniq),
                "by_codebase": {n: sum(1 for r in uniq if r["codebase"] == n) for n in ORDER},
                "by_port": {v: sum(1 for r in uniq if r.get("port") == v) for v in PORT},
                "by_impact": {v: sum(1 for r in uniq if r.get("impact") == v) for v in IMPACT},
                "repeated": [[r["id"], r["same_problem_as"]] for r in found if r.get("same_problem_as")],
            },
        },
        "candidates": {**cand, "total": ct},
        "diverged": {**div, "total": dt},
        "human_review": human or {},
        "estimate": {
            "total": ct["est"] + dt["est"],
            "by_port": _add(ct["by_port"], dt["by_port"]), "by_impact": _add(ct["by_impact"], dt["by_impact"]),
        },
    }


def write(found: list[dict], human: dict | None = None) -> tuple[dict, int]:
    """Write the list and the summary. Returns (summary, number of problems in the repeat markings)."""
    _, problems = distinct(found)
    for p in problems:
        log(f"[missing] {p}")
    write_jsonl(paths.OUT / "missing.jsonl", found)
    s = _rounded(summarize(found, human))
    write_json(paths.OUT / "summary.json", s)
    c, ct, dt = s["confirmed"], s["candidates"]["total"], s["diverged"]["total"]
    log(f"[missing] {ct['candidates']} candidates, {ct['read']} read, {ct['missing']} flagged"
        + ("" if ct["unread"] == 0 else f" ({ct['unread']} unread; scaled up, about {ct['est'] or 0:.0f} in total)")
        + f"; {dt['diverged']} diverged pairs, {dt['read']} sampled and read, {dt['same_bug']} flagged, about "
        f"{dt['est']:.0f} in total; {c['total']} flagged in all, {c['distinct']['total']} distinct problems")
    return s, len(problems)


def consistency(s: dict) -> list[str]:
    """The numbers in the summary must agree with each other. Returns the disagreements (empty when all agree)."""
    p: list[str] = []

    def eq(name: str, a, b) -> None:
        if a != b:
            p.append(f"{name}: {a} != {b}")

    f = s["funnel"]
    for n, x in f.items():
        eq(f"{n} checked = sum of the outcomes", x["checked"],
           x["same_commit"] + x["has_fix"] + x["candidate"] + x["diverged"] + x["delete_only"])
    for k in f["total"]:
        eq(f"total {k} = sum over codebases", f["total"][k], sum(f[n][k] for n in ORDER))
    ca, dv, c = s["candidates"], s["diverged"], s["confirmed"]
    for n in (*ORDER, "total"):
        x = ca[n]
        eq(f"{n} candidates = screened", x["candidates"], f[n]["candidate"])
        eq(f"{n} candidates = read + unread", x["candidates"], x["read"] + x["unread"])
        eq(f"{n} read = sum of the verdicts", x["read"], sum(x["labels"].values()))
        eq(f"{n} flagged", x["missing"], x["labels"]["missing"])
        eq(f"{n} diverged = screened", dv[n]["diverged"], f[n]["diverged"])
    for n in ORDER:
        eq(f"{n} sampled = sum over groups", dv[n]["read"], sum(b["read"] for b in dv[n]["buckets"].values()))
        eq(f"{n} flagged in the sample = sum over groups", dv[n]["same_bug"], sum(b["same_bug"] for b in dv[n]["buckets"].values()))
    eq("flagged = from candidates + from the sample", c["total"], c["from_candidates"] + c["from_diverged"])
    eq("flagged from candidates", c["from_candidates"], ca["total"]["missing"])
    eq("flagged from the sample", c["from_diverged"], dv["total"]["same_bug"])
    for k in ("by_codebase", "by_port", "by_impact", "by_confidence"):
        eq(f"flagged = sum of {k}", c["total"], sum(c[k].values()))
    d = c["distinct"]
    eq("distinct = flagged - repeats", d["total"], c["total"] - len(d["repeated"]))
    for k in ("by_codebase", "by_port", "by_impact"):
        eq(f"distinct = sum of {k}", d["total"], sum(d[k].values()))
    h = s.get("human_review") or {}
    if h:
        eq("human review: flagged by Claude Code", h["claude_missing"]["total"], c["total"])
        eq("human review: reviewed = sum of the outcomes", h["reviewed"], sum(h["final"].values()))
        cm = h["claude_missing"]
        eq("human review: reviewed among flagged = sum of the outcomes", cm["reviewed"],
           sum(v for k, v in cm.items() if k.startswith("human_")))
        eq("human review: reviewed = among flagged + others", h["reviewed"], cm["reviewed"] + h["claude_other_reviewed"])
    return p
