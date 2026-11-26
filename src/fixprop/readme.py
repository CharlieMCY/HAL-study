"""The parts of the documents that are generated from the results.

  README.md               script    step 2, the numbers computed by the scripts
                          model     step 3, the results of the model reading the code; TODO until pairs are read
                          human     step 4, the numbers set by human review; TODO until review starts
  judgments/results.md    results   a summary of the results
                          list      every pair flagged as a missed fix, with the human review verdict

Each part sits between <!-- BEGIN:name --> and <!-- END:name -->. `python -m fixprop all` replaces them with the latest
results and `check` verifies that they match. Do not edit these parts by hand; the rest of each document is hand-written.

Wording: before human review, the model's verdicts are "flagged as a missed fix"; only pairs confirmed by human review
are "confirmed by human review".
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .codebases import ORDER
from .common import read_json, read_jsonl

NAMES = {"rust": "Rust HAL", "nuttx": "NuttX", "zephyr": "Zephyr", "rtthread": "RT-Thread"}
HUMAN = {"missing": "confirmed", "not_missing": "not a missed fix", "unsure": "unsure", "conflict": "reviewers disagree"}
REASON = {"not_a_fix": "not a bug fix", "not_applicable": "does not apply", "fixed_differently": "handled differently",
          "other": "other"}
HEAD = ["| | " + " | ".join(NAMES[n] for n in ORDER) + " | Total |", "|---|---|---|---|---|---|"]


def _row(label: str, cells: list[str], total: str) -> str:
    return f"| {label} | " + " | ".join(cells) + f" | {total} |"


def _rows(spec: list[tuple]) -> list[str]:
    """Each row of spec is (label, getter); the getter takes a codebase name or "total"."""
    return [_row(name, [f"{get(n):,}" for n in ORDER], f"{get('total'):,}") for name, get in spec]


def _review_done(h: dict) -> bool:
    fl = h.get("flagged", {})
    return bool(h.get("reviewed")) and fl.get("reviewed", 0) >= fl.get("total", 0) and not h.get("final", {}).get("conflict")


def _progress(s: dict) -> tuple[int, int, int, int]:
    """(candidates, candidates read, sampled diverged pairs, sampled diverged pairs read)."""
    ct, dt = s["candidates"]["total"], s["diverged"]["total"]
    return ct["candidates"], ct["read"], dt["sampled"], dt["read"]


def _todo(s: dict) -> str:
    nc, _, nd, _ = _progress(s)
    return f"**TODO.** No pair has been read yet: {nc:,} candidates and {nd} sampled diverged pairs are waiting."


# ------------------------------------------------------------------ README.md


def script_block(s: dict) -> str:
    """Step 2: the numbers computed by the scripts."""
    f = s["funnel"]

    def get(k: str):
        return lambda n: f[n][k]

    return "\n".join(HEAD + _rows([
        ("Repositories", get("repos")),
        ("Source files analysed", get("source_files")),
        ("Drivers", get("drivers")),
        ("Copied file pairs", get("copied_file_pairs")),
        ("Bug-fix commits", get("fix_commits")),
        ("— that changed a file with a copy", get("fix_commits_on_copied_files")),
        ("Fix–copy pairs", get("checked")),
        ("— changed together", get("same_commit")),
        ("— already fixed", get("has_fix")),
        ("— **not changed yet (candidates)**", get("candidate")),
        ("— diverged", get("diverged")),
        ("Bug-fix commits the candidates come from", get("candidate_fix_commits")),
        ("Candidates where the fixed driver has changed the fixed code since", get("candidate_source_rewritten")),
    ]))


def _reading_text(rd: dict) -> str:
    """Which model read the pairs, with which settings, and the tokens the conversations used."""
    runs = []
    for x in rd["settings"]:
        opts = [f"reasoning effort {x['effort']}" if x["effort"] else "reasoning effort not set",
                f"output limit {x['max_tokens']:,} tokens" if x["max_tokens"] else "",
                f"provider {x['provider']}" if x["provider"] else "any provider"]
        runs.append(f"`{x['model']}` ({', '.join(o for o in opts if o)}; {x['conversations']:,} conversations)")
    return (f"Read by {'; '.join(runs)}. The {rd['conversations']:,} conversations made {rd['calls']:,} API calls and used "
            f"{rd['prompt_tokens']:,} prompt tokens, {rd['cached_tokens']:,} of them read from the cache, and "
            f"{rd['completion_tokens']:,} completion tokens, at a cost of ${rd['cost']:,.2f}.")


def model_block(s: dict) -> str:
    """Step 3: the results of the model reading the code. TODO until pairs have been read."""
    nc, rc, nd, rd = _progress(s)
    if rc == 0 and rd == 0:
        return _todo(s)
    ca, dv, c = s["candidates"], s["diverged"], s["flagged"]
    d, dt = c["distinct"], dv["total"]

    def label(k: str):
        return lambda n: ca[n]["labels"][k]

    rows = _rows([
        ("Candidates read", lambda n: ca[n]["read"]),
        ("— **flagged as a missed fix**", lambda n: ca[n]["missing"]),
        ("— not a bug fix", label("not_a_fix")),
        ("— fix does not apply to the copy", label("not_applicable")),
        ("— copy already handles it differently", label("fixed_differently")),
        ("— unsure", label("unsure")),
        ("Sampled diverged pairs read", lambda n: dv[n]["read"]),
        ("— flagged as a missed fix", lambda n: dv[n]["same_bug"]),
        ("**Flagged as missed fixes, total**", lambda n: c["total"] if n == "total" else c["by_codebase"][n]),
        ("**Distinct problems** (same problem reached from two fixes counted once)",
         lambda n: d["total"] if n == "total" else d["by_codebase"][n]),
    ])
    L = []
    if rc < nc or rd < nd:
        L += [f"**In progress.** {rc:,} of the {nc:,} candidates and {rd} of the {nd} sampled diverged pairs have been read "
              "so far.", ""]
    L += HEAD + rows
    if d["total"]:
        L += ["",
              f"Of the {d['total']} distinct problems, {d['by_impact']['runtime']} affect runtime behaviour and "
              f"{d['by_impact']['light']} affect only builds, debug or test commands, or log text. For "
              f"{d['by_port']['verbatim']} the original patch ports as is; {d['by_port']['adapt']} need it adapted to the "
              "copy's code."]
    if c["total"]:
        L += ["", f"The {c['total']} flagged pairs come from {c['fix_commits']} bug-fix commits."]
    L += ["",
          f"The {nd} sampled diverged pairs were drawn by group from the {dt['diverged'] - dt['not_covered']:,} diverged pairs "
          f"whose changed function could be identified (of {dt['diverged']:,}). The sample is too small to say how many "
          "missed fixes the diverged pairs hold, so no total is derived from it.",
          "", _reading_text(s["reading"])]
    return "\n".join(L)


def human_block(s: dict, rows: list[dict], final: list[dict]) -> str:
    """Step 4: the numbers set by human review. TODO until review starts."""
    c = s["flagged"]
    d = c["distinct"]
    h = s.get("human_review") or {}
    fl = h.get("flagged", {})
    _, rc, _, rd = _progress(s)
    if rc == 0 and rd == 0:
        model = ["TODO"] * 4 + ["—"]
    else:
        model = [f"{c['total']:,}", f"{d['total']:,}", f"{d['by_impact']['runtime']:,}", f"{d['by_port']['verbatim']:,}", "—"]
    if not h.get("reviewed"):
        human = ["TODO"] * 5
        status = (f"Reviewed so far: 0 of {c['total']} flagged pairs." if c["total"]
                  else "Review has not started: step 3 has not flagged any pair yet.")
    else:
        byid = {r["id"]: r for r in rows}
        conf = {x["id"] for x in final if x["final"] == "missing"}
        uniq = [byid[i] for i in sorted(conf) if i in byid and byid[i].get("same_problem_as") not in conf]
        mark = "" if _review_done(h) else " (in progress)"
        human = [f"{len(conf):,}{mark}", f"{h.get('final_missing_distinct', 0):,}{mark}",
                 f"{sum(1 for r in uniq if r.get('impact') == 'runtime'):,}{mark}",
                 f"{sum(1 for r in uniq if r.get('port') == 'verbatim'):,}{mark}",
                 f"{fl.get('human_missing', 0)} of {fl.get('reviewed', 0)} reviewed{mark}"]
        status = (f"Reviewed so far: {fl.get('reviewed', 0)} of {c['total']} flagged pairs"
                  + (f"; reviewers disagree on {fl['human_conflict']}" if fl.get("human_conflict") else "")
                  + (f"; plus {h['unflagged_reviewed']} pairs the model did not flag, "
                     f"{h['unflagged_human_missing']} of them confirmed as missed fixes" if h.get("unflagged_reviewed") else "")
                  + ".")
    labels = ["Fix–copy pairs that are missed fixes", "Distinct problems", "Distinct problems that affect runtime behaviour",
              "Distinct problems whose patch ports as is", "Flagged pairs confirmed by reviewers"]
    return "\n".join(["| | LLM (step 3) | Human review |", "|---|---|---|"]
                     + [f"| {a} | {b} | {x} |" for a, b, x in zip(labels, model, human)] + ["", status])


# ------------------------------------------------------------------ judgments/results.md


def _human_status(h: dict) -> tuple[str, str]:
    """(human review status, final benchmark status)."""
    fl = h.get("flagged", {})
    total, done = fl.get("total", 0), fl.get("reviewed", 0)
    if not h.get("reviewed"):
        return ("**TODO**, not started" + (f" (0 of {total})" if total else ""),
                "**TODO**; it will hold only the pairs confirmed by human review")
    fin = h.get("final", {})
    s = (f"{'complete' if _review_done(h) else 'in progress'}: {done} of the {total} flagged pairs reviewed; "
         f"{fl.get('human_missing', 0)} confirmed, {fl.get('human_not_missing', 0)} not a missed fix, "
         f"{fl.get('human_unsure', 0)} unsure" + (f", {fl.get('human_conflict', 0)} with reviewers disagreeing"
                                                 if fl.get("human_conflict") else ""))
    if h.get("unflagged_reviewed"):
        s += (f"; also {h['unflagged_reviewed']} pairs the model did not flag, {h['unflagged_human_missing']} of "
              f"them confirmed as missed fixes")
    b = f"{fin.get('missing', 0)} pairs confirmed by human review ({h.get('final_missing_distinct', 0)} distinct problems)"
    b += "" if _review_done(h) else "; review is not finished"
    return s, b


def results_block(s: dict) -> str:
    """A summary of the results in a few sentences."""
    f, c, ca, dv = s["funnel"], s["flagged"], s["candidates"], s["diverged"]
    ct, dt, d = ca["total"], dv["total"], c["distinct"]
    hs, hb = _human_status(s.get("human_review") or {})
    last = f"- Human review: {hs}. Final benchmark: {hb}."
    nc, rc, nd, rd = _progress(s)
    if rc == 0 and rd == 0:
        return "\n".join(["- " + _todo(s), last])
    if ct["unread"]:
        first = (f"- **{rc:,} of the {nc:,} candidates have been read so far; the model flagged {ct['missing']} as missed "
                 "fixes.**")
    else:
        first = f"- **All {nc:,} candidates were read; the model flagged {ct['missing']} as missed fixes.**"
    if rd < nd:
        second = (f"- Of the {f['total']['diverged']:,} diverged pairs, {nd} were sampled; {rd} of them have been read so far, "
                  f"and {dt['same_bug']} were flagged.")
    else:
        second = (f"- Of the {f['total']['diverged']:,} diverged pairs, {nd} were sampled and read, and {dt['same_bug']} were "
                  f"flagged. {dt['not_covered']} of the diverged pairs have no identifiable changed function and were not "
                  "sampled. The sample is too small to say how many missed fixes the diverged pairs hold, so no total is "
                  "derived from it.")
    third = (f"- In total the model flagged {c['total']} pairs, from {c['fix_commits']} bug-fix commits. {len(d['repeated'])} of them "
             f"{'reaches' if len(d['repeated']) == 1 else 'reach'} a problem already reached "
             f"from an earlier fix, which leaves **{d['total']} distinct problems**: {d['by_impact']['runtime']} affect "
             f"runtime behaviour and {d['by_impact']['light']} affect only builds, debug or test commands, or log text; for "
             f"{d['by_port']['verbatim']} the patch ports as is and {d['by_port']['adapt']} need it adapted.")
    return "\n".join([first, second, third, "- " + _reading_text(s["reading"]), last])


def _human_cell(x: dict | None) -> str:
    if x is None:
        return "not reviewed"
    if x["final"] == "not_missing":
        reasons = sorted({REASON.get(r["reason"], r["reason"] or "") for r in x["reviews"] if r.get("reason")})
        return "not a missed fix" + (f" ({', '.join(reasons)})" if reasons else "")
    return HUMAN.get(x["final"], x["final"])


def list_block(rows: list[dict], final: list[dict]) -> str:
    """Every flagged pair, one table per codebase."""
    if not rows:
        return "No pair has been flagged yet."
    short = lambda repo: repo.split("/")[1]  # noqa: E731
    human = {x["id"]: x for x in final}
    L = []
    for n in ORDER:
        mine = [r for r in rows if r["codebase"] == n]
        if not mine:
            continue
        L += [f"### {NAMES[n]} ({len(mine)} pair{'' if len(mine) == 1 else 's'})", "",
              "| ID | Fixed in | Copy | Problem | Patch | Impact | Confidence | Human review |",
              "|---|---|---|---|---|---|---|---|"]
        for r in mine:
            fx, cp = r["fix"], r["copy"]
            name = cp["file"] if cp["repo"] == fx["repo"] else f"{short(cp['repo'])}: {cp['file']}"
            L.append(f"| {r['id']} | {short(fx['repo'])} [{fx['commit'][:10]}]({fx['url']}), {fx['date'][:7]} | "
                     f"[{name}:{cp['line']}]({cp['url']}) | {r['title'].replace('|', chr(92) + '|')}"
                     + (f" (same problem as {r['same_problem_as']})" if r.get("same_problem_as") else "") + " | "
                     f"{'ports as is' if r['port'] == 'verbatim' else 'adapt'} | "
                     f"{'runtime' if r['impact'] == 'runtime' else 'minor'} | {r['confidence']} | "
                     f"{_human_cell(human.get(r['id']))} |")
        L.append("")
    return "\n".join(L).rstrip()


# ------------------------------------------------------------------ writing and checking the documents


def blocks(out: Path) -> dict[str, dict[str, str]]:
    """The generated parts of every document: {document: {part name: content}}."""
    s = read_json(out / "summary.json")
    rows, final = read_jsonl(out / "missing.jsonl"), read_jsonl(out / "final.jsonl")
    return {"README.md": {"script": script_block(s), "model": model_block(s), "human": human_block(s, rows, final)},
            "judgments/results.md": {"results": results_block(s), "list": list_block(rows, final)}}


def _split(text: str, name: str) -> tuple[str, str, str] | None:
    a, b = f"<!-- BEGIN:{name} -->", f"<!-- END:{name} -->"
    if a not in text or b not in text:
        return None
    head, rest = text.split(a, 1)
    body, tail = rest.split(b, 1)
    return head + a, body, b + tail


def sync(out: Path) -> list[str]:
    """Replace the generated parts of the documents with the latest results. Returns the files changed."""
    changed = []
    for rel, parts in blocks(out).items():
        f = paths.ROOT / rel
        if not f.is_file():
            continue
        text = old = f.read_text(encoding="utf-8")
        for name, body in parts.items():
            sp = _split(text, name)
            if sp:
                text = sp[0] + "\n" + body + "\n" + sp[2]
        if text != old:
            f.write_text(text, encoding="utf-8")
            changed.append(rel)
    return changed


def stale(out: Path) -> list[str]:
    """Where the generated parts of the documents do not match the results in out."""
    if not (out / "summary.json").is_file():
        return []
    problems = []
    for rel, parts in blocks(out).items():
        f = paths.ROOT / rel
        if not f.is_file():
            problems.append(f"{rel} does not exist")
            continue
        text = f.read_text(encoding="utf-8")
        for name, body in parts.items():
            sp = _split(text, name)
            if sp is None:
                problems.append(f"{rel} has no <!-- BEGIN:{name} --> part")
            elif sp[1].strip("\n") != body:
                problems.append(f"the {name} part of {rel} does not match the results (python -m fixprop all updates it)")
    return problems
