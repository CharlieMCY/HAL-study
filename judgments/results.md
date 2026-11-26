# Results

The results of the four steps described in the [README](../README.md). The two generated parts below are rewritten by `python -m fixprop all` and checked by `python -m fixprop check`; the rest of this file is written by hand.

## Summary

<!-- BEGIN:results -->
- **TODO.** No pair has been read yet: 749 candidates and 40 sampled diverged pairs are waiting.
- Human review: **TODO**, not started. Final benchmark: **TODO**; it will hold only the pairs confirmed by human review.
<!-- END:results -->

## Every pair flagged as a missed fix

Each row is one bug-fix commit together with one other driver whose copy still has the problem. "Fixed in" links to the fix commit, "Copy" to the problem line in the copy at the pinned commit. IDs with a D come from the sample of diverged pairs. The last column comes from the verdicts in `judgments/review/`. The full record of each pair (summary, hunks, the copy's current code, evidence and cited lines) is in `benchmark/missing.jsonl`, and the review page shows it side by side.

<!-- BEGIN:list -->
No pair has been flagged yet.
<!-- END:list -->

## Notes on the method

- **Bug-fix commits are found by the word "fix" in the commit message.** Fixes whose message does not contain it are not found, and merge commits are not analysed. Commits whose message contains it but whose changes add features or refactor are read and judged "not a bug fix". Commits that take another commit back (subject `Revert "..."`) are left out.
- **Copies are found by text, one whole file against another.** Two files count as copies when the shared lines reach 30% of the smaller file. Files that share mostly boilerplate pass this threshold. A driver that copied a single function out of a large file stays below it and is not paired. Copies are judged at the pinned commits only, so two files that have grown apart to less than 30% are not paired, whatever they shared earlier.
- **Places are compared by text.** A copy that has the same problem written differently does not match and becomes a diverged pair. Only a few diverged pairs are read, and no total is derived from them. Changed lines shorter than 16 characters (Rust) or 18 characters (C) are not compared, so a fix that changes only such a line is not seen.
- **The four codebases are not screened by identical rules, so their counts are not comparable with each other.**
  - A driver is a crate in Rust, a chip directory in NuttX, one `.c` file in Zephyr and a BSP directory in RT-Thread.
  - Zephyr files are compared only with files in the same `drivers/<subsystem>/` directory.
  - Names are replaced only in the C codebases. NuttX and RT-Thread replace a chip or vendor prefix at the start of an identifier. Zephyr replaces the parts of the file name wherever they occur, which also alters ordinary words that contain a short driver name (the driver name `ite` inside `write`).
  - Lines found in many files do not count as shared: in Rust, lines found in more than 12 files; in Zephyr, lines found in more than 6 files, or in more than a quarter of the directory's files where that is larger. NuttX and RT-Thread count such lines.
  - In the C codebases, a place where the fix replaces a single line shorter than 24 characters is not treated as "not changed yet".
- **Repeats are found by line.** Two flagged pairs count as one problem when they are in the same copy and cover a common line. The same problem flagged at different lines counts twice.
- **A moved file is found by content.** When several files are equally close, the path that sorts first is taken. This keeps every run identical; the pick is not checked further.
- **The groups of diverged pairs go by function name.** A function of the same name in the copy's file counts as the fixed function. In Rust, functions of the same name in different `impl` blocks are not told apart.

## Output files

| File | Contents |
|---|---|
| `benchmark/final.jsonl` | The final benchmark: every pair reviewed by humans, with each reviewer's verdict and the final verdict. Empty until review starts |
| `benchmark/missing.jsonl` | One record per flagged pair: `id`; `title` and `summary`; `fix` (the commit); `copy` (driver, file and line); `port` (`verbatim` or `adapt`); `impact` (`runtime` or `light`, with `impact_note`); `places` (the hunks and the copy's current code); `evidence` and `evidence_refs`; `reader` (the model); `transcript` and `transcript_sha256` (the stored conversation); `same_problem_as` when the pair repeats another one |
| `benchmark/summary.json` | The counts of every step; the flagged pairs (`flagged`); the model, settings and token usage of the conversations (`reading`); the progress of human review (`human_review`) |
| `benchmark/review_items.json` | The content of the review page |
| `benchmark/<codebase>/candidates.jsonl` | Every candidate: the places that are not changed yet, with their lines in the copy; `source_still_fixed` (false when the fixed driver has changed the fixed code since); `same_fix` (the other candidates from the same bug-fix commit); and, once read, the judgment and evidence, the path and checksum of its transcript, and the settings and token usage of its conversation (`reading`) |
| `benchmark/<codebase>/fix_status.jsonl` | One row per (bug-fix commit, changed file, file in another driver). `status` is `same_commit` (changed together), `has_fix` (already fixed), `candidate` (not changed yet) or `diverged`; `hunks` holds the outcome of each place and its line numbers in the copy |
| `benchmark/<codebase>/copy_pairs.jsonl` | The copied file pairs |
| `benchmark/<codebase>/diverged.jsonl` | Every diverged pair and its group |
| `benchmark/<codebase>/diverged_sample.jsonl` | The sampled diverged pairs with their judgments |
| `benchmark/<codebase>/stats.json` | The counts of each step |
| `benchmark/SHA256SUMS` | The checksum of every file above |

Commit subjects, commit messages and code are copied from the upstream repositories as they are. Some RT-Thread commit messages and code comments are in Chinese, so these fields contain Chinese text where the upstream text does.
