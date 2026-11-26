"""The reading material and instructions for Claude Code.

Whether a candidate is a missed fix cannot be decided by script; someone has to read the code on both sides. This module
writes everything the readers are given:

  python -m fixprop packets --out <dir>          one packet per pair not yet read, plus the instructions and the batches
  python -m fixprop packets --out <dir> --all    also the pairs that have already been read
  python -m fixprop packets --out <dir> --verify <result file> [--batch <name>]
                                                 check a reader's result file: fields complete, every cited line matches
                                                 the pinned commit, and (with --batch) every pair of the batch present
  python -m fixprop packets --out <dir> --collect <model ID>
                                                 check every batch's result file and copy the judgments into judgments/

<dir> then contains:
  GUIDE.txt            how to read a candidate, the verdicts, the evidence rules, and the output format
  GUIDE_DIVERGED.txt   how to read a sampled diverged pair
  index.jsonl          one line per packet: ID, commit, the driver with the copy, batch, whether it has been read
  <id>.txt             one packet per pair
  batches/<name>.txt   the complete prompt of one batch: TASK (or TASK_DIVERGED) with the batch filled in

How the reading is run: each batch prompt is given, as it is, to one Claude Code sub-agent. The sub-agent reads GUIDE.txt,
writes one JSON line per pair to <dir>/results/<batch>.jsonl, and runs the --verify command before it finishes. The
results are then collected into judgments/<codebase>/labels.jsonl (labels_diverged.jsonl for diverged pairs) with the
reader's model ID (--collect), and `python -m fixprop all` checks every judgment again while merging.

Packets are extracted from the repositories by script and contain no judgment of anyone. The same data always gives
byte-identical packets and prompts.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import gitio, hunks, paths
from .labels import REAL
from .census import out_dir, unit_level
from .codebases import ORDER, Codebase, get
from .common import log, read_jsonl, write_jsonl
from .candidates import blob_url, commit_url, numbered

MAX_FILES, MAX_HUNKS = 4, 6  # at most this many files per packet, and this many hunks per file
MAX_BATCH = 9  # at most this many pairs per batch; one batch goes to one reader

GUIDE = """\
Reading guide: is a "candidate" a missed fix?

[Background]
The scripts picked out a set of "candidates": a bug-fix commit changed a driver file; the file has a copy in another driver;
the copy still has the lines from before the fix and does not have the lines from after it. A candidate is not necessarily a missed fix.
Your task is to read the code on both sides, decide which of the five kinds below each candidate is, and write down the evidence.

Each candidate has one packet: <id>.txt in this directory. It holds the commit's link and message, the hunks that were picked out,
the lines still present in the other side's file (with line numbers), the local paths of both files, and the pinned commits of both repositories.

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

[How to look things up]
The local files (read-only) are in data/repos/<owner>/<repo>/, with the content at the pinned commit.
The Rust repositories are full trees; apache/nuttx has only arch/; zephyrproject-rtos/zephyr has only drivers/;
RT-Thread/rt-thread has only the drv_*.c files under bsp/.
For files that are not local (other directories, headers, bindings, Kconfig), or a file as it was at some commit, use read-only git commands.
Run them in the project root; the pinned commits are given in the packet:
  git -C data/repos/<owner>/<repo> show <commit>:<path in repo>              view a file
  git -C data/repos/<owner>/<repo> ls-tree --name-only <commit> <dir>/     list a directory
  git -C data/repos/<owner>/<repo> show <fix commit> -- <file>              view the fix commit's full change to one file
  git -C data/repos/<owner>/<repo> log -5 --format='%h %cs %s' <commit> -- <file>   view a file's latest commits
To search the content of local files use grep -n, and only inside the directories under data/repos/<owner>/<repo>/.

[Not allowed]
- Do not compile or run anything in the repositories, and do not execute their scripts.
- Do not change anything under data/repos/: no git checkout, switch, restore, reset, stash, pull, fetch, gc or
  sparse-checkout, and no creating, changing or deleting files. Use only the read-only commands listed above.
- Do not use git log -S, git log -G, git grep, git blame or other commands that read the content of every historical file:
  these clones do not carry historical file content, and such commands download it from the remote in bulk.
- Do not contact any upstream and do not search the web.
- Do not change any file in the project other than your own output file. Put temporary files in the directory given in the task description.

[Language]
Write everything in English: evidence, title, summary, impact_note, and the quotes in evidence_refs.
Some commit messages and code comments in these repositories are in Chinese. Read them, but do not copy Chinese text:
translate what you need into English, and in evidence_refs quote only the code or English part of a line.
The check rejects Chinese characters in any field you write.

