"""The whole pipeline, and the check that recomputing gives the same results.

  python -m fixprop all     recompute every result, replace benchmark/ with the new results, and update the generated
                            parts of README.md and judgments/results.md
  python -m fixprop check   three checks; any mismatch is an error:
                              1. is the data in data/repos/ the data the results were computed from
                              2. has any file in benchmark/ been changed (against SHA256SUMS); do the generated parts of
                                 the documents match the results
                              3. recompute everything in a separate process into a temporary directory and compare it
                                 with benchmark/ byte for byte

The pipeline has three inputs: the pinned commits in data/repos.toml, the repository data in data/repos/, and the
model's judgments and the human review verdicts in judgments/. Results contain no timestamps, durations or machine paths.
Recomputing is offline (`python -m fixprop fetch` has already fetched every file content that is needed).
"""

from __future__ import annotations

import filecmp
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import paths
from .common import log

SUMS = "SHA256SUMS"


# ------------------------------------------------------------------ the pipeline


def run_steps() -> int:
    """Run every step in order and write the results to paths.OUT. Returns the number of problems found."""
    from . import candidates, census, diverged, labels, missing, pairs, review
    from .codebases import ORDER, get

    bad = 0
    found: list[dict] = []
    for name in ORDER:
        cb = get(name)
        files = pairs.load_files(cb)
        census.step_pairs(cb, files)  # 1 find copied files
        _, rows = census.step_status(cb, files)  # 2, 3 find bug-fix commits; compare every fix with every copy
        cands = candidates.run(cb, rows)  # 4 the candidates
        bad += labels.merge(cb, cands)  # with their judgments; check the cited lines and the transcripts
        diverged.analyse(cb, files)  # 5 the diverged pairs: group, sample, merge the judgments
        sample = diverged.sample(cb, files)
        bad += labels.merge_diverged(cb, sample)
        labels.mark_repeats(cb, cands, sample)  # the same problem reached from two fixes is counted once
        found += missing.collect(cb)  # 6 the pairs flagged as missed fixes
    human, review_problems = review.merge(found)  # the human verdicts; the final benchmark
    summary, dup_problems = missing.write(found, human)  # the list; counts and estimates
    for msg in missing.consistency(summary):  # the numbers in the summary must agree
        log(f"[missing] numbers do not agree: {msg}")
        dup_problems += 1
    review.write_items()  # the content of the review page
    return bad + dup_problems + review_problems


# ------------------------------------------------------------------ checksums


def list_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != SUMS)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sums(root: Path) -> int:
    """benchmark/SHA256SUMS: one line per result file, in the format of sha256sum (so sha256sum -c can check it)."""
    names = list_files(root)
    (root / SUMS).write_text("".join(f"{sha256(root / n)}  {n}\n" for n in names), encoding="utf-8")
    return len(names)


def check_sums(root: Path) -> list[str]:
    f = root / SUMS
    if not f.is_file():
        return [f"{paths.rel(f)} does not exist"]
    want = {}
    for ln in f.read_text(encoding="utf-8").split("\n"):
        if ln.strip():
            digest, _, name = ln.partition("  ")
            want[name] = digest
    have = set(list_files(root))
    problems = [f"missing file {n}" for n in sorted(set(want) - have)]
    problems += [f"extra file {n}" for n in sorted(have - set(want))]
    problems += [f"changed file {n}" for n in sorted(have & set(want)) if sha256(root / n) != want[n]]
    return problems


def compare_dirs(a: Path, b: Path) -> list[str]:
    """Compare two directories file by file, byte for byte. Returns the differences."""
    fa, fb = set(list_files(a)) | {SUMS}, set(list_files(b)) | {SUMS}
    diffs = [f"only in {paths.rel(a)}: {n}" for n in sorted(fa - fb)]
    diffs += [f"only in {paths.rel(b)}: {n}" for n in sorted(fb - fa)]
    for n in sorted(fa & fb):
        if not ((a / n).is_file() and (b / n).is_file() and filecmp.cmp(a / n, b / n, shallow=False)):
            diffs.append(f"different: {n}")
    return diffs


# ------------------------------------------------------------------ recomputing


