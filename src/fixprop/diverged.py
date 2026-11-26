"""Step 5: group the diverged pairs, sample them, and send the sample to be read.

For these fixes the copy has neither the lines from before the fix nor the lines from after it, so the script cannot
tell what happened. Missed fixes can hide here too (the same problem written differently), so a sample is read as well.

The pairs are grouped by structure first, and each group is sampled separately (the share of missed fixes can differ a
lot between groups):
  near     the fixed function exists in the copy and most of the code around the change is the same; only the changed
           lines do not match
  far      the fixed function exists in the copy, but most of the code around the change is different
  gone     the fixed function cannot be found in the copy's file
  unknown  the changed function cannot be identified. Not sampled

Output benchmark/<codebase>/diverged.jsonl: one row per (bug-fix commit, other driver), with its group.
The judgments of the sampled pairs are in judgments/<codebase>/labels_diverged.jsonl; labels.py merges them into
diverged_sample.jsonl.
"""

from __future__ import annotations

import random
import re
from collections import Counter

from . import gitio, hunks, pairs
from .census import Judge, out_dir, unit_level
from .codebases import Codebase
from .common import log, read_jsonl, write_jsonl
from .candidates import blob_url, commit_url

SEED = 20261124
NEAR = 0.5  # more than half of the code around the change (context plus removed lines) is in the copy: "near"
ALLOC = {"near": 4, "far": 3, "gone": 3}  # how many to sample per group in each codebase
MAX_FILES, MAX_HUNKS = 2, 4

_C_KEYWORDS = ("if", "for", "while", "switch", "return", "else", "do", "typedef", "sizeof", "case", "goto")
_C_FUNC = re.compile(r"^[A-Za-z_][^;{}=#]*?\b([A-Za-z_]\w*)\s*\([^;]*$")
_RS_FUNC = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
                      r"(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)")


def enclosing_function(old_lines: list[str], line_no: int, lang: str) -> str:
    """The name of the function a change is in: the nearest function definition above the first changed line.
    An empty string if there is none."""
    for i in range(min(line_no, len(old_lines)) - 1, -1, -1):
        ln = old_lines[i]
        if lang == "rust":
            m = _RS_FUNC.match(ln)
            if m:
                return m.group(1)
            continue
        if not ln[:1].isalpha() and ln[:1] != "_":
            continue  # C: only lines starting in the first column
        m = _C_FUNC.match(ln)
        if m and m.group(1) not in _C_KEYWORDS and ln.split("(")[0].split()[0] not in _C_KEYWORDS:
            return m.group(1)
    return ""


def first_change_line(h: hunks.Hunk) -> int:
    n = h.old_start
    for tag, _ in h.lines:
        if tag != " ":
            break
        n += 1
    return n


def analyse(cb: Codebase, files=None) -> list[dict]:
    files = files or pairs.load_files(cb)
    rows = read_jsonl(out_dir(cb) / "fix_status.jsonl")
    agg = unit_level(rows)
    judge = Judge(cb, files)
    blob_cache: dict[tuple[str, str], list[str]] = {}
    out = []
    for key, a in sorted(agg.items()):
        if a["status"] != "diverged":
            continue
        best = {"overlap": -1.0}
        funcs_found, funcs_all, n_hunks = set(), set(), 0
        for r in a["rows"]:
            if r["status"] != "diverged":
                continue
            repo, gitdir = r["repo"], gitio.git_dir(r["repo"])
            bk = (repo, r["blobs"][0])
            if bk not in blob_cache:
                blob = gitio.cat_blobs(gitdir, [r["blobs"][0]]).get(r["blobs"][0], b"")
                blob_cache[bk] = blob.decode("utf-8", "replace").split("\n")
            old_lines = blob_cache[bk]
            hs = hunks.parse_diff(gitio.diff_blobs(gitdir, r["blobs"][0], r["blobs"][1]))
            norm = judge.norm(repo, r["source"])
            tgt = (r["target_repo"], r["target"])
            code, view = judge.view(tgt)
            text = "\n".join(code)
            for item in r["hunks"]:
                if item["status"] != "diverged":
                    continue
                h = hs[item["i"]]
                items = hunks.clean(h, norm)
                pre = [s for tag, s in items if tag in (" ", "=-", "-") and cb.valid_change(s)]
                overlap = sum(1 for s in pre if s in view.all) / len(pre) if pre else 0.0
                fn = enclosing_function(old_lines, first_change_line(h), cb.lang)
                fn_norm = norm(fn) if fn else ""
                has_fn = bool(fn_norm) and re.search(r"(?<![A-Za-z0-9_])" + re.escape(fn_norm) + r"\s*[(<]", text) is not None
                n_hunks += 1
                if fn:
                    funcs_all.add(fn)
                    if has_fn:
                        funcs_found.add(fn)
                score = (has_fn, overlap)
                if score > (best.get("has_fn", False), best["overlap"]):
                    best = {"overlap": overlap, "has_fn": has_fn, "fn": fn, "source": r["source"],
                            "target": r["target"], "hunk": item["i"], "kind": item["kind"]}
        if n_hunks == 0:
            continue
        if not best.get("has_fn"):
            bucket = "gone" if funcs_all else "unknown"
        else:
            bucket = "near" if best["overlap"] >= NEAR else "far"
        r0 = a["rows"][0]
        out.append({
            "codebase": cb.name, "repo": r0["repo"], "commit": key[0], "date": r0["date"], "subject": r0["subject"],
            "target_repo": key[1], "target_unit": key[2], "bucket": bucket, "hunks": n_hunks,
            "functions": sorted(funcs_all)[:8], "functions_found": sorted(funcs_found)[:8],
            "best": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in best.items()},
        })
    write_jsonl(out_dir(cb) / "diverged.jsonl", out)
    c = Counter(r["bucket"] for r in out)
    log(f"[diverged] {cb.name}: {len(out)} diverged pairs; near {c['near']}, far {c['far']}, gone {c['gone']}, "
        f"function not identified {c['unknown']}")
    return out


