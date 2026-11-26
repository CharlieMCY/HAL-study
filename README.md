# Do bug fixes reach the copied drivers?

Embedded driver code is often copied from one driver to another. When a bug is fixed in one copy, the other copies do not always receive the fix. This repository finds such missed fixes in 40 open-source repositories, in four steps:

1. **Restore the data**: fetch the 40 repositories at pinned commits.
2. **Script analysis**: find copied files and bug-fix commits, and list every copy that still has the code a fix replaced.
3. **Claude Code analysis**: Claude Code reads both sides of each listed case and decides whether the copy misses the fix.
4. **Human review**: reviewers check Claude Code's verdicts. The final benchmark is the set confirmed by human review.

The 40 repositories form four codebases:

| Codebase | Repositories | One driver is |
|---|---|---|
| Rust HAL | 37 Rust HAL repositories (embassy, the stm32-rs HALs, rp-hal, esp-hal and others). They copy code from each other, so they are analysed together as one codebase | a crate |
| NuttX | 1: `apache/nuttx` | a chip directory under `arch/<arch>/src/` |
| Zephyr | 1: `zephyrproject-rtos/zephyr` | a `.c` file under `drivers/<subsystem>/` |
| RT-Thread | 1: `RT-Thread/rt-thread` | a directory under `bsp/` that holds `drv_*.c` files |

The analysis covers the non-merge commits from 2023-06-01 up to a pinned commit in each repository (November 2026). The pinned commits are listed in `data/repos.toml`. The analysis reads source code and git history; nothing is built or run.

## Step 1. Restore the data

The 40 repositories are git submodules under `data/repos/`, pinned to the commits in `data/repos.toml`. A fresh clone of this repository leaves them empty, and `fetch` fills them.

```bash
git clone <this repository> && cd <directory>
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"

.venv/bin/python -m fixprop fetch            # fetch the 40 repositories: about 750 MB, a few minutes
.venv/bin/python -m fixprop fetch --verify   # check the data against the record (offline)
```

Requirements: git and Python 3.10 or later. The results were computed with git 2.34 and Python 3.10. `fetch` uses your own git configuration for network access, including any proxy.

For each repository, `fetch`:

- fetches the history since 2023-06-01 without file contents, then checks out the pinned commit. For the three large repositories it checks out only what the analysis reads: `arch/` in NuttX, `drivers/` in Zephyr and `bsp/**/drv_*.c` in RT-Thread;
- downloads the older file versions that step 2 compares against, so steps 2 to 4 run offline;
- skips repositories that are already present, so an interrupted fetch can be restarted.

Use `fetch`, not `git submodule update`. `git submodule update` downloads the full history and every file of each repository (several GB), and that history does not match the record, so verification fails. Every submodule is marked `update = none` in `.gitmodules`, which makes `git clone --recursive` skip them.

`fetch --verify` checks that every repository is at its pinned commit, that the submodule commits match `data/repos.toml`, that the history cut-off and commit count match `data/repos.lock.json`, and that all 12,268 source files the analysis reads match the pinned commits.

## Step 2. Script analysis

### How the scripts screen

1. **Copied files.** At the pinned commits, two files in different drivers are copies when they share at least 30 lines and the shared lines make up at least 30% of the smaller file. Before comparing, whitespace is collapsed and each driver's own chip and vendor names are replaced by a placeholder, so `stm32l4_i2c_init` matches `at32_i2c_init`. Comments, `#include` and `use` lines, and lines shorter than 28 characters are not counted. Details: top of `src/fixprop/pairs.py`.
2. **Bug-fix commits.** A bug-fix commit is a non-merge commit since 2023-06-01 that changes an analysed source file and whose message contains "fix" not preceded by a letter, or "bugfix" or "hotfix". "Fixes" and "fixed" count; "prefix" and "suffix" do not.
3. **Fix–copy pairs.** A fix–copy pair is one bug-fix commit together with one other driver that holds a copy of a file the commit changed. Every hunk the commit changed is compared with the copy, with comments removed and names normalized. Each pair gets one outcome:
   - **fixed in the same commit**: the commit also changed the copy;
   - **copy already has the fixed code**: every added line is in the copy;
   - **candidate**: the copy still has the pre-fix code. The removed lines are in the copy, each next to at least one of its original neighbours, and the added lines are not. For a hunk that only adds lines, the three lines before it appear together in the copy and none of the added lines do;
   - **diverged**: the copy has neither the pre-fix code nor the fixed code;
   - **delete-only**: the fix only removes lines, and the copy still has them. In Rust these are candidates; the C codebases count them separately.

   A pair with at least one candidate hunk is a candidate. Details: top of `src/fixprop/hunks.py`.
