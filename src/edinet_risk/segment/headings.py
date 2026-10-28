"""Heading candidates in BusinessRisks text (plan §3 segment/headings.py).

M0 (docs/spike-findings.md §3): every transport filer marks risk items with numbered paragraphs such as
``(1)``/``（１）``/``①``/``１．``. Those are the primary cue. Unnumbered short lines are only *candidates*
for group headings; segmenter.py decides from context (a group heading is followed by a numbered one).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_HEADING_CHARS = 80   # longer lines are body text even if they start with a number
MAX_PLAIN_CHARS = 40     # unnumbered group-heading candidates are short

WS = " \u3000\t\r\n"
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟"
_KANA = "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン"

# (family, pattern). Family = numbering style; the hierarchy level comes from the order in which families
# first appear in a document (segmenter.py), not from a fixed table.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("paren_num", re.compile(r"[（(][ 　]*([0-9０-９]{1,2})[ 　]*[）)][ 　]*(.+)")),
    ("circled", re.compile(rf"([{_CIRCLED}])[ 　]*(.+)")),
    ("num_dot", re.compile(r"([0-9０-９]{1,2})[ 　]*[．.][ 　]*([^0-9０-９．.].*)")),
    ("paren_kana", re.compile(rf"[（(]([{_KANA}])[）)][ 　]*(.+)")),
    ("kana_dot", re.compile(rf"([{_KANA}])[ 　]*[．.][ 　]*(.+)")),
]
# Sub-labels inside an item (Honda/SUBARU ＜リスク＞/＜対応策＞); never headings.
_SUBLABEL = re.compile(r"[＜<〈【\[［](リスク|対応策|対応|機会|取り?組み?|影響)[＞>〉】\]］]")
_BULLET = tuple("・※*＊-－‐•●○◆◇■□")
_SENTENCE_END = ("。", "．")


@dataclass(frozen=True)
class Candidate:
    family: str           # paren_num / circled / num_dot / paren_kana / kana_dot / plain
    number: int | None    # parsed ordinal (None for plain)
    title: str            # heading text without the number marker, as written (outer spaces stripped)
    raw: str              # whole line, outer spaces stripped

    @property
    def numbered(self) -> bool:
        return self.family != "plain"


def _ordinal(family: str, mark: str) -> int:
    if family == "circled":
        return _CIRCLED.index(mark) + 1
    if family in ("paren_kana", "kana_dot"):
        return _KANA.index(mark) + 1
    return int(unicodedata.normalize("NFKC", mark))


def classify(line: str) -> Candidate | None:
    """Heading candidate for one line, or None for body text."""
    s = line.strip(WS)
    if not s or "\t" in s or len(s) > MAX_HEADING_CHARS or s.endswith(_SENTENCE_END):
        return None
    for family, pat in _PATTERNS:
        m = pat.fullmatch(s)
        if m:
            title = m.group(2).strip(WS)
            return Candidate(family, _ordinal(family, m.group(1)), title, s) if title else None
    if len(s) <= MAX_PLAIN_CHARS and not s.startswith(_BULLET) and not _SUBLABEL.fullmatch(s) and "。" not in s:
        return Candidate("plain", None, s, s)
    return None


def normalize_heading(title: str) -> str:
    """Comparison key for a heading title: NFKC, no whitespace, outer brackets ＜…＞ dropped."""
    t = re.sub(r"\s+", "", unicodedata.normalize("NFKC", title))
    m = re.fullmatch(r"[<〈【\[](.+)[>〉】\]]", t)
    return m.group(1) if m else t
