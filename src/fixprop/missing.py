"""Step 6: list the pairs flagged as missed fixes after reading the code, and count them.

A missed fix: a bug was fixed in one driver, and another driver with a copy of the code has the same problem and has not
been fixed as of the pinned commit. The pairs here are flagged by the model; the pairs confirmed by human review are in
benchmark/final.jsonl (see review.py).

Output
  benchmark/missing.jsonl   one record per flagged pair: what was fixed, where the copy is, the hunks, the copy's current
                            code, whether the patch ports as is, impact, evidence, reader
  benchmark/summary.json    the counts of every step; the flagged pairs; the progress of human review

Unpropagated fixes come in two parts:
  candidates      every one is read, so the number flagged among them is a count.
  diverged pairs  too many to read. A few from each group are read (see diverged.py). They show what the text
                  comparison cannot decide; the sample is too small to say how many missed fixes the diverged pairs hold,
                  so nothing is scaled up from it.
"""

from __future__ import annotations

from collections import Counter

from . import gitio, hunks, paths
from .candidates import blob_url
from .census import out_dir, unit_level
from .codebases import ORDER, Codebase, get
from .common import log, read_json, read_jsonl, write_json, write_jsonl
from .diverged import ALLOC
from .labels import IMPACT, LABELS, PORT, USAGE

MAX_FILES, MAX_CHANGES = 4, 8  # at most this many files per record, and this many hunks per file


# ------------------------------------------------------------------ counts


def _by(recs: list[dict], field: str, values: tuple[str, ...]) -> dict:
    c = Counter(r.get(field) for r in recs)
    return {v: c[v] for v in values}


def count_candidates(name: str) -> dict:
    """The candidates: how many were read, how many got each verdict, how many were flagged."""
    recs = read_jsonl(out_dir(name) / "candidates.jsonl")
    read = [r for r in recs if r.get("label")]
    real = [r for r in read if r["label"] == "missing"]
    return {
        "candidates": len(recs), "read": len(read), "unread": len(recs) - len(read),
        "labels": _by(read, "label", LABELS), "missing": len(real),
        "by_port": _by(real, "port", PORT), "by_impact": _by(real, "impact", IMPACT),
    }