def _is_results_dir(d: Path) -> bool:
    """May d be replaced as a whole: it does not exist, is empty, or is a results directory this tool wrote before
    (it contains SHA256SUMS)."""
    return not d.exists() or (d.is_dir() and (not any(d.iterdir()) or (d / SUMS).is_file()))


def run_all() -> int:
    from . import readme

    final = paths.OUT
    if not _is_results_dir(final):
        print(f"[all] {final} contains other files and no {SUMS}; it does not look like a results directory. "
              f"Not overwriting it; stopping.", file=sys.stderr)
        return 2
    t0 = time.time()
    final.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=final.name + ".new.", dir=final.parent))
    try:
        paths.OUT = work  # write to a temporary directory and swap it in at the end: a failure leaves no partial results
        bad = run_steps()
        n = write_sums(work)
    except BaseException:
        paths.OUT = final
        shutil.rmtree(work, ignore_errors=True)
        raise
    paths.OUT = final
    old = final.with_name(final.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)
    work.rename(final)
    final.chmod(0o775)
    shutil.rmtree(old, ignore_errors=True)
    if final == paths.ROOT / "benchmark":  # update the documents only for the real results directory
        for rel in readme.sync(final):
            print(f"[all] updated the generated parts of {rel}.")
    print(f"[all] wrote {paths.rel(final)}/: {n} files in {time.time() - t0:.0f} seconds."
          + (f" {bad} problems in the judgments, see above." if bad else ""), flush=True)
    return 1 if bad else 0


# ------------------------------------------------------------------ checking


def check(keep: bool = False) -> int:
    from . import fetch, readme

    ok = True
    print("[check] 1. checking the repository data in data/repos/", flush=True)
    problems = fetch.verify()
    for p in problems:
        print("   ", p)
    print("    " + ("problems found" if problems else "ok: all at their pinned commits, histories match the record, and every "
                                                    "source file the analysis reads matches the pinned commit"),
          flush=True)
    ok &= not problems

    print(f"[check] 2. checking that the files in {paths.rel(paths.OUT)}/ are unchanged", flush=True)
    problems = check_sums(paths.OUT) + readme.stale(paths.OUT)
    for p in problems[:40]:
        print("   ", p)
    print("    " + (f"{len(problems)} mismatches" if problems else
                    f"ok: {len(list_files(paths.OUT))} files match {SUMS}; the generated parts of the documents match "
                    f"the results"), flush=True)
    ok &= not problems

    tmp = Path(tempfile.mkdtemp(prefix="fixprop-check."))
    out = tmp / "benchmark"
    print(f"[check] 3. recomputing everything in a separate process into {out} and comparing byte for byte "
          f"(a few minutes)", flush=True)
    # the child runs with -I: no PYTHON* environment variables, and a random hash seed each time. If any result depended
    # on the iteration order of a set or dict, it would show up here
    src = str(Path(__file__).resolve().parents[1])
    code = "import sys; sys.path.insert(0, sys.argv[1]); from fixprop.__main__ import main; sys.exit(main(['all']))"
    env = {**os.environ, "FIXPROP_OUT": str(out), "FIXPROP_ROOT": str(paths.ROOT)}
    log = tmp / "all.log"
    with log.open("w", encoding="utf-8") as fh:
        r = subprocess.run([sys.executable, "-I", "-c", code, src], env=env, cwd=paths.ROOT, stdout=fh,
                           stderr=subprocess.STDOUT)
    if r.returncode != 0:
        print(f"    the recomputation failed (exit code {r.returncode}); output in {log}")
        return 1
    diffs = compare_dirs(paths.OUT, out)
    for d in diffs[:60]:
        print("   ", d)
    if diffs:
        print(f"    {len(diffs)} differences. The recomputed results are kept in {out} for comparison.")
        return 1
    print(f"    ok: the {len(list_files(out))} recomputed files are byte-identical to {paths.rel(paths.OUT)}/, "
          f"and so is {SUMS}", flush=True)
    if keep:
        print(f"    the recomputed results are kept in {out}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[check] " + ("all three checks passed." if ok else "check 3 passed, but earlier checks found problems, see above."))
    return 0 if ok else 1