4. **Diverged sample.** The diverged pairs are put into three groups: near (the fixed function exists in the copy and most of the code around the change matches), far (the function exists, most of the code around the change differs) and gone (the copy has no such function). Each codebase contributes 10 pairs to step 3 (4 near, 3 far, 3 gone), drawn with a fixed seed. Pairs whose changed function cannot be identified are not sampled.

### Commands

```bash
.venv/bin/python -m fixprop all     # compute every result into benchmark/ (offline, 1-2 minutes)
.venv/bin/python -m fixprop check   # recompute in a separate process and compare with benchmark/ byte for byte (2-3 minutes)
.venv/bin/python -m pytest          # unit tests (seconds, no data needed)
```

`all` runs the script analysis, merges the stored results of steps 3 and 4, and rewrites the result tables in this README and in `judgments/results.md`. `check` runs the `fetch --verify` checks, compares `benchmark/` with `benchmark/SHA256SUMS` and the tables here with the results, and then recomputes.

### Which numbers are deterministic

Every number in this step is deterministic: the same data gives byte-identical output on every run and on every machine. The reasons:

- Each repository is pinned to one commit, and the history starts at a fixed instant, 2023-06-01 00:00:00 UTC.
- git runs without the user's global configuration and with fixed diff options (Myers algorithm, 3 lines of context).
- Every sort and tie-break is explicit, and the diverged sample uses a fixed seed (20261124).
- The output contains no timestamps or machine paths.

`check` tests this on every run. It recomputes everything in a fresh Python process with a random hash seed and compares the output with `benchmark/` byte for byte.

### Results

<!-- BEGIN:script -->
| | Rust HAL | NuttX | Zephyr | RT-Thread | Total |
|---|---|---|---|---|---|
| Repositories | 37 | 1 | 1 | 1 | 40 |
| Source files analysed | 4,459 | 2,937 | 3,627 | 1,245 | 12,268 |
| Drivers | 227 | 159 | 3,627 | 246 | 4,259 |
| Copied file pairs | 217 | 3,759 | 553 | 670 | 5,199 |
| Bug-fix commits | 3,515 | 912 | 4,284 | 161 | 8,872 |
| — that changed a file with a copy | 409 | 557 | 1,010 | 62 | 2,038 |
| Fix–copy pairs | 529 | 3,042 | 2,173 | 215 | 5,959 |
| — fixed in the same commit | 47 | 528 | 372 | 47 | 994 |
| — copy already has the fixed code | 84 | 506 | 316 | 8 | 914 |
| — **candidate: copy still has the pre-fix code** | 76 | 401 | 141 | 32 | 650 |
| — diverged | 322 | 1,606 | 1,338 | 128 | 3,394 |
| — delete-only | 0 | 1 | 6 | 0 | 7 |
<!-- END:script -->

Output for each codebase, in `benchmark/<codebase>/`:

- `copy_pairs.jsonl`: the copied files;
- `fix_status.jsonl`: each fix commit, changed file and copy, with the outcome of every hunk;
- `candidates.jsonl`: the candidates;
- `diverged.jsonl`: the diverged pairs and their groups;
- `stats.json`: the counts.

## Step 3. Claude Code analysis

### Approach

A candidate is a copy that still has the code a fix replaced. Reading both sides decides whether the copy has the same bug: it shows whether the matched hunks fix a bug, whether the fix depends on hardware the copy lacks, and whether the copy already handles the problem in another way. Claude Code read all 650 candidates and the 40 sampled diverged pairs.

`fixprop packets` writes one packet per pair, extracted from the repositories by script. A packet holds the commit message and link, the matched hunks, the lines the copy still has with their line numbers, the local paths of both files and the pinned commits. The reader opens both files, follows the reading rules and gives one of five verdicts:

