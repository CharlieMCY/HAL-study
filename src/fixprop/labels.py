"""Merge the judgments written by the readers into the results, and check every piece of evidence.

The judgments come from Claude Code reading the code on both sides. They are stored in judgments/ and are an input
of the pipeline; this module only reads them:
  judgments/<codebase>/labels.jsonl            one judgment per candidate, matched by (commit, repository, driver)
  judgments/<codebase>/labels_diverged.jsonl   one judgment per sampled diverged pair, matched by ID
How the readers worked and what they were given: see packets.py.

Verdicts for candidates:
  missing            flagged as a missed fix: the copy still has the pre-fix code, has the same problem,
                     and nothing shows that the fix should not be applied
  not_applicable     the fix depends on hardware, configuration or features that the copy does not have
  not_a_fix          not a bug fix: renaming, a new feature, formatting, logging
  fixed_differently  the copy already handles the same problem in another way
  unsure             the repository does not hold enough evidence to decide
Verdicts for sampled diverged pairs:
  same_bug           flagged as a missed fix: the copy has the same problem and the script did not recognise it
  not_needed_hw      not needed: different chip, device or board
  not_needed_absent  not needed: the copy does not have this feature or this code
  already_ok         not needed: the copy already fixed it, or never had the problem
  not_a_fix, unsure  as above

Every judgment records its reader (the model ID, for example claude-opus-5-5).

Judgments that flag a missed fix (missing, same_bug) carry five more fields:
  port     verbatim = the patch carries over with only names changed; adapt = it has to be rewritten, or applies in part
  impact   runtime = changes how the device behaves at run time; light = affects only builds, debug commands or log text
           (light ones also carry impact_note: why the impact is light)
  where    the file and line of the problem in the copy; it must be one of the judgment's own cited lines
  title    one sentence saying what the problem is
  summary  a short paragraph: what was fixed, what the copy looks like now, what happens if it is not fixed

The same problem counted twice: one fix is often made several times, for example by the same author for several chips,
or months later for another chip. The same unfixed code in a copy is then reached from two fix commits. The later one
carries same_problem_as (the ID of the earlier one), same_problem_note (why it is the same problem) and same_problem_by
(who decided). They are left out when counting distinct problems.

Evidence rules: whether a fix applies to the copy may only be argued from what the repository shows (register
definitions, conditional compilation, configuration, commit messages, comments, the code itself), never from
remembered hardware knowledge. Every judgment cites file lines (file, line, text of the line); this module checks
each one against the pinned commit. Everything the readers write must be in English.
"""

from __future__ import annotations

import re

from . import census, gitio, paths
from .candidates import key_of, load_labels
from .codebases import Codebase, all_repos
from .common import log, read_jsonl, write_jsonl
from .normalize import norm_ws

LABELS = ("missing", "not_applicable", "not_a_fix", "fixed_differently", "unsure")
LABELS_DIVERGED = ("not_a_fix", "same_bug", "not_needed_hw", "not_needed_absent", "already_ok", "unsure")
REAL = ("missing", "same_bug")  # the verdicts that flag a missed fix
CONFIDENCE = ("high", "medium", "low")
PORT = ("verbatim", "adapt")
IMPACT = ("runtime", "light")
REAL_FIELDS = ("port", "impact", "impact_note", "where", "title", "summary",
               "same_problem_as", "same_problem_note", "same_problem_by")
TEXT_FIELDS = ("evidence", "title", "summary", "impact_note", "same_problem_note")  # free text written by the reader
CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")  # Chinese, Japanese and Korean characters, and full-width punctuation

_FILES: dict[tuple[str, str], list[str] | None] = {}


def pinned_lines(repo: str, path: str) -> list[str] | None:
    """The lines of a file at the pinned commit. Read from the checkout when the file is there; otherwise (the three
    large repositories are checked out only in part) read from the history. None if the file does not exist."""
    key = (repo, path)
    if key not in _FILES:
        local = paths.repo_dir(repo) / path
        if local.is_file() and not local.is_symlink():
            text = local.read_text(encoding="utf-8", errors="replace")
        else:
            text = gitio.text(gitio.git_dir(repo), "show", f"{all_repos()[repo]}:{path}", check=False, net=True)
        _FILES[key] = text.split("\n") if text else None
    return _FILES[key]


def check_refs(sid: str, target_repo: str, refs: list[dict] | None) -> list[str]:
    """Every cited "file + line + text" must match the file at the pinned commit. Returns the mismatches."""
    problems = []
    for ref in refs or []:
        repo, path = ref.get("repo") or target_repo, ref.get("file", "")
        lines = pinned_lines(repo, path)
        n, quote = ref.get("line"), norm_ws(str(ref.get("quote", "")))
        if lines is None:
            problems.append(f"{sid}: cited file does not exist: {repo}:{path}")
        elif not quote or not isinstance(n, int) or not (1 <= n <= len(lines)) or quote not in norm_ws(lines[n - 1]):
            near = [i + 1 for i, ln in enumerate(lines) if quote and quote in norm_ws(ln)][:3]
            problems.append(f"{sid}: {path}:{n} does not contain the quoted text"
                            + (f" (the text is on line {near})" if near else ""))
    return problems


