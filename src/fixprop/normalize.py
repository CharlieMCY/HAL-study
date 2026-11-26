"""Normalizing code lines, and which lines count.

Normalizing: runs of whitespace become one space and leading and trailing whitespace is removed; then the driver's own
names (chip names, vendor prefixes) are replaced by one placeholder, so stm32l4_i2c_init and at32_i2c_init match.

Counted lines: very short lines, comments, includes and similar lines that look the same everywhere do not count.
  comparing a hunk    Rust: at least 16 characters; not starting with //, #[ or use
                      C:    at least 18 characters; not starting with /*, *, //, #include, #endif or #else;
                            at least 4 consecutive letters or underscores
  finding copy pairs  at least 28 characters; in Rust also not starting with pub use, in C also not starting with
                      #define DT_DRV_COMPAT
"""

from __future__ import annotations

import re
from typing import Callable, Iterable

_WS = re.compile(r"\s+")
_ALPHA4 = re.compile(r"[A-Za-z_]{4,}")

RUST_SKIP = ("//", "#[", "use ")
RUST_PAIR_SKIP = ("//", "#[", "use ", "pub use ")
C_SKIP = ("/*", "*", "//", "#include", "#endif", "#else")
C_PAIR_SKIP = C_SKIP + ("#define DT_DRV_COMPAT",)

Norm = Callable[[str], str]


def norm_ws(line: str) -> str:
    return _WS.sub(" ", line).strip()


def valid_rust_change(s: str) -> bool:
    return len(s) >= 16 and not s.startswith(RUST_SKIP)


def valid_c_change(s: str) -> bool:
    return len(s) >= 18 and not s.startswith(C_SKIP) and _ALPHA4.search(s) is not None


def valid_rust_pair(s: str) -> bool:
    return len(s) >= 28 and not s.startswith(RUST_PAIR_SKIP)


def valid_c_pair(s: str) -> bool:
    return len(s) >= 28 and not s.startswith(C_PAIR_SKIP)


# ------------------------------------------------------------------ the driver's own names

_STEM = re.compile(r"[a-z]+\d*")


def _longest_first(s: str) -> tuple[int, str]:
    """Longest first; equal lengths in alphabetical order, so the joined regex is always the same."""
    return (-len(s), s)


def stem_of(token: str) -> str:
    """The family of a name: the leading letters plus the first group of digits. stm32l4 -> stm32, gd32f4xx -> gd32,
    sam -> sam."""
    m = _STEM.match(token.lower())
    return m.group(0) if m else ""


def prefix_normalizer(stems: Iterable[str], exact: Iterable[str] = ()) -> Norm:
    """Replace a prefix "family + any letters and digits + underscore" with P_, ignoring case.
    stems: families with digits, which may be followed by any letters and digits (stm32 covers stm32l4_ and STM32F7_).
    exact: short names made only of letters match only as a whole prefix (sam_), so ordinary words like sample_ stay."""
    alts = [re.escape(s) + "[a-z0-9]*" for s in sorted(set(stems), key=_longest_first) if s]
    alts += [re.escape(s) for s in sorted(set(exact), key=_longest_first) if s]
    if not alts:
        return norm_ws
    rx = re.compile(r"(?i)\b(?:" + "|".join(alts) + r")_")
    return lambda line: rx.sub("P_", norm_ws(line))


ZEPHYR_GENERIC = frozenset({"ll", "drv", "driver", "common", "shell", "handlers", "api"})


def name_tokens(filename: str, subsystem: str) -> list[str]:
    """Zephyr: split the file name at _ and -, drop the subsystem name and a few generic words, and keep the parts of at
    least 3 characters. These parts are the driver's own names."""
    stem = filename.rsplit(".", 1)[0]
    drop = set(re.split(r"[_-]", subsystem.lower())) | ZEPHYR_GENERIC
    return [t for t in re.split(r"[_-]", stem.lower()) if len(t) >= 3 and t not in drop]


def token_normalizer(tokens: Iterable[str]) -> Norm:
    """These parts are replaced in the code by one placeholder, ignoring case."""
    toks = sorted({t for t in tokens if t}, key=_longest_first)
    if not toks:
        return norm_ws
    rx = re.compile("(?i)" + "|".join(re.escape(t) for t in toks))
    return lambda line: rx.sub("P", norm_ws(line))


# ------------------------------------------------------------------ code only, no comments

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/")  # a complete /* ... */ within the line
_OPEN_COMMENT = re.compile(r"/\*.*$")  # a /* ... opened at the end of the line and not closed
_LINE_COMMENT = re.compile(r"(?:^|(?<=\s))//.*$")  # // ... at the start of the line or after whitespace (leaves http:// alone)


def strip_comments(line: str) -> str:
    """Remove the comments from a line and keep the code. A line that is all comment becomes an empty string."""
    s = _BLOCK_COMMENT.sub(" ", line)
    s = _OPEN_COMMENT.sub("", s)
    return _LINE_COMMENT.sub("", s)


def code_only(norm: Norm) -> Norm:
    """Normalize and also remove comments. Changing a comment, or its wording, is not a change to the code."""
    return lambda line: norm_ws(strip_comments(norm(line)))
