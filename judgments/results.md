# Results

The results of the four steps described in the [README](../README.md). The two generated parts below are rewritten by `python -m fixprop all` and checked by `python -m fixprop check`; the rest of this file is written by hand.

## Summary

<!-- BEGIN:results -->
- **TODO.** No pair has been read yet: 650 candidates and 40 sampled diverged pairs are waiting.
- Human review: **TODO**, not started. Final benchmark: **TODO**; it will hold only the pairs confirmed by human review.
<!-- END:results -->

## Every pair flagged as a missed fix

Each row is one bug-fix commit together with one other driver whose copy still has the problem. "Fixed in" links to the fix commit, "Copy" to the problem line in the copy at the pinned commit. IDs with a D come from the sample of diverged pairs. The last column comes from the verdicts in `judgments/review/`. The full record of each pair (summary, hunks, the copy's current code, evidence and cited lines) is in `benchmark/missing.jsonl`, and the review page shows it side by side.

<!-- BEGIN:list -->
No pair has been flagged yet.
<!-- END:list -->

## Notes on the method

- **Bug-fix commits are found by the word "fix" in the commit message.** Fixes whose message does not contain it are not found. Commits whose message contains it but whose picked-out hunks add features or refactor are read and judged "not a bug fix".
- **Copies are found by text.** Two files count as copies when the shared lines reach 30% of the smaller file. Files that share mostly boilerplate pass this threshold; copies below it are not paired.
- **Hunks are compared by text.** A copy that has the same problem written differently does not match and becomes a diverged pair. Diverged pairs are only sampled, so missed fixes among them are estimated, not listed.
- **Repeats are found by line.** Two flagged pairs count as one problem when they are in the same copy and cover a common line. The same problem flagged at different lines counts twice.
- **A moved file is found by content.** When several files are equally close, the path that sorts first is taken. This keeps every run identical; the pick is not checked further.

## Output files

| File | Contents |
|---|---|
| `benchmark/final.jsonl` | The final benchmark: every pair reviewed by humans, with each reviewer's verdict and the final verdict. Empty until review starts |
| `benchmark/missing.jsonl` | One record per flagged pair: `id`; `title` and `summary`; `fix` (the commit); `copy` (driver, file and line); `port` (`verbatim` or `adapt`); `impact` (`runtime` or `light`, with `impact_note`); `places` (the hunks and the copy's current code); `evidence` and `evidence_refs`; `reader` (the model); `transcript` and `transcript_sha256` (the stored conversation); `same_problem_as` when the pair repeats another one |
| `benchmark/summary.json` | The counts of every step; the flagged pairs (`flagged`); the model, settings and token usage of the conversations (`reading`); the progress of human review (`human_review`) |
| `benchmark/review_items.json` | The content of the review page |
| `benchmark/<codebase>/candidates.jsonl` | Every candidate with its judgment and evidence, the path and checksum of its transcript, and the settings and token usage of its conversation (`reading`) |
| `benchmark/<codebase>/fix_status.jsonl` | One row per (bug-fix commit, changed file, file in another driver). `status` is `same_commit`, `has_fix`, `candidate`, `diverged` or `delete_only`; `hunks` holds each hunk's outcome and its line numbers in the copy |
| `benchmark/<codebase>/copy_pairs.jsonl` | The copied file pairs |
| `benchmark/<codebase>/diverged.jsonl` | Every diverged pair and its group |
| `benchmark/<codebase>/diverged_sample.jsonl` | The sampled diverged pairs with their judgments |
| `benchmark/<codebase>/stats.json` | The counts of each step |
| `benchmark/SHA256SUMS` | The checksum of every file above |

Commit subjects, commit messages and code are copied from the upstream repositories as they are. Some RT-Thread commit messages and code comments are in Chinese, so these fields contain Chinese text where the upstream text does.