def _find_line(lines: list[str], name: str) -> int:
    rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"\s*[(<]")
    for i, ln in enumerate(lines, 1):
        if rx.search(ln):
            return i
    return 0


def sample(cb: Codebase, files=None) -> list[dict]:
    """Draw the sample and return a record per sampled pair (before judgments are merged). The sample is deterministic:
    each group is sorted first, then drawn with a fixed seed."""
    files = files or pairs.load_files(cb)
    div = read_jsonl(out_dir(cb) / "diverged.jsonl")
    agg = unit_level(read_jsonl(out_dir(cb) / "fix_status.jsonl"))
    judge = Judge(cb, files)
    rng = random.Random(SEED)
    picked = []
    for bucket, n in ALLOC.items():
        pool = sorted((r for r in div if r["bucket"] == bucket), key=lambda r: (r["commit"], r["target_unit"]))
        picked += rng.sample(pool, min(n, len(pool)))
    recs = []
    for i, d in enumerate(picked, 1):
        key = (d["commit"], d["target_repo"], d["target_unit"])
        rows = [r for r in agg[key]["rows"] if r["status"] == "diverged"]
        rows.sort(key=lambda r: (r["target"] != d["best"].get("target"), r["source"]))
        repo = d["repo"]
        gitdir = gitio.git_dir(repo)
        rec = {"id": f"{cb.name}-D{i:02d}", "codebase": cb.name, "repo": repo, "commit": d["commit"],
               "commit_url": commit_url(repo, d["commit"]), "date": d["date"], "subject": d["subject"],
               "target_repo": d["target_repo"], "target_unit": d["target_unit"], "bucket": d["bucket"], "files": []}
        for r in rows[:MAX_FILES]:
            code, _ = judge.view((r["target_repo"], r["target"]))
            norm = judge.norm(repo, r["source"])
            old = gitio.cat_blobs(gitdir, [r["blobs"][0]]).get(r["blobs"][0], b"").decode("utf-8", "replace").split("\n")
            hs = hunks.parse_diff(gitio.diff_blobs(gitdir, r["blobs"][0], r["blobs"][1]))
            dh = [x for x in r["hunks"] if x["status"] == "diverged"]
            dh.sort(key=lambda x: x["i"] != d["best"].get("hunk"))
            frec = {"source": r["source"], "source_now": r["source_now"], "target": r["target"], "hunks": []}
            for x in dh[:MAX_HUNKS]:
                fn = enclosing_function(old, first_change_line(hs[x["i"]]), cb.lang)
                where = _find_line(code, norm(fn)) if fn else 0
                frec["hunks"].append({"old_start": x["old_start"], "kind": x["kind"], "function": fn, "target_line": where})
            lines_ = [hk["target_line"] for hk in frec["hunks"] if hk["target_line"]]
            frec["target_url"] = blob_url(r["target_repo"], r["target_commit"], r["target"], lines_)
            rec["files"].append(frec)
        recs.append(rec)
    log(f"[diverged] {cb.name}: sampled {len(recs)} ({dict(Counter(r['bucket'] for r in recs))}, seed {SEED})")
    return recs
