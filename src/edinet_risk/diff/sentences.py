"""Sentence split, sentence alignment and the LLM change list (plan §3 diff/sentences.py, §5, §6-6, §6-7).

* Sentences end at 「。」 (with any closing brackets right after it) or at a line break. Each sentence keeps
  its offsets in the *document* text, so ``text[start:end] == sentence.text`` (round trip, §7 tests).
* Evidence ID (§5): ``{side}:{doc_id}:{start}-{end}:{sha1(text)[:8]}`` — independent of the item split.
* Alignment: ``difflib.SequenceMatcher(autojunk=False)`` over number-masked keys (``minor.mask_numbers``), so a
  sentence that differs only in numbers / notation / whitespace lines up as ``mod`` with ``is_minor``. Inside a
  replaced block, sentences are paired order-preservingly by similarity (``MOD_MIN``); the rest are add / del.
* LLM input: only add / del / mod that are not minor; the previous / next sentence goes in ``context`` with no ID.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Mapping, Sequence

from . import minor

DIFFER_VER = "diff-1"
MOD_MIN = 0.5      # similarity (on notation-normalised text) to pair two sentences of a replaced block as mod
SIDES = ("cur", "prev")

_SENT_RE = re.compile(r"[^\n。]*(?:。[」』）)〕】]*|\n|$)")
_EDGE_WS = " \t\r\f\v　"


@dataclass(frozen=True)
class Sentence:
    text: str
    start: int     # offset in the document text
    end: int


def split_sentences(text: str, base: int = 0) -> list[Sentence]:
    """Split ``text`` (which starts at offset ``base`` of the document) into non-blank sentences."""
    out: list[Sentence] = []
    for m in _SENT_RE.finditer(text):
        s, e = m.span()
        while s < e and text[s] in _EDGE_WS + "\n":
            s += 1
        while e > s and text[e - 1] in _EDGE_WS + "\n":
            e -= 1
        if s < e:
            out.append(Sentence(text[s:e], base + s, base + e))
    return out


def item_sentences(doc_text: str, items: Iterable[Mapping]) -> list[Sentence]:
    """Sentences of the item spans ``[start_off, end_off)`` (heading line included), in the given order."""
    out: list[Sentence] = []
    for it in items:
        s, e = it["start_off"], it["end_off"]
        out.extend(split_sentences(doc_text[s:e], s))
    return out


def text_sha8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def evidence_id(side: str, doc_id: str, s: Sentence) -> str:
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}: {side!r}")
    return f"{side}:{doc_id}:{s.start}-{s.end}:{text_sha8(s.text)}"


_EID_RE = re.compile(r"^(cur|prev):([^:]+):(\d+)-(\d+):([0-9a-f]{8})$")


def parse_evidence_id(eid: str) -> tuple[str, str, int, int, str]:
    m = _EID_RE.match(eid)
    if not m:
        raise ValueError(f"not an evidence ID: {eid!r}")
    side, doc, s, e, h = m.groups()
    return side, doc, int(s), int(e), h


def resolve_evidence(eid: str, doc_text: str) -> str:
    """The sentence an evidence ID points at; raises if the text there no longer has the recorded hash."""
    _, _, s, e, h = parse_evidence_id(eid)
    text = doc_text[s:e]
    if text_sha8(text) != h:
        raise ValueError(f"evidence {eid!r} does not match the document text")
    return text


# ---------------------------------------------------------------- alignment

@dataclass(frozen=True)
class Row:
    """One aligned row: ``equal`` (same text), ``mod`` (both sides), ``add`` (cur only), ``del`` (prev only)."""
    op: str
    prev: int | None     # index into the prev sentence list
    cur: int | None
    minor_kind: str | None = None   # minor.classify() for mod rows; None = substantive


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, minor.norm_notation(a), minor.norm_notation(b), autojunk=False).ratio()


def _pair_block(prev: Sequence[str], cur: Sequence[str]) -> list[tuple[int | None, int | None]]:
    """Order-preserving max-similarity pairing (pairs need ``MOD_MIN``); unpaired ones stand alone."""
    n, m = len(prev), len(cur)
    sim = [[_similarity(p, c) for c in cur] for p in prev]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            take = sim[i][j] + best[i + 1][j + 1] if sim[i][j] >= MOD_MIN else -1.0
            best[i][j] = max(take, best[i + 1][j], best[i][j + 1])
    out: list[tuple[int | None, int | None]] = []
    i = j = 0
    while i < n and j < m:
        if sim[i][j] >= MOD_MIN and best[i][j] == sim[i][j] + best[i + 1][j + 1]:
            out.append((i, j))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            out.append((i, None))
            i += 1
        else:
            out.append((None, j))
            j += 1
    out += [(k, None) for k in range(i, n)] + [(None, k) for k in range(j, m)]
    return out


def align(prev: Sequence[Sentence], cur: Sequence[Sentence]) -> list[Row]:
    """Rows covering every sentence of both sides once, in document order."""
    pt, ct = [s.text for s in prev], [s.text for s in cur]
    rows: list[Row] = []
    sm = SequenceMatcher(None, [minor.mask_numbers(t) for t in pt], [minor.mask_numbers(t) for t in ct],
                         autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                kind = minor.classify(pt[i], ct[j])
                rows.append(Row("equal", i, j) if kind == "identical" else Row("mod", i, j, kind))
            continue
        for a, b in _pair_block(pt[i1:i2], ct[j1:j2]):
            if a is not None and b is not None:
                rows.append(Row("mod", i1 + a, j1 + b, minor.classify(pt[i1 + a], ct[j1 + b])))
            elif a is not None:
                rows.append(Row("del", i1 + a, None))
            else:
                rows.append(Row("add", None, j1 + b))
    return rows


# ---------------------------------------------------------------- changes (sentence_changes rows)

@dataclass
class Change:
    op: str                          # add / del / mod
    cur: Sentence | None
    prev: Sentence | None
    cur_evidence_id: str | None
    prev_evidence_id: str | None
    is_minor: bool
    minor_kind: str | None
    context: dict = field(default_factory=dict)   # {cur,prev}_{before,after}: text only, never an ID

    def to_row(self, cur_doc: str, prev_doc: str, pair_idx: int) -> dict:
        """A ``sentence_changes`` row (§5)."""
        offsets = {k: [s.start, s.end] for k, s in (("cur", self.cur), ("prev", self.prev)) if s}
        key = "|".join([DIFFER_VER, cur_doc, prev_doc, str(pair_idx), self.op,
                        self.cur_evidence_id or "", self.prev_evidence_id or ""])
        return {"change_id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
                "cur_doc": cur_doc, "prev_doc": prev_doc, "differ_ver": DIFFER_VER, "pair_idx": pair_idx,
                "op": self.op, "cur_evidence_id": self.cur_evidence_id,
                "prev_evidence_id": self.prev_evidence_id,
                "text_cur": self.cur.text if self.cur else None,
                "text_prev": self.prev.text if self.prev else None,
                "offsets_json": json.dumps(offsets), "is_minor": int(self.is_minor),
                "context_json": json.dumps(self.context, ensure_ascii=False)}


def _context(sents: Sequence[Sentence], i: int, side: str) -> dict:
    out = {}
    if i > 0:
        out[f"{side}_before"] = sents[i - 1].text
    if i + 1 < len(sents):
        out[f"{side}_after"] = sents[i + 1].text
    return out


def changes(prev: Sequence[Sentence], cur: Sequence[Sentence], *, cur_doc: str, prev_doc: str,
            rows: list[Row] | None = None) -> list[Change]:
    """All add / del / mod rows as changes (minor ones flagged, not dropped — see ``for_llm``)."""
    out: list[Change] = []
    for r in rows if rows is not None else align(prev, cur):
        if r.op == "equal":
            continue
        c = cur[r.cur] if r.cur is not None else None
        p = prev[r.prev] if r.prev is not None else None
        ctx = (_context(cur, r.cur, "cur") if c else {}) | (_context(prev, r.prev, "prev") if p else {})
        out.append(Change(r.op, c, p, evidence_id("cur", cur_doc, c) if c else None,
                          evidence_id("prev", prev_doc, p) if p else None,
                          r.minor_kind is not None, r.minor_kind, ctx))
    return out


def for_llm(chs: Iterable[Change]) -> list[Change]:
    """Changes sent to the LLM: minor ones are shown to people only (§6-6, D4)."""
    return [c for c in chs if not c.is_minor]