[Output]
One JSON line per candidate, appended to the output file given in the task description. Write each line as soon as that candidate is done;
do not save them up until the end.

{"id": "nuttx-041", "label": "not_a_fix", "confidence": "high", "evidence": "...", "evidence_refs": [{"file": "arch/arm/src/stm32h7/stm32_i2c.c", "line": 367, "quote": "static inline void stm32_i2c_sem_post(struct i2c_master_s *dev);"}]}

- id: the packet's ID.
- label: one of the five verdicts.
- confidence: high, medium or low.
- evidence: three to five sentences in English stating the basis. When you mention the commit message, quote its exact words
  (translated into English if it is in another language).
- evidence_refs: the file lines you cite, at least one. file is the path inside the repository, line is the line number at the pinned commit,
  quote is the text of that line (it may be a part of the line, but it must appear verbatim in that line).
  By default it refers to the repository of the other driver; when citing a file in another repository, add "repo": "<owner>/<repo>".
  A script checks every one; any that do not match have to be redone.

When label is missing, five more fields are required:
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

GUIDE_DIVERGED = """\
Reading guide: a fix where the code on the two sides no longer matches. Did the other side miss the fix, or does it not need it?

First read GUIDE.txt in the same directory. Its sections "Evidence", "How to look things up", "Not allowed" and "Language",
and the field formats in its "Output" section, apply here as well.

[Background]
A bug-fix commit changed a file of one driver; another driver has a file that is a copy of it. The fix did not change that file in the same commit,
and the other side's code does not match textually either: neither the lines from before the fix nor the lines from after it can be found there.
The script can only say "does not match", not why. Your task is to read the code on both sides, decide which kind below it is, and write down the evidence.

Each item has one packet: <id>.txt in this directory. It holds the commit's link and message, the hunks that do not match, the name of the function
each change is in, the approximate line number of the function with the same name on the other side (if it can be found), and the local paths of both files.
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

[Output]
One JSON line per item, with the same fields as in GUIDE.txt (id, label, confidence, evidence, evidence_refs). At least one reference per item.
When label is same_bug, also fill port, impact (plus impact_note when it is light), where, title and summary, in the same way as for missing.
"""

_BATCH = """\
Your batch: {batch}
IDs in this batch: {ids}
Packets: {dir}/<id>.txt
Output file: {output}
Temporary directory: {tmp}
"""

_WRITE = """\
Write the results to the output file: one JSON line per pair, appending a line as soon as each pair is done, not saving them up
until the end. Put temporary files only in the temporary directory.

When everything is written, check it; if a problem is reported, fix the corresponding line in the output file, until there are no problems:
  .venv/bin/python -I -m fixprop packets --out {dir} --verify {output} --batch {batch}
"""

_RULES = """\
The most common mistakes:
- Read only. Under data/repos/, do nothing that changes files or git state (no checkout, switch, restore, reset, stash,
  pull, fetch, gc), and do not compile or run anything there. Use only the read-only commands listed in GUIDE.txt.
- Evidence may only come from what is visible in the repository, not from chip knowledge you remember. If you cannot cite evidence, mark unsure.
- What you judge is the hunks listed in the packet. A commit message that contains "fix" does not mean the hunks picked out fix a bug.
- LOGIC
- The quote of every evidence_refs entry must appear verbatim in that line, and the line number is the one at the pinned commit.
- Write in English only. Some commit messages and code comments are in Chinese: translate what you need, and quote only the code or English part of a line.
- When one commit has several pairs in your batch, first understand the fix itself, then look at the other drivers one by one; write one line for each,
  each citing lines in its own driver. Do not assume a later one has the same verdict as an earlier one: each driver may be written differently.
- Locally there are only nuttx's arch/, zephyr's drivers/ and rt-thread's bsp/**/drv_*.c; view files in other directories with the git show command in GUIDE.txt.
- Do not change any other file besides your output file and temporary directory.
"""

