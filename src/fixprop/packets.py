"""The reading rules and the packet of each pair: everything the model is given.

Whether a candidate is a missed fix cannot be decided by script; a model reads the code on both sides (reader.py).

  GUIDE              the rules for reading a candidate: verdicts, order of judging, conventions, evidence, how to look
                     things up, language, and the fields of a judgment
  GUIDE_DIVERGED     the rules for reading a sampled diverged pair
  candidate_packet,  the packet of one pair: the commit and its message, the hunks, the lines of the copy, and the
  diverged_packet    pinned commits
  iter_packets       every pair to read, in order

A conversation starts with the rules as the system message and the packet as the first user message. Packets are
extracted from the repositories by script and contain no judgment of anyone. The same data always gives byte-identical
rules and packets.
"""

from __future__ import annotations

from . import gitio, hunks, paths
from .census import out_dir, unit_level
from .codebases import ORDER, Codebase, get
from .common import read_jsonl
from .candidates import blob_url, commit_url, numbered

MAX_FILES, MAX_HUNKS = 4, 6  # at most this many files per packet, and this many hunks per file

# ------------------------------------------------------------------ the reading rules
# Both sets of rules share the introduction, the evidence rules, the way to look things up, the language rule and the
# fields of a judgment.

_INTRO = """\
You read embedded driver source code to decide whether a bug fix made in one driver was missed in a copy of the same code
in another driver. You work through tools that can only read: nothing is compiled, run or changed.
"""

_EVIDENCE = """\
[Evidence (hard rules)]
When judging whether a fix applies, evidence may only come from what is visible in the repository:
- register definitions (whether a header defines this bit or this register)
- conditional compilation (#ifdef, #if defined(CONFIG_...), Rust's #[cfg(...)])
- Kconfig, features, devicetree bindings
- the exact words of the commit message
- code comments, and the code itself (whether a function exists, what its parameters are, what a table holds)
Do not use hardware knowledge you remember as evidence. "I remember this bit is reserved on this chip" does not count; a header that does not define this bit does.
If you cannot cite evidence from the repository but think the fix may not apply, mark unsure and say what evidence is missing.

missing also needs evidence: which function the lines on the other side are in; why they are the same logic as the source before the fix;
which places you checked that could make it not applicable (whether the register bits, macros and functions the fix uses exist on the other side).
"""

_LOOKUP = """\
[How to look things up]
Use the tools. Repositories are named <owner>/<repo>, as in the packet; paths are relative to the repository root.
- read_file: the lines of a file, with line numbers. Without "commit" it is the file at the repository's pinned commit;
  with "commit" it is the file as it was at that commit (add ^ to a hash for the commit before it).
- list_dir: the entries of a directory.
- search: find a string in the files checked out locally at the pinned commit. The Rust repositories are checked out
  in full; apache/nuttx only has arch/; zephyrproject-rtos/zephyr only has drivers/; RT-Thread/rt-thread only has the
  drv_*.c files under bsp/. Files outside these (headers, bindings, Kconfig) are not searched: find them with list_dir
  and read them with read_file.
- commit_diff: the full change a commit made to one file.
- file_log: the latest commits that changed a file. The history goes back to 2023-06-01.
There is no web access and nobody to ask.
"""

_LANGUAGE = """\
[Language]
Write everything in English: evidence, title, summary, impact_note, and the quotes in evidence_refs.
Some commit messages and code comments in these repositories are in Chinese. Read them, but do not copy Chinese text:
translate what you need into English, and in evidence_refs quote only the code or English part of a line.
The check rejects Chinese characters in any field you write.
"""

_FIELDS = """\
- confidence: high, medium or low.
- evidence: three to five sentences in English stating the basis. When you mention the commit message, quote its exact words
  (translated into English if it is in another language).
- evidence_refs: the file lines you cite, at least one. file is the path inside the repository, line is the line number at the pinned commit,
  quote is the text of that line (it may be a part of the line, but it must appear verbatim in that line).
  By default it refers to the repository of the other driver; when citing a file in another repository, add "repo": "<owner>/<repo>".
  A script checks every one; any that do not match have to be redone.
"""

