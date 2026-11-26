"""Where things are in the project.

  data/repos.toml              the 40 upstream repositories and the commit each one is pinned to
  data/repos.lock.json         what the fetched history should look like (where it is cut off, how many commits), for checking
  data/repos/<owner>/<repo>/   the upstream repositories as git submodules: the files at the pinned commit, plus the history
                               since 2023-06-01. Read-only
  judgments/                   the judgments written after reading the code. An input of the pipeline; never modified by it
  benchmark/                   the computed results. The whole directory can be deleted and recomputed byte for byte

The environment variable FIXPROP_OUT writes the results somewhere else (`python -m fixprop check` uses it to recompute and
compare).
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_root() -> Path:
    env = os.environ.get("FIXPROP_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "data" / "repos.toml").is_file():
            return p
    return here.parents[2]


ROOT = _find_root()

DATA = ROOT / "data"
REPOS_TOML = DATA / "repos.toml"
LOCK = DATA / "repos.lock.json"
REPOS = DATA / "repos"

JUDGMENTS = ROOT / "judgments"  # input: the judgments written after reading the code
OUT = Path(os.environ["FIXPROP_OUT"]).resolve() if os.environ.get("FIXPROP_OUT") else ROOT / "benchmark"  # output


def repo_dir(repo: str) -> Path:
    """data/repos/<owner>/<repo>/: this upstream repository's files at the pinned commit. Its .git holds the history."""
    owner, name = repo.split("/", 1)
    return REPOS / owner / name


def rel(path: Path) -> str:
    """For printing: a path inside the project relative to the project root, any other path as it is."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)
