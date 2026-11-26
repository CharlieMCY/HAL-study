"""The four codebases: their scope, what one driver is, and how names are normalized. The repositories and their pinned
commits are in data/repos.toml.

| Codebase | One driver (unit) | Source files in scope |
|---|---|---|
| rust | a crate | the .rs files under each crate's src/ (not in examples, tests or docs directories) |
| nuttx | arch/<arch>/src/<chip directory>/, plus the chip-family directories under common/ | the .c files of the unit |
| zephyr | one .c file under drivers/<subsystem>/ | the .c files under drivers/ |
| rtthread | the directory under bsp/ that holds drv_*.c files | the drv_*.c files under bsp/ |
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 needs tomli
    import tomli as tomllib

from . import normalize as N
from . import paths

RUST_EXCLUDE_DIRS = frozenset({"examples", "tests", "docs"})

# NuttX: the directories under arch/<arch>/src/ that are not chip directories (common code, build scripts, CPU
# architecture level). Decided by looking at the directories at the pinned commit. Directories that existed in the past
# and are gone now (such as stm32, stm32f0l0g0) count as chip directories.
NUTTX_NON_CHIP = frozenset({
    "common", "cmake", "arm", "arm_a_r", "arm_m", "armv6-m", "armv7-a", "armv7-m", "armv7-r", "armv8-m", "armv8-r",
    "avr", "avr32", "xmega", "xc5", "xm6", "mips32", "sbi", "sparc_v8", "illd", "i486", "lx6", "lx7",
})
# the chip-family directories under common/ are units too
NUTTX_COMMON_FAMILIES = frozenset({"stm32", "ameba", "espressif", "renesas"})


@dataclass(frozen=True)
class SrcFile:
    repo: str
    path: str  # path inside the repository
    unit: str  # the unit (driver) it belongs to
    group: str = ""  # files are compared only within a group (Zephyr's subsystem directories); empty = one group


def _toml() -> dict:
    return tomllib.loads(paths.REPOS_TOML.read_text(encoding="utf-8"))


def rtos_repos() -> dict[str, str]:
    """The NuttX, Zephyr and RT-Thread repositories: name -> pinned commit."""
    return dict(_toml().get("rtos", {}))


def rust_repos() -> dict[str, str]:
    """The 37 Rust HAL repositories: name -> pinned commit."""
    return dict(_toml().get("rust", {}))


def all_repos() -> dict[str, str]:
    return {**rust_repos(), **rtos_repos()}


class Codebase:
    name = ""
    lang = "c"
    strict_replace = True  # the stricter rule for replace hunks (see hunks.py); used by the C codebases
    valid_change: Callable[[str], bool] = staticmethod(N.valid_c_change)
    valid_pair: Callable[[str], bool] = staticmethod(N.valid_c_pair)
    min_valid = 40  # files with fewer counted lines are left out when finding copy pairs

    def __init__(self) -> None:
        self.repos: dict[str, str] = {}

    def root(self, repo: str) -> Path:
        return paths.repo_dir(repo)

    def files(self) -> list[SrcFile]:
        raise NotImplementedError

    def in_scope(self, repo: str, path: str) -> bool:
        raise NotImplementedError

    def in_tree_scope(self, repo: str, path: str, tree: dict) -> bool:
        """For checking the data: is path a source file in scope, given the tree at the pinned commit (path -> ...)?
        Must make the same choice as files()."""
        return self.in_scope(repo, path)

    def unit_of(self, repo: str, path: str) -> str:
        raise NotImplementedError

    def group_of(self, repo: str, path: str) -> str:
        return ""

    def normalizer(self, repo: str, path: str) -> N.Norm:
        return N.norm_ws

    def read(self, f: SrcFile) -> str:
        return (self.root(f.repo) / f.path).read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------ Rust


def rust_crate_dir(path: str) -> str | None:
    """The part of the path before the first src is the crate directory. Paths without a src part are out of scope."""
    parts = path.split("/")
    if "src" not in parts[:-1]:
        return None
    return "/".join(parts[: parts.index("src")])


class Rust(Codebase):
    name = "rust"
    lang = "rust"
    strict_replace = False
    valid_change = staticmethod(N.valid_rust_change)
    valid_pair = staticmethod(N.valid_rust_pair)

    def __init__(self) -> None:
        super().__init__()
        self.repos = rust_repos()

    def in_scope(self, repo: str, path: str) -> bool:
        if not path.endswith(".rs") or rust_crate_dir(path) is None:
            return False
        return not (set(path.split("/")[:-1]) & RUST_EXCLUDE_DIRS)

    def unit_of(self, repo: str, path: str) -> str:
        return f"{repo}:{rust_crate_dir(path) or '.'}"

    def in_tree_scope(self, repo: str, path: str, tree: dict) -> bool:
        if not self.in_scope(repo, path):
            return False
        crate = rust_crate_dir(path) or ""
        return (f"{crate}/Cargo.toml" if crate else "Cargo.toml") in tree  # the src directory must belong to a crate

    def files(self) -> list[SrcFile]:
        out = []
        for repo in sorted(self.repos):
            root = self.root(repo)
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d != ".git" and not (Path(dirpath) / d).is_symlink())
                for name in sorted(filenames):
                    p = Path(dirpath) / name
                    if p.is_symlink():
                        continue
                    rel = p.relative_to(root).as_posix()
                    if not self.in_scope(repo, rel):
                        continue
                    crate = rust_crate_dir(rel) or ""
                    if not (root / crate / "Cargo.toml").is_file():  # the src directory does not belong to a crate
                        continue
                    out.append(SrcFile(repo, rel, self.unit_of(repo, rel)))
        return out


# ------------------------------------------------------------------ NuttX

_NUTTX_PATH = re.compile(r"^arch/([^/]+)/src/([^/]+)/(.+)$")


class NuttX(Codebase):
    name = "nuttx"
    REPO = "apache/nuttx"

    def __init__(self) -> None:
        super().__init__()
        self.repos = {self.REPO: rtos_repos()[self.REPO]}

    def unit_of(self, repo: str, path: str) -> str:
        m = _NUTTX_PATH.match(path)
        if not m:
            return ""
        arch, d, rest = m.groups()
        if d == "common":
            fam = rest.split("/")[0]
            return f"arch/{arch}/src/common/{fam}" if "/" in rest and fam in NUTTX_COMMON_FAMILIES else ""
        return "" if d in NUTTX_NON_CHIP else f"arch/{arch}/src/{d}"

    def in_scope(self, repo: str, path: str) -> bool:
        return path.endswith(".c") and bool(self.unit_of(repo, path))

    def files(self) -> list[SrcFile]:
        root = self.root(self.REPO)
        out = []
        for p in sorted((root / "arch").rglob("*.c")):
            rel = p.relative_to(root).as_posix()
            if self.in_scope(self.REPO, rel) and not p.is_symlink():
                out.append(SrcFile(self.REPO, rel, self.unit_of(self.REPO, rel)))
        return out

    @lru_cache(maxsize=None)
    def _unit_tokens(self, unit: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The unit's own names: the directory name, plus the prefix most of its file names share.
        Returns (families with digits, whole prefixes made only of letters)."""
        d = self.root(self.REPO) / unit
        names = [p.name for p in d.rglob("*.c")] if d.is_dir() else []
        first = Counter(n.split("_")[0].lower() for n in names if "_" in n)
        toks = {unit.rsplit("/", 1)[-1].lower()}
        toks |= {t for t, c in first.items() if c >= max(2, 0.2 * len(names))}
        return _split_stems(toks, set(first))

    def normalizer(self, repo: str, path: str) -> N.Norm:
        return self._norm_for_unit(self.unit_of(repo, path))

    @lru_cache(maxsize=None)
    def _norm_for_unit(self, unit: str) -> N.Norm:
        stems, exact = self._unit_tokens(unit)
        return N.prefix_normalizer(stems, exact)