_FLAG_FIELDS = """\
- port: verbatim = the patch can be carried over with only the names changed; adapt = it has to be rewritten for the other side's code, or only part of it applies.
- impact: runtime = affects how the device behaves at run time (wrong results, hangs, crashes, lost data, wrong register writes, races);
  light = only affects building, debug or test commands, or log text, or the code is wrong but the runtime result most likely stays the same.
  When impact is light, also fill impact_note: one sentence saying why it is minor.
- where: the file and line of the problem on the other side, {"file": ..., "line": ...}.
  It must be one of the evidence_refs, and in the repository of the other driver.
- title: one plain sentence saying what the problem is.
- summary: one plain paragraph of three to five sentences: which side fixed what, and when; what the other side looks like now; what happens if it is not fixed;
  if you are not confident, what is missing.
"""

_SUBMIT = """\
submit_judgment checks the judgment: the required fields, the language, and every quote against the cited line.
If it reports problems, fix them and call it again. The task ends when a call is accepted, so do not write the judgment
as a plain reply.
"""

_CANDIDATE = """\
Reading guide: is a "candidate" a missed fix?

[Background]
The scripts picked out a set of "candidates": a bug-fix commit changed a driver file; the file has a copy in another driver;
the copy still has the lines from before the fix and does not have the lines from after it. A candidate is not necessarily a missed fix.
Your task is to read the code on both sides, decide which of the five kinds below the candidate is, and submit the evidence.

The candidate's packet is the first user message. It holds the commit's link and message, the hunks that were picked out,
the lines still present in the other side's file (with line numbers), the paths of both files, and the pinned commits of both repositories.

[Five verdicts]
missing            missed fix: the other side does still have the pre-fix code, has the same problem, and shows no reason not to fix it
not_applicable     does not apply there: the fix depends on hardware, configuration or features that the other side lacks or has differently
not_a_fix          not actually a bug fix: the commit (or the hunks picked out) renames, adds a feature, reformats, changes printing, changes comments or refactors
fixed_differently  the other side already handles the same problem in another way
unsure             the repository does not hold enough evidence to decide

Order of judging:
1. First check whether the hunks picked out fix a behavioural error. What you judge is the hunks listed in the packet, not the whole commit:
   a commit may mix a real fix with incidental cleanup. If what was picked out is only the formatting, renaming, logging or new-feature part, mark not_a_fix.
2. If it is a fix, open the other side's file, look at the function and context around those lines, and confirm it is the same logic.
   If the text only happens to be the same and it is not the same logic, mark not_applicable and give the reason.
3. Check whether the other side already handles the same problem in another way (fixed_differently).
4. Check whether what the fix depends on exists on the other side (not_applicable).
5. Only when all of these are ruled out is it missing. If you are not sure, mark unsure.

Conventions:
- When a candidate has several hunks in different situations: if any one of them is a missed fix, mark missing, and explain in the evidence which apply and which do not.
- Commits that fix build errors: if the other side would also fail to build under the same conditions, it is missing; if the commit only silences a compiler warning
  and the runtime result does not change, it is not_a_fix.
- Changes a downstream repository made for its own chips (for example py32-hal's changes against embassy-stm32): when you cannot see that they also hold upstream,
  mark not_applicable.
- How to tell "the other side never had this problem" from "the other side fixed it another way": a difference that comes from hardware, configuration or device model
  is not_applicable; a difference that is purely in how the code is written is fixed_differently. Neither is a missed fix.
- Nothing is compiled, nothing is run, there is no hardware. Conclusions can only come from reading code. If you are not confident, lower the confidence or mark unsure;
  do not force a verdict.
"""

_DIVERGED = """\
Reading guide: a fix where the code on the two sides no longer matches. Did the other side miss the fix, or does it not need it?

[Background]
A bug-fix commit changed a file of one driver; another driver has a file that is a copy of it. The fix did not change that file in the same commit,
and the other side's code does not match textually either: neither the lines from before the fix nor the lines from after it can be found there.
The script can only say "does not match", not why. Your task is to read the code on both sides, decide which kind below it is, and write down the evidence.

The item's packet is the first user message. It holds the commit's link and message, the hunks that do not match, the name of the function
each change is in, the approximate line number of the function with the same name on the other side (if it can be found), and the paths of both files.
You have to find the corresponding code in the other side's file yourself: first look for a function with the same or a similar name;
if there is none, look by what the code does.

[Six verdicts]
not_a_fix          the commit (or the hunks listed in the packet) is not actually a bug fix: renaming, a new feature, formatting, printing, comments, refactoring
same_bug           missed fix: the other side has the same problem and has not fixed it. The script just did not recognise it because the text is different
not_needed_hw      not needed: a different chip, device or board. The registers, bits or peripheral features the fix depends on are absent or different on the other side
not_needed_absent  not needed: the other side does not have this feature or this code at all
already_ok         not needed: the other side has already fixed it (the same way or another way), or the way it is written never had this problem
unsure             the repository does not hold enough evidence to decide

- First decide whether it is a bug fix. What you judge is the hunks listed in the packet.
- same_bug requires you to find the corresponding logic on the other side and explain why it has the same problem.
- The evidence for not_needed_hw may only be what is visible in the repository. If you think there may be a hardware reason but cannot cite repository evidence,
  mark unsure.
- The evidence for not_needed_absent and already_ok is the other side's code itself: point out which function, member or path the other side lacks,
  or how it handles the case.
- When the hunks in one packet are in different situations, label it by the most important one, and explain each of them in the evidence.
"""


