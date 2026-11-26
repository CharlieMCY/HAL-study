"""The core of step 3: take one hunk of a bug-fix commit and compare it with the copy in another driver.

A hunk is one block of a git diff (starting with @@, 3 lines of context). Four outcomes:

  has_fix    the copy already has the fixed code: every counted added line is in the copy
  candidate  the copy still has the pre-fix code. Two kinds:
             replace: every counted removed line is in the copy, at a matching position; not every counted added line is
             add:     there are no counted removed lines; the 3 nearest counted context lines before the change appear
                      together in the copy (at least 2 of them must exist), and none of the counted added lines does
  diverged   neither of the above
  skip       the hunk has no counted lines and is not checked: it only changes comments, whitespace or line breaks, or
             only moves a line

Comments are removed before comparing. A line that appears both as removed and as added (moved, or only its name prefix
changed) cancels out.
"At a matching position" means: a removed line found in the copy must sit next to at least one of its original
neighbours. Looking only for the text anywhere in the file is fooled by identical sentences elsewhere. Whether the fixed
side still has its fix today (still_fixed) is decided the same way.

Stricter rule for the replace kind (strict, used by the C codebases): at least 2 counted removed lines, or one of at
least 24 characters.
A hunk that only removes lines (no counted added lines) is candidate / kind=delete. Rust counts it as a candidate, the C
codebases do not (see codebases.py).
"Fixed in the same commit" is not decided here; it follows from the files the commit changed (see census.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    new_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (mark, text); mark is ' ', '-' or '+'

    @property
    def dels(self) -> list[str]:
        return [t for tag, t in self.lines if tag == "-"]

    @property
    def adds(self) -> list[str]:
        return [t for tag, t in self.lines if tag == "+"]

    @property
    def ctx_before(self) -> list[str]:
        """The context lines before the first added or removed line."""
        out = []
        for tag, t in self.lines:
            if tag != " ":
                break
            out.append(t)
        return out

    def text(self) -> str:
        return "\n".join(tag + t for tag, t in self.lines)


def parse_diff(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    cur: Hunk | None = None
    for ln in diff.split("\n"):
        m = _HUNK.match(ln)
        if m:
            cur = Hunk(int(m.group(1)), int(m.group(3)))
            hunks.append(cur)
        elif cur is not None and ln[:1] in (" ", "-", "+"):
            cur.lines.append((ln[0], ln[1:]))
    return hunks


def line_numbers(lines: list[str], wanted: list[str], cap: int = 8) -> list[int]:
    """Every line number at which a line in wanted appears in the file (from 1, ascending, at most cap per line)."""
    want = set(wanted)
    seen: dict[str, int] = {}
    out = []
    for i, ln in enumerate(lines, 1):
        if ln in want and seen.get(ln, 0) < cap:
            seen[ln] = seen.get(ln, 0) + 1
            out.append(i)
    return out


@dataclass
class View:
    """A file seen as code only."""

    all: frozenset  # every line, with comments removed and normalized
    seq: list[str]  # the counted lines among them, in order
    pos: dict[str, list[int]]  # counted line -> its positions in seq


def make_view(code_lines: list[str], valid: Callable[[str], bool]) -> View:
    seq = [s for s in code_lines if valid(s)]
    pos: dict[str, list[int]] = {}
    for i, s in enumerate(seq):
        pos.setdefault(s, []).append(i)
    return View(frozenset(code_lines), seq, pos)


def clean(h: Hunk, norm: Callable[[str], str]) -> list[tuple[str, str]]:
    """Normalize a hunk and drop what does not count as a change. Returns (mark, normalized line):
    ' ' context; '-' really removed; '+' really added; '=-' / '=+' removed / added lines that cancel each other out."""
    items = [(tag, norm(t)) for tag, t in h.lines]
    dels = [s for tag, s in items if tag == "-"]
    adds = [s for tag, s in items if tag == "+"]
    if "".join(dels).replace(" ", "") == "".join(adds).replace(" ", ""):
        # the same once all whitespace is removed: only reformatted
        return [(tag if tag == " " else "=" + tag, s) for tag, s in items]
    from collections import Counter

    common = Counter(dels) & Counter(adds)
    left = {"-": Counter(common), "+": Counter(common)}
    out = []
    for tag, s in items:
        if tag != " " and left[tag][s] > 0:
            left[tag][s] -= 1
            out.append(("=" + tag, s))
        else:
            out.append((tag, s))
    return out


def _image(items: list[tuple[str, str]], side: str, valid) -> list[tuple[str, bool]]:
    """The counted lines of this hunk before (side='-') or after (side='+') the change, in order: (line, really changed)."""
    keep = (" ", "=" + side, side)
    return [(s, tag == side) for tag, s in items if tag in keep and valid(s)]


def _anchored(image: list[tuple[str, bool]], view: View) -> bool | None:
    """Does any really changed line of this hunk appear in the file next to one of its neighbours in the hunk?
    Returns None when the hunk has only one counted line (no neighbour to compare)."""
    if len(image) < 2:
        return None
    for i, (s, changed) in enumerate(image):
        if not changed:
            continue
        for p in view.pos.get(s, ()):
            if i > 0 and p > 0 and view.seq[p - 1] == image[i - 1][0]:
                return True
            if i + 1 < len(image) and p + 1 < len(view.seq) and view.seq[p + 1] == image[i + 1][0]:
                return True
    return False


def _block_at(view: View, block: list[str], nxt: str | None) -> bool:
    """Do the lines of block appear together in the file?
    With nxt (the line right after the change): if nxt exists in this file it must follow block directly, otherwise the
    file already has something else at that position; if nxt does not exist in this file at all, nothing is required
    (the following line may differ anyway, for example a call to each vendor's own API)."""
    n = len(block)
    found = False
    for p in view.pos.get(block[-1], ()):
        if p - n + 1 < 0 or view.seq[p - n + 1 : p + 1] != block:
            continue
        found = True
        if nxt is None or (p + 1 < len(view.seq) and view.seq[p + 1] == nxt):
            return True
    return found and nxt is not None and nxt not in view.pos


@dataclass
class Verdict:
    status: str  # has_fix / candidate / diverged / skip
    kind: str  # replace / add / delete / none
    dels: list[str] = field(default_factory=list)  # counted removed lines, normalized
    adds: list[str] = field(default_factory=list)  # counted added lines, normalized
    ctx: list[str] = field(default_factory=list)  # counted context lines used by the add kind
    why: str = ""  # for diverged: the text does not match, or the text is there but not at a matching position (unanchored)
    items: list = field(default_factory=list)


def classify(h: Hunk, target: View, norm: Callable[[str], str], valid: Callable[[str], bool],
             strict: bool = False) -> Verdict:
    """The outcome of one hunk on the other side. norm must be wrapped with normalize.code_only (comments removed)."""
    items = clean(h, norm)
    vd = [s for tag, s in items if tag == "-" and valid(s)]
    va = [s for tag, s in items if tag == "+" and valid(s)]
    if not vd and not va:
        return Verdict("skip", "none", items=items)
    kind = "replace" if vd and va else "delete" if vd else "add"
    v = Verdict("diverged", kind, vd, va, items=items)
    if va and all(a in target.all for a in va):
        v.status = "has_fix"
        return v
    if vd:
        if not all(d in target.all for d in vd):
            return v
        if not (kind == "delete" or not strict or len(vd) >= 2 or any(len(d) >= 24 for d in vd)):
            return v
        if _anchored(_image(items, "-", valid), target) is False:
            v.why = "unanchored"
            return v
        v.status = "candidate"
        return v
    # add kind: the 3 nearest counted context lines before the change are all in the copy (at least 2), together;
    # the counted context line right after the change, if the copy has it, must follow directly (so the copy added
    # nothing at that position)
    first = next(i for i, (tag, _) in enumerate(items) if tag == "+")
    last = max(i for i, (tag, _) in enumerate(items) if tag == "+")
    before = [s for tag, s in items[:first] if tag in (" ", "=-") and valid(s)][-3:]
    after = [s for tag, s in items[last + 1 :] if tag in (" ", "=-") and valid(s)]
    v.ctx = before
    if len(before) < 2 or not all(c in target.all for c in before) or any(a in target.all for a in va):
        return v
    if not _block_at(target, before, after[0] if after else None):
        v.why = "unanchored"
        return v
    v.status = "candidate"
    return v


def still_fixed(v: Verdict, source_now: View | None, valid: Callable[[str], bool]) -> bool:
    """Does the source still have this fix now (judged by position)?"""
    if source_now is None:
        return False
    if v.kind == "delete":
        back = _anchored(_image(v.items, "-", valid), source_now)
        return any(d not in source_now.all for d in v.dels) if back is None else not back
    if not v.adds or not all(a in source_now.all for a in v.adds):
        return False
    return _anchored(_image(v.items, "+", valid), source_now) is not False