def _split_stems(tokens: set[str], observed: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Two kinds of names: families with digits, which may be followed by any letters and digits; names made only of
    letters, which match only as the whole prefix seen."""
    stems, exact = set(), set()
    for t in tokens:
        s = N.stem_of(t)
        if not s:
            continue
        if any(ch.isdigit() for ch in s):
            stems.add(s)
        else:
            exact.add(t)
            exact |= {o for o in observed if o.startswith(s) and N.stem_of(o) == s}
    return tuple(sorted(stems)), tuple(sorted(exact))


# ------------------------------------------------------------------ Zephyr


class Zephyr(Codebase):
    name = "zephyr"
    REPO = "zephyrproject-rtos/zephyr"

    def __init__(self) -> None:
        super().__init__()
        self.repos = {self.REPO: rtos_repos()[self.REPO]}

    def in_scope(self, repo: str, path: str) -> bool:
        parts = path.split("/")
        return path.endswith(".c") and len(parts) >= 3 and parts[0] == "drivers"

    def unit_of(self, repo: str, path: str) -> str:
        return path  # one driver source file is one unit

    def group_of(self, repo: str, path: str) -> str:
        return path.split("/")[1]

    def files(self) -> list[SrcFile]:
        root = self.root(self.REPO)
        out = []
        for p in sorted((root / "drivers").rglob("*.c")):
            rel = p.relative_to(root).as_posix()
            if self.in_scope(self.REPO, rel) and not p.is_symlink():
                out.append(SrcFile(self.REPO, rel, rel, self.group_of(self.REPO, rel)))
        return out

    def normalizer(self, repo: str, path: str) -> N.Norm:
        return _zephyr_norm(path)


@lru_cache(maxsize=None)
def _zephyr_norm(path: str) -> N.Norm:
    parts = path.split("/")
    return N.token_normalizer(N.name_tokens(parts[-1], parts[1]))


# ------------------------------------------------------------------ RT-Thread

_DRV = re.compile(r"^drv_.*\.c$")


class RTThread(Codebase):
    name = "rtthread"
    REPO = "RT-Thread/rt-thread"

    def __init__(self) -> None:
        super().__init__()
        self.repos = {self.REPO: rtos_repos()[self.REPO]}

    def in_scope(self, repo: str, path: str) -> bool:
        parts = path.split("/")
        return len(parts) >= 3 and parts[0] == "bsp" and _DRV.match(parts[-1]) is not None

    def unit_of(self, repo: str, path: str) -> str:
        return path.rsplit("/", 1)[0]

    def files(self) -> list[SrcFile]:
        root = self.root(self.REPO)
        out = []
        for p in sorted((root / "bsp").rglob("drv_*.c")):
            rel = p.relative_to(root).as_posix()
            if self.in_scope(self.REPO, rel) and not p.is_symlink():
                out.append(SrcFile(self.REPO, rel, self.unit_of(self.REPO, rel)))
        return out

    def normalizer(self, repo: str, path: str) -> N.Norm:
        return _rtt_norm(path.split("/")[1])


@lru_cache(maxsize=None)
def _rtt_norm(bsp: str) -> N.Norm:
    """The first directory name under bsp is the unit's name (stm32, gd32, at32, ...)."""
    stems, exact = _split_stems({bsp.lower()}, set())
    return N.prefix_normalizer(stems, exact)


CODEBASES: dict[str, Callable[[], Codebase]] = {
    "rust": Rust, "nuttx": NuttX, "zephyr": Zephyr, "rtthread": RTThread,
}
ORDER = ("rust", "nuttx", "zephyr", "rtthread")


def get(name: str) -> Codebase:
    return CODEBASES[name]()
