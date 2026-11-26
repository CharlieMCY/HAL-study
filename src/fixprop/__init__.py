"""Bug fixes in embedded drivers that were not carried over to copies of the same code.

When a driver file is copied into another driver and a bug is later fixed in one copy, the other copy can keep the bug.
This package finds such cases: copies that still have the problem and have not been fixed.

  fetch.py      the data: 40 upstream repositories, pinned to the commits in data/repos.toml
  pairs.py      step 1  find files copied between drivers
  census.py     steps 2 and 3  find bug-fix commits; compare every fix with every copy (hunks.py holds the rules for one hunk)
  candidates.py step 4  list every candidate (copy still has the pre-fix code) and give it a fixed ID
  packets.py            the reading material, instructions and batch prompts for Claude Code; checking a reader's output
  diverged.py   step 5  group and sample the diverged pairs for reading
  labels.py     merge the judgments (judgments/) and check every cited line
  missing.py    step 6  the list of flagged missed fixes; counts and estimates
  review.py     step 7  the local human review page; merging the human verdicts into the final benchmark
  pipeline.py   the whole pipeline (all), and the check that a recomputation gives the same bytes (check)

Inputs are data/repos.toml, data/repos/ and judgments/; every output goes to benchmark/.
The upstream repositories are read-only: only read-only git operations and file reads, nothing is built or run.
"""