def count_diverged(name: str) -> dict:
    """The diverged pairs: how many there are in each group, how many were sampled and read, how many were flagged."""
    d = out_dir(name)
    pop = Counter(r["bucket"] for r in read_jsonl(d / "diverged.jsonl"))
    sample = read_jsonl(d / "diverged_sample.jsonl")
    labs = [r for r in sample if r.get("label")]
    real = [r for r in labs if r["label"] == "same_bug"]
    return {
        "diverged": sum(pop.values()), "not_covered": pop["unknown"], "sampled": len(sample), "read": len(labs),
        "same_bug": len(real),
        "buckets": {b: {"total": pop[b], "sampled": sum(1 for r in sample if r["bucket"] == b),
                        "read": sum(1 for r in labs if r["bucket"] == b),
                        "same_bug": sum(1 for r in real if r["bucket"] == b)} for b in ALLOC},
        "by_port": _by(real, "port", PORT), "by_impact": _by(real, "impact", IMPACT),
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
        sel = [h for h in row["hunks"] if h["status"] == kind]
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
    for k in ("impact_note", "same_problem_as", "same_fix", "transcript", "transcript_sha256"):
        if r.get(k):
            rec[k] = r[k]
    return rec


def distinct(found: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop the pairs that repeat a problem already counted from another fix (marked by labels.mark_repeats). Returns
    (distinct problems, problems). same_problem_as must point at another flagged pair that is not itself a repeat."""
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


def reading() -> dict:
    """The conversations the judgments came from: which model read them with which settings, and the tokens they used.
    Taken from the stored transcripts (labels.check_transcript)."""
    recs = [r for n in ORDER for f in ("candidates.jsonl", "diverged_sample.jsonl")
            for r in read_jsonl(out_dir(n) / f) if r.get("label")]
    runs = Counter((r.get("reader") or "", (r.get("reading") or {}).get("effort") or "",
                    (r.get("reading") or {}).get("max_tokens") or 0, (r.get("reading") or {}).get("provider") or "")
                   for r in recs)
    return {
        "conversations": len(recs),
        "settings": [{"model": m, "effort": e or None, "max_tokens": t or None, "provider": p or None, "conversations": n}
                     for (m, e, t, p), n in sorted(runs.items())],
        **{k: sum((r.get("reading") or {}).get(k) or 0 for r in recs) for k in USAGE},
    }


def _rounded(x):
    """Costs are sums of fractions; they are written with two decimals."""
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v) for v in x]
    return x


NOTES = ("Every count is of (bug-fix commit, other driver) pairs. funnel: the outcomes of the script analysis. "
         "candidates: how many were read and what the verdicts were. diverged: how many pairs each group holds, and how "
         "many of the sampled ones were read and flagged; nothing is scaled up from the sample. flagged: the pairs the "
         "model flagged as missed fixes after reading the code. reading: the conversations the verdicts came from. "
         "human_review: the progress and outcome of human review.")


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
            "revert_commits_left_out": s["revert_commits_left_out"],
            "checked": s["checked_pairs"], **s["pair_status"],
            "candidate_fix_commits": s["candidate_commits"],
            "candidate_source_rewritten": s["candidate_pairs_not_still_fixed"],
        }
        cand[name], div[name] = count_candidates(name), count_diverged(name)
    funnel["total"] = {k: sum(funnel[n][k] for n in ORDER) for k in funnel[ORDER[0]]}

    def total(parts: dict, skip: tuple[str, ...]) -> dict:
        first = parts[ORDER[0]]
        t: dict = {}
        for k, v in first.items():
            if k in skip:
                continue
            if isinstance(v, dict):
                t[k] = {kk: sum(parts[n][k][kk] for n in ORDER) for kk in v}
            else:
                t[k] = sum(parts[n][k] for n in ORDER)
        return t

    ct, dt = total(cand, ()), total(div, ("buckets",))
    by = lambda f, vals: {v: sum(1 for r in found if r.get(f) == v) for v in vals}  # noqa: E731
    uniq, _ = distinct(found)
    return {
        "notes": NOTES,
        "funnel": funnel,
        "flagged": {
            "total": len(found), "fix_commits": len({(r["fix"]["repo"], r["fix"]["commit"]) for r in found}),
            "from_candidates": sum(1 for r in found if r["found_in"] == "candidate"),
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
        "reading": reading(),
        "human_review": human or {},
    }


def write(found: list[dict], human: dict | None = None) -> tuple[dict, int]:
    """Write the list and the summary. Returns (summary, number of problems in the repeat markings)."""
    _, problems = distinct(found)
    for p in problems:
        log(f"[missing] {p}")
    write_jsonl(paths.OUT / "missing.jsonl", found)
    s = _rounded(summarize(found, human))
    write_json(paths.OUT / "summary.json", s)
    c, ct, dt = s["flagged"], s["candidates"]["total"], s["diverged"]["total"]
    log(f"[missing] {ct['candidates']} candidates, {ct['read']} read, {ct['missing']} flagged; {dt['diverged']} diverged "
        f"pairs, {dt['sampled']} sampled, {dt['read']} read, {dt['same_bug']} flagged; {c['total']} flagged in all, from "
        f"{c['fix_commits']} bug-fix commits, {c['distinct']['total']} distinct problems")
    return s, len(problems)


def consistency(s: dict) -> list[str]:
    """The numbers in the summary must agree with each other. Returns the disagreements (empty when all agree)."""
    p: list[str] = []

    def eq(name: str, a, b) -> None:
        if a != b:
            p.append(f"{name}: {a} != {b}")

    f = s["funnel"]
    for n, x in f.items():
        eq(f"{n} checked = sum of the outcomes", x["checked"], x["same_commit"] + x["has_fix"] + x["candidate"] + x["diverged"])
    for k in f["total"]:
        eq(f"total {k} = sum over codebases", f["total"][k], sum(f[n][k] for n in ORDER))
    ca, dv, c = s["candidates"], s["diverged"], s["flagged"]
    for n in (*ORDER, "total"):
        x = ca[n]
        eq(f"{n} candidates = screened", x["candidates"], f[n]["candidate"])
        eq(f"{n} candidates = read + unread", x["candidates"], x["read"] + x["unread"])
        eq(f"{n} read = sum of the verdicts", x["read"], sum(x["labels"].values()))
        eq(f"{n} flagged", x["missing"], x["labels"]["missing"])
        eq(f"{n} diverged = screened", dv[n]["diverged"], f[n]["diverged"])
        if dv[n]["read"] > dv[n]["sampled"]:
            p.append(f"{n} more sampled diverged pairs read than sampled: {dv[n]['read']} > {dv[n]['sampled']}")
    for n in ORDER:
        eq(f"{n} sampled = sum over groups", dv[n]["sampled"], sum(b["sampled"] for b in dv[n]["buckets"].values()))
        eq(f"{n} sampled and read = sum over groups", dv[n]["read"], sum(b["read"] for b in dv[n]["buckets"].values()))
        eq(f"{n} flagged in the sample = sum over groups", dv[n]["same_bug"], sum(b["same_bug"] for b in dv[n]["buckets"].values()))
    eq("conversations = pairs read", s["reading"]["conversations"], ca["total"]["read"] + dv["total"]["read"])
    eq("conversations = sum over the settings", s["reading"]["conversations"],
       sum(x["conversations"] for x in s["reading"]["settings"]))
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
        eq("human review: flagged by the model", h["flagged"]["total"], c["total"])
        eq("human review: reviewed = sum of the outcomes", h["reviewed"], sum(h["final"].values()))
        fl = h["flagged"]
        eq("human review: reviewed among flagged = sum of the outcomes", fl["reviewed"],
           sum(v for k, v in fl.items() if k.startswith("human_")))
        eq("human review: reviewed = among flagged + others", h["reviewed"], fl["reviewed"] + h["unflagged_reviewed"])
    return p
