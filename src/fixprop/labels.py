"""Merge the model's judgments into the results, check every one of them, and mark repeated problems.

The judgments come from a model reading the code on both sides through the API (reader.py). They are stored in
judgments/ and are an input of the pipeline; this module only reads them:
  judgments/<codebase>/labels.jsonl                one judgment per candidate, matched by (commit, repository, driver)
  judgments/<codebase>/labels_diverged.jsonl       one judgment per sampled diverged pair, matched by ID
  judgments/<codebase>/transcripts/<id>.json.gz    the complete conversation each judgment came from
What the model is given: see packets.py.

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

Every judgment records its reader (the model, as the API names it) and transcript_sha256, the checksum of the stored
conversation it came from. The merge checks that the transcript exists, has that checksum, is the conversation of this
pair with this model, was held under the current reading rules about the material the scripts extract now, and ends
with exactly this judgment accepted. A judgment without its transcript is a problem. The merged record also carries
the settings of the conversation and its token usage (reading), taken from the transcript.

Judgments that flag a missed fix (missing, same_bug) carry five more fields:
  port     verbatim = the patch carries over with only names changed; adapt = it has to be rewritten, or applies in part
  impact   runtime = changes how the device behaves at run time; light = affects only builds, debug commands or log text
           (light ones also carry impact_note: why the impact is light)
  where    the file and line of the problem in the copy; it must be one of the judgment's own cited lines
  title    one sentence saying what the problem is
  summary  a short paragraph: what was fixed, what the copy looks like now, what happens if it is not fixed

The same problem reached twice: one fix is often made several times, for example by the same author for several chips,
or months later for another chip. The same unfixed code in a copy is then reached from two fix commits. mark_repeats
finds these with a fixed rule, without any judgment: a flagged candidate covers the lines of the copy that still hold
the pre-fix code, a flagged sampled diverged pair covers the one line given as the location of the problem, and flagged
pairs in the same copy that cover a common line are one problem. The pair with the earliest fix is counted; the others
carry same_problem_as (its ID) and are left out when counting distinct problems.

Evidence rules: whether a fix applies to the copy may only be argued from what the repository shows (register
definitions, conditional compilation, configuration, commit messages, comments, the code itself), never from
remembered hardware knowledge. Every judgment cites file lines (file, line, text of the line); this module checks
each one against the pinned commit. Everything the model writes must be in English.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re

from collections import defaultdict

from . import census, gitio, packets, paths
from .candidates import id_order, key_of, load_labels
from .codebases import Codebase, all_repos
from .common import log, read_jsonl, write_jsonl
from .normalize import norm_ws

LABELS = ("missing", "not_applicable", "not_a_fix", "fixed_differently", "unsure")
LABELS_DIVERGED = ("not_a_fix", "same_bug", "not_needed_hw", "not_needed_absent", "already_ok", "unsure")
REAL = ("missing", "same_bug")  # the verdicts that flag a missed fix
CONFIDENCE = ("high", "medium", "low")
PORT = ("verbatim", "adapt")
IMPACT = ("runtime", "light")
BASIC_FIELDS = ("label", "confidence", "evidence", "evidence_refs")  # in every judgment
REAL_FIELDS = ("port", "impact", "impact_note", "where", "title", "summary")  # added when a missed fix is flagged
JUDGMENT_FIELDS = BASIC_FIELDS + REAL_FIELDS  # what the model submits; the rest of a judgment line is added by reader.collect
TEXT_FIELDS = ("evidence", "title", "summary", "impact_note")  # free text written by the model
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


USAGE = ("calls", "prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens", "reasoning_tokens", "cost")


def check_transcript(name: str, r: dict, lab: dict, rules: str, packet) -> tuple[list[str], dict | None]:
    """The stored conversation behind a judgment: it must exist, have the recorded checksum, belong to this pair and
    this model, start with the current reading rules and the pair's current packet (packet builds it), and end with
    exactly this judgment accepted. Returns (the problems, the settings and token usage of the conversation)."""
    sid = r["id"]
    f = paths.JUDGMENTS / name / "transcripts" / f"{sid}.json.gz"
    if not lab.get("transcript_sha256"):
        return [f"{sid}: the judgment does not record its transcript (transcript_sha256)"], None
    if not f.is_file():
        return [f"{sid}: the transcript {paths.rel(f)} is missing"], None
    raw = f.read_bytes()
    if hashlib.sha256(raw).hexdigest() != lab["transcript_sha256"]:
        return [f"{sid}: the transcript {paths.rel(f)} does not have the recorded checksum"], None
    try:
        t = json.loads(gzip.decompress(raw).decode("utf-8"))
        request, outcome, usage = t["request"], t["outcome"], t["usage"]
        reading = {"effort": (request.get("reasoning") or {}).get("effort"), "max_tokens": request.get("max_tokens"),
                   "provider": ", ".join((request.get("provider") or {}).get("only") or []) or None,
                   **{k: usage.get(k) or 0 for k in USAGE}}
    except (OSError, ValueError, EOFError, KeyError, TypeError, AttributeError):
        return [f"{sid}: the transcript {paths.rel(f)} cannot be read"], None
    problems = []
    if any(t.get(k) != r.get(k) for k in ("id", "commit", "target_repo", "target_unit")):
        problems.append(f"{sid}: the transcript is the conversation of another pair")
    if outcome.get("status") != "accepted" or outcome.get("judgment") != {k: lab[k] for k in JUDGMENT_FIELDS if k in lab}:
        problems.append(f"{sid}: the judgment is not the one accepted in its transcript")
    if request.get("model") != lab.get("reader"):
        problems.append(f"{sid}: the reader is not the model of the transcript")
    first = [m.get("content") if isinstance(m, dict) else None for m in (t.get("messages") or [])[:2]]
    if first[:1] != [rules]:
        problems.append(f"{sid}: the conversation was held under other reading rules than the current ones; read the pair again")
    elif first[1:] != [packet()]:
        problems.append(f"{sid}: the conversation was about other material than the scripts extract now; read the pair again")
    return problems, reading


def _apply(name: str, r: dict, lab: dict | None, allowed: tuple[str, ...], packet) -> list[str]:
    """Merge one judgment into its record r. packet builds the pair's current packet. Returns the problems."""
    if lab is None or not lab.get("label"):
        r["label"] = None
        return []
    problems = check_one(r["id"], r["target_repo"], lab, allowed)
    if not str(lab.get("reader", "")).strip():
        problems.append(f"{r['id']}: the reader is not recorded")
    found, reading = check_transcript(name, r, lab, packets.GUIDE if allowed is LABELS else packets.GUIDE_DIVERGED, packet)
    problems += found
    r["transcript"] = f"judgments/{name}/transcripts/{r['id']}.json.gz"
    r["transcript_sha256"] = lab.get("transcript_sha256")
    r["reading"] = reading
    for k in (*BASIC_FIELDS, "reader"):
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
    agg = census.unit_level(read_jsonl(census.out_dir(cb) / "fix_status.jsonl"))
    for r in recs:
        problems += _apply(cb.name, r, labels.get(key_of(r)), LABELS,
                           lambda r=r: packets.candidate_packet(cb, r["id"], key_of(r), agg[key_of(r)]))
    write_jsonl(census.out_dir(cb) / "candidates.jsonl", recs)
    return _report(cb, "candidates", recs, problems)


