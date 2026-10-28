"""Character diff for people (plan §3 diff/chardiff.py, §6-6): previous-only / current-only ranges.

Sentences are aligned first (``sentences.align``), then each ``mod`` row is diffed character by character
with ``difflib.SequenceMatcher(autojunk=False)``; ``add`` / ``del`` rows are one current-only / previous-only
segment. Minor changes (numbers / notation / whitespace) are kept and flagged — people see them, the LLM not.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

from .sentences import Row, Sentence, align

OPS = ("equal", "prev_only", "cur_only")
_TAGS = {"delete": (("prev_only", "a"),), "insert": (("cur_only", "b"),),
         "replace": (("prev_only", "a"), ("cur_only", "b"))}


@dataclass(frozen=True)
class Segment:
    op: str        # equal / prev_only / cur_only
    text: str


@dataclass
class Block:
    """One aligned sentence row with its character segments."""
    op: str                      # equal / mod / add / del (sentences.Row.op)
    prev: Sentence | None
    cur: Sentence | None
    minor_kind: str | None       # set for minor mod rows (minor.KINDS); None otherwise
    segments: list[Segment]

    @property
    def is_minor(self) -> bool:
        return self.minor_kind is not None


def _push(out: list[Segment], op: str, text: str) -> None:
    if not text:
        return
    if out and out[-1].op == op:
        out[-1] = Segment(op, out[-1].text + text)
    else:
        out.append(Segment(op, text))


def char_diff(prev: str, cur: str) -> list[Segment]:
    """Segments such that the equal + prev_only texts rebuild ``prev`` and equal + cur_only rebuild ``cur``."""
    out: list[Segment] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, prev, cur, autojunk=False).get_opcodes():
        if tag == "equal":
            _push(out, "equal", prev[i1:i2])
            continue
        for op, side in _TAGS[tag]:
            _push(out, op, prev[i1:i2] if side == "a" else cur[j1:j2])
    return out


def blocks(prev: Sequence[Sentence], cur: Sequence[Sentence], rows: list[Row] | None = None) -> list[Block]:
    """Aligned blocks of a matched item pair (pass ``rows`` to reuse an alignment already computed)."""
    out: list[Block] = []
    for r in rows if rows is not None else align(prev, cur):
        p = prev[r.prev] if r.prev is not None else None
        c = cur[r.cur] if r.cur is not None else None
        if r.op == "equal":
            segs = [Segment("equal", c.text)]
        elif r.op == "add":
            segs = [Segment("cur_only", c.text)]
        elif r.op == "del":
            segs = [Segment("prev_only", p.text)]
        else:
            segs = char_diff(p.text, c.text)
        out.append(Block(r.op, p, c, r.minor_kind, segs))
    return out


def side_text(segs: Sequence[Segment], side: str) -> str:
    """The prev (``side='prev'``) or cur text a segment list was built from."""
    keep = {"prev": "prev_only", "cur": "cur_only"}[side]
    return "".join(s.text for s in segs if s.op in ("equal", keep))


def render_markers(bs: Sequence[Block], *, sep: str = "\n") -> str:
    """Plain-text view: ``[-前期のみ-]`` / ``{+当期のみ+}``, one block per line."""
    def seg(s: Segment) -> str:
        return {"equal": s.text, "prev_only": f"[-{s.text}-]", "cur_only": f"{{+{s.text}+}}"}[s.op]
    return sep.join("".join(seg(s) for s in b.segments) for b in bs)