def check_one(sid: str, target_repo: str, lab: dict, allowed: tuple[str, ...]) -> list[str]:
    """Is one judgment complete and correct?"""
    problems = []
    if lab.get("label") not in allowed:
        problems.append(f"{sid}: label {lab.get('label')!r} is not one of {allowed}")
    if lab.get("confidence") not in CONFIDENCE:
        problems.append(f"{sid}: confidence must be one of {CONFIDENCE}")
    if not str(lab.get("evidence", "")).strip():
        problems.append(f"{sid}: no evidence")
    if not lab.get("evidence_refs"):
        problems.append(f"{sid}: no evidence_refs")
    if lab.get("label") in REAL:
        if lab.get("port") not in PORT or lab.get("impact") not in IMPACT \
                or not str(lab.get("title", "")).strip() or not str(lab.get("summary", "")).strip():
            problems.append(f"{sid}: a flagged missed fix needs port, impact, title and summary")
        if lab.get("impact") == "light" and not str(lab.get("impact_note", "")).strip():
            problems.append(f"{sid}: impact light needs impact_note")
        w = lab.get("where") or {}
        if (w.get("file"), w.get("line")) not in {(x.get("file"), x.get("line")) for x in lab.get("evidence_refs") or []
                                                  if not x.get("repo")}:
            problems.append(f"{sid}: where must be one of the cited lines in the copy's repository")
    for k in TEXT_FIELDS:
        if CJK.search(str(lab.get(k) or "")):
            problems.append(f"{sid}: {k} must be in English (it contains Chinese characters)")
    if any(CJK.search(str(ref.get("quote", ""))) for ref in lab.get("evidence_refs") or []):
        problems.append(f"{sid}: quote only the code or English part of a line (a quote contains Chinese characters)")
    return problems + check_refs(sid, target_repo, lab.get("evidence_refs"))


def _apply(r: dict, lab: dict | None, allowed: tuple[str, ...]) -> list[str]:
    """Merge one judgment into its record r. Returns the problems."""
    if lab is None or not lab.get("label"):
        r["label"] = None
        return []
    problems = check_one(r["id"], r["target_repo"], lab, allowed)
    if not str(lab.get("reader", "")).strip():
        problems.append(f"{r['id']}: the reader is not recorded")
    for k in ("label", "confidence", "evidence", "evidence_refs", "reader"):
        r[k] = lab.get(k)
    for k in REAL_FIELDS:
        if lab.get("label") in REAL and k in lab:
            r[k] = lab[k]
    return problems


def _report(cb: Codebase, what: str, recs: list[dict], problems: list[str]) -> int:
    done = sum(1 for r in recs if r.get("label"))
    refs = sum(len(r.get("evidence_refs") or []) for r in recs)
    log(f"[labels] {cb.name}: {what}: {done}/{len(recs)} read; {refs} cited lines; {len(problems)} problems")
    for p_ in problems[:40]:
        log("    " + p_)
    return len(problems)


def merge(cb: Codebase, recs: list[dict]) -> int:
    """Candidates (records from candidates.run) + judgments -> candidates.jsonl. Returns the number of problems."""
    labels = load_labels(cb.name)
    keys = {key_of(r) for r in recs}
    problems = [f"{x['id']}: has a judgment but is no longer a candidate" for k, x in sorted(labels.items()) if k not in keys]
    for r in recs:
        problems += _apply(r, labels.get(key_of(r)), LABELS)
    write_jsonl(census.out_dir(cb) / "candidates.jsonl", recs)
    return _report(cb, "candidates", recs, problems)


def merge_diverged(cb: Codebase, recs: list[dict]) -> int:
    """Sampled diverged pairs (records from diverged.sample) + judgments -> diverged_sample.jsonl.
    Returns the number of problems."""
    labels = {x["id"]: x for x in read_jsonl(paths.JUDGMENTS / cb.name / "labels_diverged.jsonl")}
    ids = {r["id"] for r in recs}
    problems = [f"{i}: has a judgment but is not in the sample" for i in sorted(set(labels) - ids)]
    for r in recs:
        problems += _apply(r, labels.get(r["id"]), LABELS_DIVERGED)
    write_jsonl(census.out_dir(cb) / "diverged_sample.jsonl", recs)
    return _report(cb, "sampled diverged pairs", recs, problems)