def _guide(body: str, verdicts: str, flag: str) -> str:
    return (_INTRO + "\n" + body + "\n" + _EVIDENCE + "\n" + _LOOKUP + "\n" + _LANGUAGE + "\n"
            + "[Output]\nWhen you have decided, call submit_judgment with these fields:\n\n"
            + f"- label: one of the {verdicts} verdicts.\n" + _FIELDS + "\n"
            + f"When label is {flag}, five more fields are required:\n" + _FLAG_FIELDS + "\n" + _SUBMIT)


GUIDE = _guide(_CANDIDATE, "five", "missing")
GUIDE_DIVERGED = _guide(_DIVERGED, "six", "same_bug")


# ------------------------------------------------------------------ the packets


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


def _head(cb: Codebase, sid: str, repo: str, sha: str, date: str, trepo: str, tunit: str) -> list[str]:
    gitdir = gitio.git_dir(repo)
    msg = gitio.text(gitdir, "log", "-1", "--format=%an%n%s%n%n%b", sha).split("\n")
    changed = gitio.text(gitdir, "show", "--no-renames", "--name-status", "--format=", sha).strip().split("\n")
    L = [f"ID: {sid}", f"Codebase: {cb.name}", "",
         f"Bug-fix commit: {repo} {sha} ({date})",
         f"Link: {commit_url(repo, sha)}",
         f"Author: {msg[0]}",
         f"Other driver: {tunit}",
         f"Pinned commits of both repositories: {repo} {cb.repos[repo]}"
         + ("" if trepo == repo else f"; {trepo} {cb.repos[trepo]}"),
         "", "==== Commit message", *msg[1:60], "",
         "==== Files changed by this commit", *changed[:40], *(["..."] if len(changed) > 40 else []), ""]
    return L


def candidate_packet(cb: Codebase, sid: str, key: tuple[str, str, str], a: dict) -> str:
    """The packet of one candidate. a is this candidate's entry from census.unit_level."""
    sha, trepo, tunit = key
    rows = [r for r in a["rows"] if r["status"] == "candidate"]
    repo = rows[0]["repo"]
    gitdir = gitio.git_dir(repo)
    L = _head(cb, sid, repo, sha, rows[0]["date"], trepo, tunit)
    L.insert(7, "Does the side that was fixed still have this fix now (judged by script, may be inaccurate): "
             + ("yes" if a["still_fixed"] else "no"))
    for r in rows[:MAX_FILES]:
        tfile = cb.root(trepo) / r["target"]
        tlines = tfile.read_text(encoding="utf-8", errors="replace").split("\n")
        hs = hunks.parse_diff(gitio.diff_blobs(gitdir, r["blobs"][0], r["blobs"][1]))
        cand = [h for h in r["hunks"] if h["status"] == "candidate"]
        at = sorted({n for h in cand for n in h["target_lines"]})
        L += [f"==== Changed file {r['source']}  →  other side's file {r['target']}",
              f"Current location of the changed file: {r['source_now']} (local: {paths.rel(cb.root(repo) / r['source_now'])})",
              f"Other side's file (local): {paths.rel(tfile)}",
              f"Other side's link: {blob_url(trepo, r['target_commit'], r['target'], at)}",
              f"Candidate hunks: {len(cand)}" + (f"; only the first {MAX_HUNKS} are listed below" if len(cand) > MAX_HUNKS else ""), ""]
        for h in cand[:MAX_HUNKS]:
            kind = {"replace": "replace", "add": "add", "delete": "delete only"}[h["kind"]]
            L += [f"---- Hunk (near line {h['old_start']} of the changed file; {kind}; "
                  f"the side that was fixed still has it: {'yes' if h['source_still_fixed'] else 'no'})",
                  hs[h["i"]].text(), "",
                  f"Lines still present in the other side's {r['target']} (line numbers {h['target_lines']}):",
                  _excerpt(tlines, h["target_lines"]), ""]
            if h["added_elsewhere"]:
                L += [f"Lines that this hunk adds also appear in the other side's {r['target']}, at line numbers "
                      f"{h['added_elsewhere']}. The hunk is listed because the pre-fix lines above are still in place: "
                      "check whether those other lines are this fix, or only the same statements in another place.", ""]
    if len(rows) > MAX_FILES:
        L.append(f"({len(rows) - MAX_FILES} more files in this driver have candidate hunks; they are not listed.)")
    return "\n".join(L).rstrip() + "\n"


