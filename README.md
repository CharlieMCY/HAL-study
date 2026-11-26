# Do bug fixes reach the copied drivers?

Embedded driver code is often copied from one driver to another. When a bug is fixed in one copy, the other copies do not always receive the fix. This repository finds such missed fixes in 40 open-source repositories, in four steps:

1. **Restore the data**: fetch the 40 repositories at pinned commits.
2. **Script analysis**: find copied files and bug-fix commits, and list every copy that still has the code a fix replaced.
3. **LLM analysis**: a language model reads both sides of each listed case through an API and decides whether the copy misses the fix. Each case is one conversation, and every request and response is stored.
4. **Human review**: reviewers check the model's verdicts. The final benchmark is the set confirmed by human review.

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

## Step 3. LLM analysis

### Approach

A candidate is a copy that still has the code a fix replaced. Reading both sides decides whether the copy has the same bug: it shows whether the matched hunks fix a bug, whether the fix depends on hardware the copy lacks, and whether the copy already handles the problem in another way. A language model reads every candidate and every sampled diverged pair, one pair per conversation.

For each pair a script extracts a packet from the repositories. A packet holds the commit message and link, the matched hunks, the lines the copy still has with their line numbers, the paths of both files and the pinned commits. The model opens both files with its tools, follows the reading rules and gives one of five verdicts:

| Verdict | Meaning |
|---|---|
| `missing` | missed fix: the copy has the same problem, and nothing in the repository shows that the fix does not apply |
| `not_applicable` | the fix depends on hardware, configuration or features that the copy does not have |
| `not_a_fix` | the matched hunks rename, add a feature, reformat, refactor, or change logging or comments |
| `fixed_differently` | the copy already handles the problem in another way |
| `unsure` | the repository does not hold enough evidence to decide |

The reading rules (the `GUIDE` text in `src/fixprop/packets.py`):

- Evidence comes from the repository only: register definitions, conditional compilation, Kconfig, Cargo features, devicetree bindings, commit messages, comments and code. Remembered hardware knowledge does not count, and without repository evidence the verdict is `unsure`.
- Every verdict cites at least one file line with its exact text. The cited lines are checked against the pinned commit when the model submits its judgment, and again by `fixprop all` on each run.
- The model only reads. Its tools return file lines, directory listings, search results and commit changes; it has no way to build or run anything and no web access.
- A `missing` verdict also records whether the patch ports as is or needs adapting, whether the problem affects runtime behaviour or only builds, debug commands and log text, the file and line in the copy, a one-line title and a short summary.
- The model writes in English. Some upstream commit messages and comments are in Chinese, and the model translates what it uses.

Sampled diverged pairs follow a second set of rules, the `GUIDE_DIVERGED` text in the same file. It has the same evidence rules and six verdicts, and the model first locates the matching code in the copy.

One unfixed piece of code can be reached from two fix commits, for example when one fix was applied to several chips at different times. A script counts such a problem once. A flagged candidate covers the lines of the copy that still hold the pre-fix code, and a flagged sampled diverged pair covers the line the model gave as the location of the problem. Flagged pairs in the same copy that cover a common line are one problem: the pair with the earliest fix is counted, and the others are marked `same_problem_as` and left out of "distinct problems".

### Commands