def merge_diverged(cb: Codebase, recs: list[dict]) -> int:
    """Sampled diverged pairs (records from diverged.sample) + judgments -> diverged_sample.jsonl.
    Returns the number of problems."""
    labels = {x["id"]: x for x in read_jsonl(paths.JUDGMENTS / cb.name / "labels_diverged.jsonl")}
    ids = {r["id"] for r in recs}
    problems = [f"{i}: has a judgment but is not in the sample" for i in sorted(set(labels) - ids)]
    agg = census.unit_level(read_jsonl(census.out_dir(cb) / "fix_status.jsonl"))
    for r in recs:
        problems += _apply(cb.name, r, labels.get(r["id"]), LABELS_DIVERGED,
                           lambda r=r: packets.diverged_packet(cb, r, agg[key_of(r)]))
    write_jsonl(census.out_dir(cb) / "diverged_sample.jsonl", recs)
    return _report(cb, "sampled diverged pairs", recs, problems)


# ------------------------------------------------------------------ the same problem reached from two fixes


def _covered(r: dict, kind: str) -> set[tuple[str, int]]:
    """The lines of the copy that a flagged pair is about, as (file, line): for a candidate the lines that still hold
    the pre-fix code, for a sampled diverged pair the line given as the location of the problem."""
    if kind == "candidate":
        return {(f["target"], n) for f in r["files"] for n in f["target_lines"]}
    w = r.get("where") or {}
    return {(w["file"], w["line"])} if w.get("file") and isinstance(w.get("line"), int) else set()


def mark_repeats(cb: Codebase, cands: list[dict], sample: list[dict]) -> int:
    """Set same_problem_as on every flagged pair that reaches a problem already reached from an earlier fix, and write
    candidates.jsonl and diverged_sample.jsonl again. cands and sample are the records after their judgments were
    merged. Returns the number of repeats."""
    copies: dict[tuple[str, str], list[tuple[dict, set]]] = defaultdict(list)
    for r in cands:
        if r.get("label") == "missing":
            copies[(r["target_repo"], r["target_unit"])].append((r, _covered(r, "candidate")))
    for r in sample:
        if r.get("label") == "same_bug":
            copies[(r["target_repo"], r["target_unit"])].append((r, _covered(r, "diverged")))
    repeats = 0
    for members in copies.values():
        groups: list[tuple[list[dict], set]] = []  # in the order of their earliest member
        for r, lines in sorted(members, key=lambda m: (m[0]["date"], id_order(m[0]["id"]))):
            hit = [g for g in groups if g[1] & lines]
            if not hit:
                groups.append(([r], set(lines)))
                continue
            first = hit[0]
            for g in hit[1:]:  # this pair joins groups that were apart so far
                first[0].extend(g[0])
                first[1].update(g[1])
                groups.remove(g)
            first[0].append(r)
            first[1].update(lines)
        for rs, _ in groups:
            for r in rs[1:]:
                r["same_problem_as"] = rs[0]["id"]
                repeats += 1
    write_jsonl(census.out_dir(cb) / "candidates.jsonl", cands)
    write_jsonl(census.out_dir(cb) / "diverged_sample.jsonl", sample)
    if repeats:
        log(f"[labels] {cb.name}: {repeats} flagged pairs reach a problem already reached from an earlier fix")
    return repeats