_BUCKET = {"near": "the fixed function exists on the other side and most of the code around the change is the same; only the changed lines do not match",
           "far": "the fixed function exists on the other side, but most of the code around the change is different",
           "gone": "the fixed function cannot be found in the other side's file"}


def diverged_packet(cb: Codebase, rec: dict, a: dict) -> str:
    """The packet of one sampled diverged pair. rec is the record from diverged.sample, a its entry from census.unit_level."""
    repo = rec["repo"]
    gitdir = gitio.git_dir(repo)
    L = _head(cb, rec["id"], repo, rec["commit"], rec["date"], rec["target_repo"], rec["target_unit"])
    L.insert(7, f"Script's rough grouping: {_BUCKET[rec['bucket']]}")
    for f in rec["files"]:
        row = next(r for r in a["rows"] if r["status"] == "diverged" and r["source"] == f["source"] and r["target"] == f["target"])
        hs = hunks.parse_diff(gitio.diff_blobs(gitdir, row["blobs"][0], row["blobs"][1]))
        by_start = {h["old_start"]: h["i"] for h in row["hunks"] if h["status"] == "diverged"}
        n_all = len(by_start)
        L += [f"==== Changed file {f['source']}  →  other side's file {f['target']}",
              f"Current location of the changed file: {f['source_now']} (local: {paths.rel(cb.root(repo) / f['source_now'])})",
              f"Other side's file (local): {paths.rel(cb.root(rec['target_repo']) / f['target'])}",
              f"Hunks that do not match: {n_all}" + (f"; only {len(f['hunks'])} are listed below" if n_all > len(f["hunks"]) else ""), ""]
        for h in f["hunks"]:
            where = (f"the function with the same name on the other side is near line {h['target_line']}" if h["target_line"]
                     else "no function with the same name on the other side")
            L += [f"---- Hunk (near line {h['old_start']} of the changed file; function: {h['function'] or 'not identified'}; {where})",
                  hs[by_start[h["old_start"]]].text(), ""]
    n_files = sum(1 for r in a["rows"] if r["status"] == "diverged")
    if n_files > len(rec["files"]):
        L.append(f"({n_files - len(rec['files'])} more files in this driver do not match; they are not listed.)")
    return "\n".join(L).rstrip() + "\n"


def iter_packets(everything: bool = False):
    """Every pair to read, in order, as (index entry, function that builds the packet text). By default only the
    pairs not yet read; with everything, all candidates and all sampled diverged pairs."""
    for name in ORDER:
        cb = get(name)
        agg = unit_level(read_jsonl(out_dir(cb) / "fix_status.jsonl"))
        for sid, key, read in numbered(name, agg):
            if read and not everything:
                continue
            entry = {"id": sid, "codebase": name, "kind": "candidate", "commit": key[0], "target_repo": key[1],
                     "target_unit": key[2], "read": read}
            yield entry, (lambda cb=cb, sid=sid, key=key, a=agg[key]: candidate_packet(cb, sid, key, a))
        if everything:  # the sampled diverged pairs
            for rec in read_jsonl(out_dir(cb) / "diverged_sample.jsonl"):
                key = (rec["commit"], rec["target_repo"], rec["target_unit"])
                entry = {"id": rec["id"], "codebase": name, "kind": "diverged", "commit": key[0], "target_repo": key[1],
                         "target_unit": key[2], "read": bool(rec.get("label"))}
                yield entry, (lambda cb=cb, rec=rec, a=agg[key]: diverged_packet(cb, rec, a))