| Verdict | Meaning |
|---|---|
| `missing` | missed fix: the copy has the same problem, and nothing in the repository shows that the fix does not apply |
| `not_applicable` | the fix depends on hardware, configuration or features that the copy does not have |
| `not_a_fix` | the matched hunks rename, add a feature, reformat, refactor, or change logging or comments |
| `fixed_differently` | the copy already handles the problem in another way |
| `unsure` | the repository does not hold enough evidence to decide |

The reading rules (the `GUIDE` text at the top of `src/fixprop/packets.py`):

- Evidence comes from the repository only: register definitions, conditional compilation, Kconfig, Cargo features, devicetree bindings, commit messages, comments and code. Remembered hardware knowledge does not count, and without repository evidence the verdict is `unsure`.
- Every verdict cites at least one file line with its exact text. `fixprop all` checks every cited line against the pinned commit on each run.
- Readers only read: no building or running, read-only git commands, no web access.
- A `missing` verdict also records whether the patch ports as is or needs adapting, whether the problem affects runtime behaviour or only builds, debug commands and log text, the file and line in the copy, a one-line title and a short summary.

Diverged pairs follow a second set of rules, the `GUIDE_DIVERGED` text in the same file. It has the same evidence rules and six verdicts, and the reader first locates the matching code in the copy.

One unfixed piece of code can be reached from two fix commits, for example when one fix was applied to several chips at different times. The flagged pairs that point at the same code in the same copy are compared pairwise by the main Claude Code session, with the same model. A duplicate is marked `same_problem_as` and counted once under "distinct problems".

### Commands and instructions

```bash
.venv/bin/python -m fixprop packets --out work/read --all       # reading material, rules and batch prompts for all 690 pairs
.venv/bin/python -m fixprop packets --out work/read --verify work/read/results/<batch>.jsonl --batch <batch>
                                                                 # check one batch's output
.venv/bin/python -m fixprop packets --out work/read --collect claude-opus-5-5
                                                                 # check every batch, copy the judgments into judgments/
.venv/bin/python -m fixprop all                                 # merge judgments/ into benchmark/ and recheck every cited line
```

The instructions given to Claude Code are stored in `src/fixprop/packets.py`: `GUIDE` holds the reading rules, `GUIDE_DIVERGED` the rules for diverged pairs, and `TASK` and `TASK_DIVERGED` the prompt templates for one batch. `packets` writes the following into `work/read/`, which git ignores:

- one packet per pair;
- the two sets of reading rules;
- `index.jsonl`, one line per packet with its batch;
- `batches/<batch>.txt`, the complete prompt of each batch.

The pairs are split into 82 batches: per codebase and kind, in ID order, into batches of at most 9 pairs whose sizes differ by at most one. Each batch went to one Claude Code sub-agent:

| Setting | Value |
|---|---|
| Tool | Claude Code 2.1.322; sub-agents started with its Agent tool |
| Model | Claude Opus 5.5, model ID `claude-opus-5-5`, for every batch |
| Sub-agent type | `general-purpose` (Claude Code's default tools: reading and searching files, shell commands) |
| Reasoning effort | not overridden (Claude Code's default for sub-agents) |
| Prompt | `Read work/read/batches/<batch>.txt in full and follow it exactly; it is your complete task.` |
| Batches | 74 batches of candidates (8 or 9 pairs each) and 8 batches of diverged pairs (5 each) |
| Parallelism | at most 20 sub-agents at a time; a sub-agent does not see the output of other batches |
| Date | 2026-11-25 |

Each sub-agent read its batch prompt and the reading rules, wrote one JSON line per pair to `work/read/results/<batch>.jsonl`, and ran the `--verify` command until it reported no problems. The check requires every field, finds every quoted line at the cited line of the pinned commit, and rejects Chinese characters in any field: some upstream commit messages and comments are in Chinese, and the readers translate what they use. `packets --collect` then checked every batch again and copied the output into `judgments/<codebase>/labels.jsonl` (`labels_diverged.jsonl` for diverged pairs), with `"reader": "claude-opus-5-5"` on every judgment.

Reading is not deterministic: a new reading run produces its own judgments. The judgments behind the numbers below are stored in `judgments/`, and `fixprop all` reproduces the tables from them byte for byte.

### Results

<!-- BEGIN:claude -->
| | Rust HAL | NuttX | Zephyr | RT-Thread | Total |
|---|---|---|---|---|---|
| Candidates read | 76 | 401 | 141 | 32 | 650 |
| — **flagged as a missed fix** | 15 | 71 | 25 | 13 | 124 |
| — not a bug fix | 29 | 205 | 26 | 13 | 273 |
| — fix does not apply to the copy | 29 | 106 | 60 | 6 | 201 |
| — copy already handles it differently | 3 | 16 | 27 | 0 | 46 |
| — unsure | 0 | 3 | 3 | 0 | 6 |
| Diverged pairs sampled and read | 10 | 10 | 10 | 10 | 40 |
| — flagged as a missed fix | 2 | 0 | 1 | 0 | 3 |
| **Flagged as missed fixes, total** | 17 | 71 | 26 | 13 | 127 |
| **Distinct problems** (same problem reached from two fixes counted once) | 17 | 66 | 24 | 13 | 120 |

Of the 120 distinct problems, 80 affect runtime behaviour and 40 affect only builds, debug or test commands, or log text. For 76 the original patch ports as is; 44 need it adapted to the copy's code.

The 40 sampled diverged pairs were drawn by group from the 3,155 diverged pairs whose changed function could be identified (of 3,394). Scaling the sample up by group gives about 212 missed fixes among those pairs; they are not part of the totals above.
<!-- END:claude -->

`benchmark/missing.jsonl` has one record per flagged pair: what was fixed, where the copy is, the hunks and the copy's current code, port, impact, evidence and reader. [judgments/results.md](judgments/results.md) lists every flagged pair with links.

## Step 4. Human review (TODO)

The final benchmark is the set of pairs confirmed by human review. The human-review column below is filled in from the reviewers' verdicts by `fixprop all` and shows TODO until review starts.

<!-- BEGIN:human -->
| | Claude Code (step 3) | Human review |
|---|---|---|
| Fix–copy pairs that are missed fixes | 127 | TODO |
| Distinct problems | 120 | TODO |
| Distinct problems that affect runtime behaviour | 80 | TODO |
| Distinct problems whose patch ports as is | 76 | TODO |
| Flagged pairs confirmed by reviewers | — | TODO |

Reviewed so far: 0 of 127 flagged pairs.
<!-- END:human -->

```bash
.venv/bin/python -m fixprop review --reviewer <your-name>   # then open http://127.0.0.1:8765/
.venv/bin/python -m fixprop all                              # merge the verdicts into benchmark/final.jsonl and update the tables
```

The review page runs locally, is reachable only from your machine, and needs no data fetch. It lists the flagged pairs first, then the other candidates and the sampled diverged pairs. For each pair it shows:

- **The fix**: what the bug-fix commit changed in the original file.
- **The unfixed copy**: the copy's code at the pinned commit, with the pre-fix lines highlighted.
- **Claude Code's verdict** and its evidence. It can be hidden for blind review, and each verdict records whether the reviewer saw it.

The verdicts are "missed fix", "not a missed fix" (with a reason) and "unsure". Each verdict is saved immediately to `judgments/review/<your-name>.jsonl`. Several reviewers can work under different names, and `all` marks the pairs on which they disagree. `benchmark/final.jsonl` holds the human-reviewed pairs only.

## Repository layout

| Path | Contents |
|---|---|
| `data/repos.toml` | The 40 repositories and their pinned commits |
| `data/repos.lock.json` | The expected shape of the fetched history, used by `fetch --verify` |
| `data/repos/` | The repositories (git submodules), filled in by `fetch` |
| `judgments/<codebase>/` | Claude Code's judgments with their evidence |
| `judgments/review/` | Human review verdicts. The review page creates it when the first verdict is saved |
| `judgments/results.md` | The results: a summary, every flagged pair with links, notes on the method, and the output files |
| `benchmark/` | All computed results, generated by `fixprop all`. The final benchmark is `final.jsonl` |
| `src/fixprop/` | The analysis code |
| `tests/` | Unit tests |