TASK = ("""\
You will read a batch of embedded driver source code and decide, for each "candidate" (a bug fix that may not have been carried over
to a copy of the code in another driver), whether the fix was missed. This is a read-only code analysis task.
Work in the project's root directory (the one that contains data/repos/).

""" + _BATCH + """
Step one: read {dir}/GUIDE.txt in full and follow it strictly (the kinds of verdicts, the evidence rules, how to look things up,
what is not allowed, the language, and the output format are all in it).

""" + _WRITE + "\n" + _RULES.replace("LOGIC", "Before concluding missing, first confirm that the lines on the other side are the same logic as the source "
                                     "before the fix. Text that only happens to be the same does not count.") + """
When everything is done and the check passes, reply with two lines only: the first line, how many of each of the five verdicts;
the second line, the IDs judged missing (write "none" if there are none).
""")

TASK_DIVERGED = ("""\
You will read a batch of embedded driver source code. Each item is a bug fix in one driver whose code no longer matches the copy
in another driver; decide whether the other side missed the fix or does not need it. This is a read-only code analysis task.
Work in the project's root directory (the one that contains data/repos/).

""" + _BATCH + """
Step one: read {dir}/GUIDE_DIVERGED.txt in full and follow it strictly; it says which parts of {dir}/GUIDE.txt also apply.

""" + _WRITE + "\n" + _RULES.replace("LOGIC", "same_bug requires you to find the corresponding logic on the other side and to explain "
                                     "why it has the same problem.") + """
When everything is done and the check passes, reply with two lines only: the first line, how many of each of the six verdicts;
the second line, the IDs judged same_bug (write "none" if there are none).
""")


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
        cand = [h for h in r["hunks"] if h["status"] == "candidate" and (cb.count_delete_only or h["kind"] != "delete")]
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


def split_batches(index: list[dict]) -> dict[str, list[dict]]:
    """Assign every packet to a batch. Per codebase and kind, in ID order, the packets are cut into ceil(n / MAX_BATCH)
    consecutive batches whose sizes differ by at most one. Batch names: <codebase>-c01... for candidates,
    <codebase>-d01... for diverged pairs. Sets the "batch" field of every index entry."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in index:
        groups.setdefault((it["codebase"], it["kind"]), []).append(it)
    out: dict[str, list[dict]] = {}
    for (name, kind), items in groups.items():
        k = -(-len(items) // MAX_BATCH)
        size, extra = divmod(len(items), k)
        i = 0
        for b in range(k):
            n = size + (1 if b < extra else 0)
            bname = f"{name}-{'c' if kind == 'candidate' else 'd'}{b + 1:02d}"
            out[bname] = items[i:i + n]
            for it in out[bname]:
                it["batch"] = bname
            i += n
    return out


def write(out: Path, everything: bool = False) -> int:
    """Write the packets, instructions and batch prompts to out. By default only the pairs not yet read.
    Returns the number of packets."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "GUIDE.txt").write_text(GUIDE, encoding="utf-8")
    (out / "GUIDE_DIVERGED.txt").write_text(GUIDE_DIVERGED, encoding="utf-8")
    index = []
    for name in ORDER:
        cb = get(name)
        agg = unit_level(read_jsonl(out_dir(cb) / "fix_status.jsonl"))
        n = 0
        for sid, key, read in numbered(name, agg):
            if read and not everything:
                continue
            (out / f"{sid}.txt").write_text(candidate_packet(cb, sid, key, agg[key]), encoding="utf-8")
            index.append({"id": sid, "codebase": name, "kind": "candidate", "commit": key[0], "target_repo": key[1],
                          "target_unit": key[2], "read": read})
            n += 1
        m = 0
        if everything:  # the sampled diverged pairs
            for rec in read_jsonl(out_dir(cb) / "diverged_sample.jsonl"):
                key = (rec["commit"], rec["target_repo"], rec["target_unit"])
                (out / f"{rec['id']}.txt").write_text(diverged_packet(cb, rec, agg[key]), encoding="utf-8")
                index.append({"id": rec["id"], "codebase": name, "kind": "diverged", "commit": key[0],
                              "target_repo": key[1], "target_unit": key[2], "read": bool(rec.get("label"))})
                m += 1
        log(f"[packets] {name}: {n} candidates" + (f", {m} diverged pairs" if m else ""))
    batches = split_batches(index)
    d = paths.rel(out)
    (out / "batches").mkdir(exist_ok=True)
    for bname, items in batches.items():
        template = TASK if items[0]["kind"] == "candidate" else TASK_DIVERGED
        prompt = template.format(batch=bname, ids=", ".join(it["id"] for it in items), dir=d,
                                 output=f"{d}/results/{bname}.jsonl", tmp=f"{d}/tmp/{bname}/")
        (out / "batches" / f"{bname}.txt").write_text(prompt, encoding="utf-8")
    write_jsonl(out / "index.jsonl", index)
    log(f"[packets] {len(index)} packets in {len(batches)} batches, written to {d}/")
    return len(index)


