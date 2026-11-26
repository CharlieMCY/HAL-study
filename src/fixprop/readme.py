"""The parts of the documents that are generated from the results.

  README.md               script    step 2, the numbers computed by the scripts
                          claude    step 3, the results of Claude Code reading the code
                          human     step 4, the numbers set by human review; TODO until review starts
  judgments/results.md    results   a summary of the results
                          list      every pair flagged as a missed fix, with the human review verdict

Each part sits between <!-- BEGIN:name --> and <!-- END:name -->. `python -m fixprop all` replaces them with the latest
results and `check` verifies that they match. Do not edit these parts by hand; the rest of each document is hand-written.

Wording: before human review, Claude Code's verdicts are "flagged as a missed fix"; only pairs confirmed by human review
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
    cm = h.get("claude_missing", {})
    return bool(h.get("reviewed")) and cm.get("reviewed", 0) >= cm.get("total", 0) and not h.get("final", {}).get("conflict")


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
        ("— fixed in the same commit", get("same_commit")),
        ("— copy already has the fixed code", get("has_fix")),
        ("— **candidate: copy still has the pre-fix code**", get("candidate")),
        ("— diverged", get("diverged")),
        ("— delete-only", get("delete_only")),
    ]))


def claude_block(s: dict) -> str:
    """Step 3: the results of Claude Code reading the code."""
    ca, dv, c = s["candidates"], s["diverged"], s["confirmed"]
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
        ("Diverged pairs sampled and read", lambda n: dv[n]["read"]),
        ("— flagged as a missed fix", lambda n: dv[n]["same_bug"]),
        ("**Flagged as missed fixes, total**", lambda n: c["total"] if n == "total" else c["by_codebase"][n]),
        ("**Distinct problems** (same problem reached from two fixes counted once)",
         lambda n: d["total"] if n == "total" else d["by_codebase"][n]),
    ])
    return "\n".join(HEAD + rows + [
        "",
        f"Of the {d['total']} distinct problems, {d['by_impact']['runtime']} affect runtime behaviour and "
        f"{d['by_impact']['light']} affect only builds, debug or test commands, or log text. For {d['by_port']['verbatim']} "
        f"the original patch ports as is; {d['by_port']['adapt']} need it adapted to the copy's code.",
        "",
        f"The {dt['read']} sampled diverged pairs were drawn by group from the {dt['diverged'] - dt['not_covered']:,} "
        f"diverged pairs whose changed function could be identified (of {dt['diverged']:,}). Scaling the sample up "
        f"by group gives about {dt['est']:.0f} missed fixes among those pairs; they are not part of the totals above."])


def human_block(s: dict, rows: list[dict], final: list[dict]) -> str:
    """Step 4: the numbers set by human review. TODO until review starts."""
    c = s["confirmed"]
    d = c["distinct"]
    h = s.get("human_review") or {}
    cm = h.get("claude_missing", {})
    claude = [f"{c['total']:,}", f"{d['total']:,}", f"{d['by_impact']['runtime']:,}", f"{d['by_port']['verbatim']:,}", "—"]
    if not h.get("reviewed"):
        human = ["TODO"] * 5
        status = f"Reviewed so far: 0 of {c['total']} flagged pairs."
    else:
        byid = {r["id"]: r for r in rows}
        conf = {x["id"] for x in final if x["final"] == "missing"}
        uniq = [byid[i] for i in sorted(conf) if i in byid and byid[i].get("same_problem_as") not in conf]
        mark = "" if _review_done(h) else " (in progress)"
        human = [f"{len(conf):,}{mark}", f"{h.get('final_missing_distinct', 0):,}{mark}",
                 f"{sum(1 for r in uniq if r.get('impact') == 'runtime'):,}{mark}",
                 f"{sum(1 for r in uniq if r.get('port') == 'verbatim'):,}{mark}",
                 f"{cm.get('human_missing', 0)} of {cm.get('reviewed', 0)} reviewed{mark}"]
        status = (f"Reviewed so far: {cm.get('reviewed', 0)} of {c['total']} flagged pairs"
                  + (f"; reviewers disagree on {cm['human_conflict']}" if cm.get("human_conflict") else "")
                  + (f"; plus {h['claude_other_reviewed']} pairs Claude Code did not flag, "
                     f"{h['claude_other_human_missing']} of them confirmed as missed fixes" if h.get("claude_other_reviewed") else "")
                  + ".")
    labels = ["Fix–copy pairs that are missed fixes", "Distinct problems", "Distinct problems that affect runtime behaviour",
              "Distinct problems whose patch ports as is", "Flagged pairs confirmed by reviewers"]
    return "\n".join(["| | Claude Code (step 3) | Human review |", "|---|---|---|"]
                     + [f"| {a} | {b} | {x} |" for a, b, x in zip(labels, claude, human)] + ["", status])


# ------------------------------------------------------------------ judgments/results.md


def _human_status(h: dict) -> tuple[str, str]:
    """(human review status, final benchmark status)."""
    cm = h.get("claude_missing", {})
    total, done = cm.get("total", 0), cm.get("reviewed", 0)
    if not h.get("reviewed"):
        return f"**TODO**, not started (0 of {total})", "**TODO**; it will hold only the pairs confirmed by human review"
    fin = h.get("final", {})
    s = (f"{'complete' if _review_done(h) else 'in progress'}: {done} of the {total} flagged pairs reviewed; "
         f"{cm.get('human_missing', 0)} confirmed, {cm.get('human_not_missing', 0)} not a missed fix, "
         f"{cm.get('human_unsure', 0)} unsure" + (f", {cm.get('human_conflict', 0)} with reviewers disagreeing"
                                                 if cm.get("human_conflict") else ""))
    if h.get("claude_other_reviewed"):
        s += (f"; also {h['claude_other_reviewed']} pairs Claude Code did not flag, {h['claude_other_human_missing']} of "
              f"them confirmed as missed fixes")
    b = f"{fin.get('missing', 0)} pairs confirmed by human review ({h.get('final_missing_distinct', 0)} distinct problems)"
    b += "" if _review_done(h) else "; review is not finished"
    return s, b


def results_block(s: dict) -> str:
    """A summary of the results in a few sentences."""
    f, c, ca, dv = s["funnel"], s["confirmed"], s["candidates"], s["diverged"]
    ct, dt, d = ca["total"], dv["total"], c["distinct"]
    if ct["unread"]:
        first = (f"- **{ct['read']:,} of the {ct['candidates']:,} candidates have been read so far; Claude Code flagged "
                 f"{ct['missing']} as missed fixes.**")
    else:
        first = (f"- **All {ct['candidates']:,} candidates were read; Claude Code flagged {ct['missing']} as missed fixes.** "
                 "This is a count, not an estimate.")
    hs, hb = _human_status(s.get("human_review") or {})
    return "\n".join([
        first,
        f"- Of the {f['total']['diverged']:,} diverged pairs, {dt['read']} were sampled and read, and {dt['same_bug']} were "
        f"flagged. {dt['not_covered']} of the diverged pairs have no identifiable changed function and were not sampled; "
        f"scaling the sample up gives about {dt['est']:.0f} missed fixes among the other "
        f"{f['total']['diverged'] - dt['not_covered']:,}. This estimate is not part of the totals.",
        f"- In total Claude Code flagged {c['total']} pairs. {len(d['repeated'])} of them reach a problem already reached "
        f"from another fix, which leaves **{d['total']} distinct problems**: {d['by_impact']['runtime']} affect runtime "
        f"behaviour and {d['by_impact']['light']} affect only builds, debug or test commands, or log text; for "
        f"{d['by_port']['verbatim']} the patch ports as is and {d['by_port']['adapt']} need it adapted.",
        f"- Human review: {hs}. Final benchmark: {hb}.",
    ])


def _human_cell(x: dict | None) -> str:
    if x is None:
        return "not reviewed"
    if x["final"] == "not_missing":
        reasons = sorted({REASON.get(r["reason"], r["reason"] or "") for r in x["reviews"] if r.get("reason")})
        return "not a missed fix" + (f" ({', '.join(reasons)})" if reasons else "")
    return HUMAN.get(x["final"], x["final"])


def list_block(rows: list[dict], final: list[dict]) -> str:
    """Every flagged pair, one table per codebase."""
    short = lambda repo: repo.split("/")[1]  # noqa: E731
    human = {x["id"]: x for x in final}
    L = []
    for n in ORDER:
        mine = [r for r in rows if r["codebase"] == n]
        if not mine:
            continue
        L += [f"### {NAMES[n]} ({len(mine)} pairs)", "",
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
    return {"README.md": {"script": script_block(s), "claude": claude_block(s), "human": human_block(s, rows, final)},
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