`python -m fixprop read` talks to the [OpenRouter](https://openrouter.ai) chat completions API.

```bash
cp .env.example .env    # put the key in .env as OPENROUTER_API_KEY=..., or export OPENROUTER_API_KEY in the shell

.venv/bin/python -m fixprop read --out judgments --model <model>            # read every pair that has no accepted transcript yet
.venv/bin/python -m fixprop read --out judgments --collect                  # build the judgment files from the transcripts
.venv/bin/python -m fixprop all                                             # merge them, check each one against its transcript, update the tables

.venv/bin/python -m fixprop read --out judgments --model <model> --request nuttx-031   # print the first request of one pair; sends nothing
.venv/bin/python -m fixprop read --out judgments --show nuttx-031           # print one conversation
.venv/bin/python -m fixprop read --out judgments --usage                    # calls, tokens and cost per codebase
```

`<model>` is the model's name at OpenRouter, for example `anthropic/claude-opus-5.5`. `read` can be stopped and started again: it skips the pairs that already have an accepted transcript. `--codebase`, `--kind`, `--ids` and `--limit` select a subset, and `--max-cost` starts no new pair once the run has cost the given amount. Another directory, for example `--out work/pilot`, keeps a trial run apart; `work/` is ignored by git.

| Setting | Option of `read` | Default |
|---|---|---|
| Model | `--model` | none; required |
| Reasoning effort | `--effort` | `medium`, sent as `reasoning.effort` |
| Output limit of one response | `--max-tokens` | 32000 |
| Provider | `--provider` | `anthropic`: only this provider serves the requests, with no fallback |
| Prompt caching | | automatic (`cache_control`) for `anthropic/` models |
| Conversations at the same time | `--jobs` | 4 |
| Cost limit of a run | `--max-cost` | none |

No sampling parameter such as temperature is sent. The settings each conversation was run with are stored in its transcript, and the Results section below states them for the stored judgments.

### What one conversation contains

- The system message: the reading rules, `GUIDE` or `GUIDE_DIVERGED`.
- The first user message: the pair's packet.
- Tool calls by the model, until it submits a judgment that passes the checks.

The model has six tools, defined in `src/fixprop/reader.py`. Five of them read: lines of a file at a commit, a directory listing, a text search in the local checkout, the change a commit made to a file, and the latest commits of a file. Each takes a repository name and a relative path, validates both, and runs one fixed `git` or `grep` command. The sixth, `submit_judgment`, checks the judgment and returns the problems to the model, which corrects them and submits again. The checks require every field, find every quoted line at the cited line of the pinned commit, and reject non-English text.

`read` writes one transcript per pair, `<out>/<codebase>/transcripts/<id>.json.gz`:

- the fixed part of the request: model, tools and settings;
- every message in order: the rules, the packet, each reply of the model as received with its reasoning, and each tool result;
- for each API call, the number of messages that were sent and the complete response, with its token usage and cost.

Messages are only ever appended, so request *k* is the fixed part plus the first `messages_sent` messages of call *k*. A conversation that ends without an accepted judgment is kept as `<id>.failed-<n>.json.gz`. The API key is read from `OPENROUTER_API_KEY` or from `.env`, is sent only in the `Authorization` header, and is never stored.

`--collect` writes each accepted judgment into `<out>/<codebase>/labels.jsonl` (`labels_diverged.jsonl` for sampled diverged pairs) with the model's name and the SHA-256 of its transcript. On every run, `fixprop all` checks that each judgment has its transcript, that the checksum matches, and that the judgment is the one accepted in that conversation.

Reading is not deterministic: a second run gives its own judgments. The stored judgments and conversations in `judgments/` are therefore an input of `fixprop all`, which reproduces every number below from them byte for byte.

### Results

<!-- BEGIN:model -->
**TODO.** No pair has been read yet: 650 candidates and 40 sampled diverged pairs are waiting.
<!-- END:model -->

`benchmark/missing.jsonl` has one record per flagged pair: what was fixed, where the copy is, the hunks and the copy's current code, port, impact, evidence, the model and the transcript. [judgments/results.md](judgments/results.md) lists every flagged pair with links.

## Step 4. Human review (TODO)

The final benchmark is the set of pairs confirmed by human review. The human-review column below is filled in from the reviewers' verdicts by `fixprop all` and shows TODO until review starts.

<!-- BEGIN:human -->
| | LLM (step 3) | Human review |
|---|---|---|
| Fix–copy pairs that are missed fixes | TODO | TODO |
| Distinct problems | TODO | TODO |
| Distinct problems that affect runtime behaviour | TODO | TODO |
| Distinct problems whose patch ports as is | TODO | TODO |
| Flagged pairs confirmed by reviewers | — | TODO |

Review has not started: step 3 has not flagged any pair yet.
<!-- END:human -->

```bash
.venv/bin/python -m fixprop review --reviewer <your-name>   # then open http://127.0.0.1:8765/
.venv/bin/python -m fixprop all                              # merge the verdicts into benchmark/final.jsonl and update the tables
```

The review page runs locally, is reachable only from your machine, and needs no data fetch. It lists the flagged pairs first, then the other candidates and the sampled diverged pairs. For each pair it shows:

- **The fix**: what the bug-fix commit changed in the original file.
- **The unfixed copy**: the copy's code at the pinned commit, with the pre-fix lines highlighted.
- **The model's verdict** and its evidence. It can be hidden for blind review, and each verdict records whether the reviewer saw it.

The verdicts are "missed fix", "not a missed fix" (with a reason) and "unsure". Each verdict is saved immediately to `judgments/review/<your-name>.jsonl`. Several reviewers can work under different names, and `all` marks the pairs on which they disagree. `benchmark/final.jsonl` holds the human-reviewed pairs only.

## Repository layout

| Path | Contents |
|---|---|
| `data/repos.toml` | The 40 repositories and their pinned commits |
| `data/repos.lock.json` | The expected shape of the fetched history, used by `fetch --verify` |
| `data/repos/` | The repositories (git submodules), filled in by `fetch` |
| `judgments/<codebase>/` | The model's judgments (`labels.jsonl`, `labels_diverged.jsonl`) and the conversation behind each one (`transcripts/`), written by `fixprop read` |
| `judgments/review/` | Human review verdicts. The review page creates it when the first verdict is saved |
| `judgments/results.md` | The results: a summary, every flagged pair with links, notes on the method, and the output files |
| `benchmark/` | All computed results, generated by `fixprop all`. The final benchmark is `final.jsonl` |
| `.env.example` | Template for `.env`, the file that holds the API key for step 3; git ignores `.env` |
| `src/fixprop/` | The analysis code |
| `tests/` | Unit tests |