def verify(out: Path, result: Path, batch: str | None = None) -> int:
    """Check a reader's result file (one JSON line per pair) with the same checks that are applied when merging.
    With batch, also check that every pair of that batch is present. Returns the number of problems."""
    from .labels import LABELS, LABELS_DIVERGED, check_one

    index = {x["id"]: x for x in read_jsonl(out / "index.jsonl")}
    problems, seen = [], set()
    lines = result.read_text(encoding="utf-8").split("\n") if result.is_file() else []
    if not result.is_file():
        problems.append(f"{paths.rel(result)} does not exist")
    for n, ln in enumerate(lines, 1):
        if not ln.strip():
            continue
        try:
            lab = json.loads(ln)
        except ValueError as e:
            problems.append(f"line {n} is not valid JSON: {e}")
            continue
        it = index.get(lab.get("id"))
        if it is None:
            problems.append(f"line {n}: ID {lab.get('id')!r} is not in {paths.rel(out / 'index.jsonl')}")
            continue
        if batch and it.get("batch") != batch:
            problems.append(f"line {n}: {lab['id']} is not in batch {batch}")
        if lab["id"] in seen:
            problems.append(f"{lab['id']}: written more than once")
        seen.add(lab["id"])
        allowed = LABELS if it["kind"] == "candidate" else LABELS_DIVERGED
        problems += check_one(lab["id"], it["target_repo"], lab, allowed)
    if batch:
        want = [i for i, x in index.items() if x.get("batch") == batch]
        if not want:
            problems.append(f"there is no batch named {batch}")
        todo = [i for i in want if i not in seen]
        if todo:
            problems.append(f"not written yet: {' '.join(todo)}")
    for p in problems:
        log("    " + p)
    log(f"[packets] {paths.rel(result)}: {len(seen)} pairs, {len(problems)} problems")
    return len(problems)


def _id_order(sid: str) -> tuple[int, int]:
    """Sort key of an ID: candidates (<codebase>-001) before diverged pairs (<codebase>-D01), each by number."""
    tail = sid.rsplit("-", 1)[1]
    return (1, int(tail[1:])) if tail.startswith("D") else (0, int(tail))


FIELDS = ("label", "confidence", "evidence", "evidence_refs", "port", "impact", "impact_note", "where", "title", "summary")
SAME = ("same_problem_as", "same_problem_note", "same_problem_by")


def collect(out: Path, reader: str) -> int:
    """Check the result file of every batch in out and copy the judgments into judgments/<codebase>/: labels.jsonl for
    candidates, labels_diverged.jsonl for diverged pairs. Each judgment gets the pair's key and the reader's model ID,
    and replaces an earlier judgment of the same pair (keeping its repeat marking if it still flags a missed fix).
    Nothing is written while any check fails. Returns the number of problems."""
    index = {x["id"]: x for x in read_jsonl(out / "index.jsonl")}
    problems, got = 0, {}
    for b in sorted({x["batch"] for x in index.values()}):
        f = out / "results" / f"{b}.jsonl"
        problems += verify(out, f, b)
        if f.is_file():
            got.update({x["id"]: x for x in read_jsonl(f)})
    if problems:
        log(f"[packets] {problems} problems; nothing was written to {paths.rel(paths.JUDGMENTS)}/")
        return problems
    n = 0
    for name in dict.fromkeys(x["codebase"] for x in index.values()):
        for kind, fname in (("candidate", "labels.jsonl"), ("diverged", "labels_diverged.jsonl")):
            path = paths.JUDGMENTS / name / fname
            rows = {x["id"]: x for x in read_jsonl(path)}
            for sid, it in index.items():
                if it["codebase"] != name or it["kind"] != kind or sid not in got:
                    continue
                lab, old = got[sid], rows.get(sid, {})
                new = {"id": sid, "commit": it["commit"], "target_repo": it["target_repo"], "target_unit": it["target_unit"],
                       **{k: lab[k] for k in FIELDS if k in lab}, "reader": reader}
                if lab["label"] in REAL:
                    new.update({k: old[k] for k in SAME if k in old})
                rows[sid] = new
                n += 1
            if rows:
                write_jsonl(path, sorted(rows.values(), key=lambda x: _id_order(x["id"])))
    log(f"[packets] collected {n} judgments into {paths.rel(paths.JUDGMENTS)}/")
    return 0
