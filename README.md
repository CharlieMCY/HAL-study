# Do bug fixes reach the copied drivers?

Embedded driver code is often copied from one driver to another. When a bug is fixed in one copy, the other copies do not always receive the fix. This repository finds such missed fixes in 40 open-source repositories, in four steps:

1. **Restore the data**: fetch the 40 repositories at pinned commits.
2. **Script analysis**: find copied files and bug-fix commits, and list every copy that, going by its text, has not followed a fix.
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

### Approach

The problem to find is this: a bug was fixed in one driver, and another driver that holds a copy of the same code did not get the fix.

A script cannot settle that by itself. It reads source text and commit history, not meaning. A copy may have solved the problem in another way, or its hardware may not need the fix at all. So this step does a narrower job: it picks out every case in which the copy, going by its text, has not followed the fix, and it is built to miss as few of those as it can. Whether such a case is a missed fix is decided afterwards, by the model in step 3 and by reviewers in step 4.

The screening has three parts.

**1. Find copied files.** Each codebase is split into drivers along its own directory layout (see the table above). Two files in different drivers are copies when they share at least 30 counted lines and those lines make up at least 30% of the smaller file. Before lines are compared, runs of whitespace are collapsed, and in the three C codebases each driver's own chip and vendor prefixes are replaced by a placeholder, so `stm32l4_i2c_init` matches `at32_i2c_init`. Comment lines, `#include` and `use` lines, and lines shorter than 28 characters are not counted. Details: top of `src/fixprop/pairs.py`.

**2. Find bug-fix commits.** A bug-fix commit is a non-merge commit since 2023-06-01 that changes driver source and whose message contains "fix", "bugfix" or "hotfix". "Fixes" and "fixed" count; "prefix" and "suffix" do not. A commit that takes another commit back, with a subject of the form `Revert "..."`, is left out. The filter is loose on purpose: it lets in commits that only refactor or add features, which step 3 sorts out, so that fewer real fixes are lost.

**3. Compare every fix with every copy.** A bug-fix commit changes a file in one or more places. Each place is looked up in every copy of that file, after the same cleaning and with comments removed. What the copy has at that place gives one of four outcomes:

| Outcome | Meaning |
|---|---|
| changed together | the bug-fix commit also changed the copy |
| already fixed | at the place of the change, the copy has the code from after the fix |
| **not changed yet** | at the place of the change, the copy still has the code from before the fix. Where the fix only adds lines: the lines around the insertion point are there, with nothing added between them |
| diverged | at the place of the change, the copy has neither. The two files have grown apart here |

A line counts as being at the place only when it sits next to one of its neighbouring lines from the fix. The same statement somewhere else in the file does not count, because an unlock call or an error return appears in many functions. The code from before the fix is looked for first: a copy that has the new lines somewhere else and the old code at the place of the change is "not changed yet". Details: top of `src/fixprop/hunks.py`.

A bug-fix commit together with one driver that holds a copy is a **fix–copy pair**. A pair with at least one place that is not changed yet is a **candidate**, and the candidates go to step 3. Every other pair takes the first outcome that fits one of its places, in the order changed together, already fixed, diverged.

For each candidate the scripts record two more things. The first is whether the driver that was fixed has changed the fixed code since the fix; step 3 shows this to the model. The second is which other candidates come from the same bug-fix commit, so that results can be counted per fix as well as per pair.

**The diverged pairs.** The text comparison cannot say what happened to them, and they are too many to read. They are put into three groups: near (the fixed function exists in the copy and most of the code around the change matches), far (the function exists, but most of the code around the change differs) and gone (the copy has no such function). From each codebase, 10 of them go to step 3 (4 near, 3 far, 3 gone), drawn with a fixed seed. Pairs whose changed function cannot be identified are not sampled. The sample shows what the text comparison leaves open. It is too small to say how many missed fixes the diverged pairs hold.

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
| Bug-fix commits | 3,511 | 910 | 4,269 | 161 | 8,851 |
| — that changed a file with a copy | 409 | 556 | 1,006 | 62 | 2,033 |
| Fix–copy pairs | 529 | 3,034 | 2,157 | 215 | 5,935 |
| — changed together | 47 | 526 | 367 | 47 | 987 |
| — already fixed | 62 | 345 | 231 | 5 | 643 |
| — **not changed yet (candidates)** | 85 | 463 | 169 | 32 | 749 |
| — diverged | 335 | 1,700 | 1,390 | 131 | 3,556 |
| Bug-fix commits the candidates come from | 72 | 155 | 113 | 17 | 357 |
| Candidates where the fixed driver has changed the fixed code since | 19 | 30 | 27 | 2 | 78 |
<!-- END:script -->

Output for each codebase, in `benchmark/<codebase>/`:

- `copy_pairs.jsonl`: the copied files;
- `fix_status.jsonl`: each bug-fix commit, changed file and copy, with the outcome of every place;
- `candidates.jsonl`: the candidates, each with the lines of the copy that are not changed yet, whether the fixed driver has rewritten the fix since, and the other candidates from the same commit;
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
**TODO.** No pair has been read yet: 749 candidates and 40 sampled diverged pairs are waiting.
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
