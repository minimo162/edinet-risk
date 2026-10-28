"""Split BusinessRisks text into items (plan §3 segment/segmenter.py, §4 M1, decision D2).

Rules (version ``SEGMENTER_VER``):
1. Each line is classified by headings.classify (numbered paragraph = primary cue).
2. A run of two or more same-family numbered lines with no body between them is a list inside an item
   (e.g. SUBARU's 品質方針 「１．…」「２．…」), not headings.
3. An unnumbered short line is a group heading only if the next non-blank line is a numbered heading
   (Mazda 「市場及び事業に関するリスク」 → (1)…). Otherwise it stays body text.
4. Levels: numbering families rank by first appearance; a group heading sits just above the family of
   the heading that follows it. The tree is built with a stack, so restarted numbering under a new group
   is fine.
5. Items are all heading nodes; ``is_leaf`` marks the lowest titled level (D2). Spans
   [start_off, end_off) tile the text from the first heading to the end, so preamble + item spans
   reproduce the input exactly. A non-leaf heading keeps its own intro text in ``body``.
6. No heading at all → paragraph fallback (one item per non-blank line, ``segmentation="fallback"``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .headings import WS, Candidate, classify, normalize_heading

SEGMENTER_VER = "seg-1"


@dataclass
class Item:
    idx: int
    level: int                     # depth in the heading tree, 1 = top
    family: str                    # numbering family of the heading ("plain" for group headings, "para" for fallback)
    number: int | None
    heading_raw: str
    heading_norm: str
    body: str                      # text after the heading line up to the next heading (no heading line)
    start_off: int                 # offset of the heading line in the input text
    end_off: int                   # offset of the next heading (or len(text))
    parents: tuple[str, ...] = ()  # heading_norm of the ancestors, top first
    is_leaf: bool = True
    segmentation: str = "normal"   # normal / fallback


@dataclass
class Segmentation:
    preamble: str
    items: list[Item]
    segmentation: str              # normal / fallback / empty
    families: list[str] = field(default_factory=list)  # numbered families in level order
    segmenter_ver: str = SEGMENTER_VER

    @property
    def leaves(self) -> list[Item]:
        return [it for it in self.items if it.is_leaf]


def _lines(text: str) -> list[tuple[int, str]]:
    out, off = [], 0
    for ln in text.splitlines(keepends=True):
        out.append((off, ln))
        off += len(ln)
    return out


def _detect(lines: list[tuple[int, str]]) -> list[Candidate | None]:
    cands = [classify(ln) if ln.strip() else None for _, ln in lines]
    nonblank = [i for i, (_, ln) in enumerate(lines) if ln.strip(WS)]

    # rule 2: same-family numbered lines back to back (blank lines ignored) are a list
    demote = set()
    for a, b in zip(nonblank, nonblank[1:]):
        ca, cb = cands[a], cands[b]
        if ca and cb and ca.numbered and ca.family == cb.family:
            demote.update((a, b))
    for i in demote:
        cands[i] = None

    # rule 3: plain lines count only when a numbered heading follows
    for j, i in enumerate(nonblank):
        c = cands[i]
        if c and not c.numbered:
            nxt = cands[nonblank[j + 1]] if j + 1 < len(nonblank) else None
            if not (nxt and nxt.numbered):
                cands[i] = None
    return cands


def _fallback(text: str, lines: list[tuple[int, str]]) -> Segmentation:
    items = []
    for off, ln in lines:
        s = ln.strip(WS)
        if s:
            items.append(Item(len(items), 1, "para", None, "", "", ln.rstrip("\r\n"), off, off + len(ln),
                              segmentation="fallback"))
    if not items:
        return Segmentation(text, [], "empty")
    pre = text[: items[0].start_off]
    for a, b in zip(items, items[1:]):  # blank lines belong to the item before them
        a.end_off = b.start_off
    items[-1].end_off = len(text)
    return Segmentation(pre, items, "fallback")


def segment(text: str) -> Segmentation:
    lines = _lines(text)
    cands = _detect(lines)
    heads = [i for i, c in enumerate(cands) if c]
    if not heads:
        return _fallback(text, lines)

    families: list[str] = []
    for i in heads:
        if cands[i].numbered and cands[i].family not in families:
            families.append(cands[i].family)

    def rank(i: int) -> float:
        c = cands[i]
        if c.numbered:
            return families.index(c.family) + 1.0
        nxt = next(j for j in heads if j > i and cands[j].numbered)  # exists by rule 3
        return families.index(cands[nxt].family) + 0.5

    items: list[Item] = []
    stack: list[tuple[float, Item]] = []
    for k, i in enumerate(heads):
        c = cands[i]
        off, ln = lines[i]
        body_start = off + len(ln)
        end = lines[heads[k + 1]][0] if k + 1 < len(heads) else len(text)
        r = rank(i)
        while stack and stack[-1][0] >= r:
            stack.pop()
        if stack:
            stack[-1][1].is_leaf = False
        it = Item(len(items), len(stack) + 1, c.family, c.number, c.raw, normalize_heading(c.title),
                  text[body_start:end].rstrip("\r\n"), off, end,
                  parents=tuple(p.heading_norm for _, p in stack))
        items.append(it)
        stack.append((r, it))
    return Segmentation(text[: items[0].start_off], items, "normal", families)
