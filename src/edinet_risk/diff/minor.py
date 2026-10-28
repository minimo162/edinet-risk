"""Minor-change test (plan §3 diff/minor.py, §6-6, decision D4).

A change is *minor* when the two texts become equal after one of these normalisations, tried in order:

1. ``whitespace`` — all whitespace removed (half/full-width spaces, tabs, line breaks).
2. ``notation``   — plus NFKC, lower case and the small variant table ``_VARIANTS`` (dashes, tildes).
3. ``number``     — plus every number and era year replaced by ``<NUM>`` (``2025年3月期`` ⇔ ``2026年3月期``,
   ``令和6年`` ⇔ ``令和7年``, ``1,234`` ⇔ ``2,345``).

Minor changes stay visible in the human diff (chardiff) but are not sent to the LLM (§6-6).
Arabic digits only: kanji numerals (一部, 二輪) are words far more often than figures.
"""
from __future__ import annotations

import re
import unicodedata

MINOR_VER = "minor-1"
KINDS = ("identical", "whitespace", "notation", "number")

_WS_RE = re.compile(r"\s+")
_VARIANTS = str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-", "―": "-", "−": "-", "〜": "~", "～": "~"})
# After NFKC, so digits and separators are ASCII. Era years first so 令和元年 is one <NUM>.
_NUM_RE = re.compile(r"(?:明治|大正|昭和|平成|令和)\s*(?:\d+|元)|\d+(?:[,.]\d+)*")
NUM = "<NUM>"


def strip_ws(s: str) -> str:
    return _WS_RE.sub("", s)


def norm_notation(s: str) -> str:
    return strip_ws(unicodedata.normalize("NFKC", s)).lower().translate(_VARIANTS)


def mask_numbers(s: str) -> str:
    """Notation-normalised text with numbers / era years replaced by ``<NUM>``."""
    return _NUM_RE.sub(NUM, norm_notation(s))


def classify(prev: str, cur: str) -> str | None:
    """The first ``KINDS`` level at which ``prev`` and ``cur`` agree, or None for a substantive change."""
    if prev == cur:
        return "identical"
    if strip_ws(prev) == strip_ws(cur):
        return "whitespace"
    if norm_notation(prev) == norm_notation(cur):
        return "notation"
    if mask_numbers(prev) == mask_numbers(cur):
        return "number"
    return None


def is_minor(prev: str, cur: str) -> bool:
    return classify(prev, cur) is not None
