"""The core of step 3: take one hunk of a bug-fix commit and look at the same place in a copy in another driver.

A hunk is one block of a git diff (starting with @@, 3 lines of context). Its outcome says what the copy has at the
place of the change:

  candidate  the pre-fix code is still in place. Three kinds:
             replace: every counted removed line is in the copy, at a matching position
             delete:  the same, for a hunk that only removes lines
             add:     the hunk only adds lines; the 3 nearest counted context lines before the change appear together
                      in the copy (at least 2 of them must exist), and nothing has been added after them
  has_fix    the fixed code is in place: every counted added line is in the copy, at a matching position
  diverged   neither: the place looks like neither the code before the fix nor the code after it
  skip       the hunk has no counted lines and is not checked: it only changes comments, whitespace or line breaks, or
             only moves a line

The pre-fix code is looked for first. A copy can hold the added lines somewhere else (a common statement such as an
unlock or an error return, or a second copy of the same function that was fixed) while the place of the change is
still as it was before the fix. That is a candidate.

Comments are removed before comparing. A line that appears both as removed and as added (moved, or only its name prefix
changed) cancels out.

"At a matching position" means: a changed line found in the copy must sit next to at least one of its neighbours in
the hunk. Looking only for the text anywhere in the file is fooled by identical statements elsewhere. A hunk with only
one counted line has no neighbour to compare, and then the text alone decides. Whether the fixed side still has its fix
today (still_fixed) is decided the same way.

"Nothing has been added after them" (add kind) is checked in one of two ways. If the copy has the counted line that
follows the change, that line must come directly after the context lines: the gap is closed, so the added lines are
not there, wherever else they may be. If the copy does not have that line at all (the following line may differ
anyway, for example a call to each vendor's own API), the gap cannot be seen, and none of the added lines may appear
anywhere in the copy.

Stricter rule for the replace kind (strict, used by the C codebases): at least 2 counted removed lines, or one of at
least 24 characters.
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


def _pre_fix_in_place(v: Verdict, target: View, valid: Callable[[str], bool], strict: bool) -> bool | None:
    """Is the code from before the fix still at the place of the change? Returns None when the text is in the copy but
    not at a matching position."""
    if v.dels:  # replace and delete kinds: the removed lines, next to a neighbour
        if not all(d in target.all for d in v.dels):
            return False
        if v.kind == "replace" and strict and len(v.dels) < 2 and not any(len(d) >= 24 for d in v.dels):
            return False
        return None if _anchored(_image(v.items, "-", valid), target) is False else True
    # add kind: the 3 nearest counted context lines before the change, together, with nothing added after them
    items = v.items
    first = next(i for i, (tag, _) in enumerate(items) if tag == "+")
    last = max(i for i, (tag, _) in enumerate(items) if tag == "+")
    before = [s for tag, s in items[:first] if tag in (" ", "=-") and valid(s)][-3:]
    after = [s for tag, s in items[last + 1 :] if tag in (" ", "=-") and valid(s)]
    v.ctx = before
    if len(before) < 2 or not all(c in target.all for c in before):
        return False
    nxt = after[0] if after else None
    if nxt is not None and nxt in target.pos:
        # the copy has the line that follows the change: it must come directly after the context lines
        return True if _block_at(target, before, nxt) else None
    # the gap cannot be seen: the added lines must be nowhere in the copy
    if any(a in target.all for a in v.adds):
        return False
    return True if _block_at(target, before, None) else None


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
    old = _pre_fix_in_place(v, target, valid, strict)  # looked for first: see the top of this file
    if old:
        v.status = "candidate"
        return v
    if va and all(a in target.all for a in va):
        if _anchored(_image(items, "+", valid), target) is not False:
            v.status = "has_fix"
            return v
        v.why = "unanchored"
    elif old is None:
        v.why = "unanchored"
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
