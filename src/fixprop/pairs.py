"""Step 1: find files copied between drivers. Done at the pinned commits.

Two files are copies of each other when they belong to different drivers, share at least 30 counted lines, and the shared
lines are at least 30% of the counted lines of the smaller file (the 20% and 50% levels are recorded too, for comparison).
Files with fewer than 40 counted lines are left out.

Possible pairs are found first, then the shared lines are counted exactly:
  rust     only lines shared by two or more drivers and found in at most 12 files count (lines found everywhere do not
           show who copied whom).
  zephyr   only files in the same subsystem directory are compared; lines found in more files than the larger of 6 and
           a quarter of the files do not count.
  nuttx, rtthread  files with the same normalized name; also files with different names that share many lines
           (NuttX's common/stm32 adds suffixes such as _m3m4_v1 to file names, so names alone do not match).
           Common lines are not excluded in these two codebases.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations

from .codebases import Codebase, SrcFile

MIN_SHARED = 30
TIERS = (0.2, 0.3, 0.5)
RUST_MAX_FILES = 12  # rust: lines found in more files than this do not count
GEN_MAX_FILES = 30  # nuttx, rtthread: lines found in more files than this are not used to find possible pairs by content
GEN_MIN_SHARED = 15

_DERIVED = re.compile(
    r"(?i)\b(modified from|adapted from|derived from|derives from|ported from|copied from|borrowed from|"
    r"taken from|forked from|based (?:heavily |largely |loosely |partly )?on|port of)\b.{0,100}"
    r"|\u6539\u81ea.{0,60}|\u79fb\u690d\u81ea.{0,60}")  # the last two are Chinese for "adapted from" and "ported from"


@dataclass
class FileLines:
    f: SrcFile
    lines: list[str]  # every line, normalized (index + 1 is the line number)
    all: frozenset  # all normalized lines, to test whether the other side has a line
    valid: frozenset  # the counted lines used to find copy pairs
    name_key: str  # the normalized file name
    derived: str  # the "adapted from ..." sentence in the file header, or an empty string


def derived_note(text: str, lang: str) -> str:
    """The sentence in the header comment (before the first include / use, at most 60 lines) that states where the file
    came from."""
    for ln in text.split("\n")[:60]:
        s = ln.strip()
        if s.startswith(("#include", "use ", "pub use ", "mod ", "pub mod ")):
            break
        if s.startswith(("//", "/*", "*")):
            m = _DERIVED.search(s)
            if m:
                return m.group(0).strip(" */")[:140]
    return ""


def load_files(cb: Codebase) -> dict[tuple[str, str], FileLines]:
    out = {}
    for f in cb.files():
        text = cb.read(f)
        norm = cb.normalizer(f.repo, f.path)
        lines = [norm(ln) for ln in text.split("\n")]
        out[(f.repo, f.path)] = FileLines(
            f=f, lines=lines, all=frozenset(lines), valid=frozenset(ln for ln in lines if cb.valid_pair(ln)),
            name_key=norm(f.path.rsplit("/", 1)[-1]), derived=derived_note(text, cb.lang))
    return out


def find_pairs(cb: Codebase, files: dict[tuple[str, str], FileLines]) -> tuple[list[dict], dict]:
    """Returns (copy pairs, counts). Keeps the pairs with at least 30 shared lines and a ratio of at least 20%."""
    groups: dict[str, list[FileLines]] = defaultdict(list)
    for fl in files.values():
        groups[fl.f.group].append(fl)
    pairs: list[dict] = []
    stats = {"files": len(files), "participating": 0}
    for group, members in sorted(groups.items()):
        part = [fl for fl in members if len(fl.valid) >= cb.min_valid]
        stats["participating"] += len(part)
        occ: dict[str, list[int]] = defaultdict(list)
        for i, fl in enumerate(part):
            for ln in fl.valid:
                occ[ln].append(i)
        if cb.name == "rust":
            counted = {ln for ln, ids in occ.items()
                       if len(ids) <= RUST_MAX_FILES and len({part[i].f.unit for i in ids}) >= 2}
            shared = _count_shared(part, occ, counted)
            cand = {k: v for k, v in shared.items() if v >= MIN_SHARED}
        elif cb.name == "zephyr":
            cap = max(6, len(members) / 4)
            counted = {ln for ln, ids in occ.items() if 2 <= len(ids) <= cap}
            shared = _count_shared(part, occ, counted)
            cand = {k: v for k, v in shared.items() if v >= MIN_SHARED}
        else:
            keys: set[tuple[int, int]] = set()
            by_name: dict[str, list[int]] = defaultdict(list)
            for i, fl in enumerate(part):
                by_name[fl.name_key].append(i)
            for ids in by_name.values():
                keys |= {(a, b) for a, b in combinations(ids, 2) if part[a].f.unit != part[b].f.unit}
            gen = {ln for ln, ids in occ.items() if 2 <= len(ids) <= GEN_MAX_FILES}
            keys |= {k for k, v in _count_shared(part, occ, gen).items() if v >= GEN_MIN_SHARED}
            cand = {}
            for a, b in keys:
                n = len(part[a].valid & part[b].valid)
                if n >= MIN_SHARED:
                    cand[(a, b)] = n
        for (a, b), n in cand.items():
            fa, fb = part[a], part[b]
            ratio = n / min(len(fa.valid), len(fb.valid))
            if ratio < TIERS[0]:
                continue
            if (fa.f.repo, fa.f.path) > (fb.f.repo, fb.f.path):
                fa, fb = fb, fa
            pairs.append({
                "codebase": cb.name, "group": group,
                "repo_a": fa.f.repo, "commit_a": cb.repos[fa.f.repo], "unit_a": fa.f.unit, "file_a": fa.f.path,
                "valid_a": len(fa.valid),
                "repo_b": fb.f.repo, "commit_b": cb.repos[fb.f.repo], "unit_b": fb.f.unit, "file_b": fb.f.path,
                "valid_b": len(fb.valid),
                "shared": n, "ratio": round(ratio, 4), "same_name": fa.name_key == fb.name_key,
                "derived_a": fa.derived, "derived_b": fb.derived,
            })
    pairs.sort(key=lambda p: (p["repo_a"], p["file_a"], p["repo_b"], p["file_b"]))
    return pairs, stats


def _count_shared(part: list[FileLines], occ: dict[str, list[int]], counted: set[str]) -> Counter:
    """For every pair of files in different drivers, how many of the counted lines they share."""
    units = [fl.f.unit for fl in part]
    shared: Counter = Counter()
    for ln in counted:
        ids = occ[ln]
        for x in range(len(ids)):
            a = ids[x]
            for y in range(x + 1, len(ids)):
                b = ids[y]
                if units[a] != units[b]:
                    shared[(a, b)] += 1
    return shared


def at_tier(pairs: list[dict], tier: float) -> list[dict]:
    return [p for p in pairs if p["shared"] >= MIN_SHARED and p["ratio"] >= tier]


def partner_index(pairs: list[dict], tier: float = 0.3) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """File -> the files on the other side of its copy pairs."""
    idx: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for p in at_tier(pairs, tier):
        a, b = (p["repo_a"], p["file_a"]), (p["repo_b"], p["file_b"])
        idx[a].append(b)
        idx[b].append(a)
    return idx
