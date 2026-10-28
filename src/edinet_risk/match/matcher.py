"""Two-stage item matching, current vs previous period (plan §3 match/matcher.py, §6-6, review §5-1).

Input: the leaf items of both periods in document order (segment.segmenter ``Segmentation.leaves``, or
the dicts in ``segments_transport7.json``). Thresholds come from ``config/thresholds.toml``.

1. **anchor** — the normalised heading occurs exactly once on each side (1:1 exact match) and
   body_sim >= ``anchor_body_sim_min``.
2. **assign** — on the remaining items, S = weight_body*body_sim + weight_head*head_sim and the
   Hungarian method maximises the total S. S >= ``accept_min`` → matched; ``review_min`` <= S < accept →
   matched with ``review_flag``; S < ``review_min`` → left unmatched.
3. **contain** — an unmatched current item whose text is contained (ratio >= ``contain_min``) in a
   previous item joins that item's pair (1 prev : n cur = split); then an unmatched previous item
   contained in a current item joins that pair (n prev : 1 cur = merged). A pair grown this way gets
   stage ``contain`` and is flagged for review if its S is below ``accept_min``.
4. Still unmatched → ``added`` (current only) / ``deleted`` (previous only), stage None.
5. **moved** — pairs ordered by previous position; those outside the LIS of their current positions are
   ``moved``. Renumbering (different ordinal) is recorded in ``renumbered``, not in the label.

Labels for 1:1 pairs, first that applies: moved > retitled (normalised heading differs) > modified
(normalised body differs) > same. Normalisation (NFKC, whitespace removed) is for comparison only, so
whitespace-only edits are ``same`` here; the diff step still shows them.

body_sim / head_sim: cosine of character n-gram TF-IDF vectors (ngram_tfidf), fitted per document pair on
the bodies (resp. headings) of both periods. If either heading is empty (paragraph fallback), S = body_sim.
Containment(a in b) = share of a's normalised characters covered by common runs of at least
``CONTAIN_MIN_RUN`` characters (difflib, autojunk off), so shared stock phrases do not count.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..config import Thresholds
from .hungarian import linear_sum_assignment
from .lis import not_in_lis
from .ngram_tfidf import Tfidf, cosine, normalize

MATCHER_VER = "match-1"
LABELS = ("same", "modified", "retitled", "moved", "merged", "split", "added", "deleted")
CONTAIN_MIN_RUN = 8   # characters; shorter common runs (stock phrases) are not evidence of containment


@dataclass(frozen=True)
class MItem:
    idx: int                 # item_idx in the segmentation (stable id written to matches.*_items)
    heading_raw: str
    heading_norm: str
    body: str
    number: int | None = None

    @classmethod
    def coerce(cls, x: Any) -> "MItem":
        if isinstance(x, MItem):
            return x
        get = x.get if isinstance(x, Mapping) else (lambda k, d=None: getattr(x, k, d))
        return cls(int(get("idx")), get("heading_raw") or "", get("heading_norm") or "", get("body") or "",
                   get("number"))


@dataclass
class Pair:
    cur_items: list[int]               # item idx values (current period)
    prev_items: list[int]              # item idx values (previous period)
    label: str
    stage: str | None                  # anchor / assign / contain; None for added / deleted
    score_s: float | None = None
    body_sim: float | None = None
    head_sim: float | None = None
    review_flag: bool = False
    renumbered: bool = False

    def to_row(self) -> dict:
        return asdict(self)


@dataclass
class MatchResult:
    pairs: list[Pair]
    input_sha256: str
    thresholds_version: str
    matcher_ver: str = MATCHER_VER
    n_cur: int = 0
    n_prev: int = 0

    def label_counts(self) -> Counter:
        return Counter(p.label for p in self.pairs)

    def stage_counts(self) -> Counter:
        return Counter(p.stage for p in self.pairs if p.stage)


@dataclass
class _Group:
    cur: list[int]                     # positions in the cur list
    prev: list[int]                    # positions in the prev list
    stage: str
    review: bool = False
    s: float = 0.0
    b: float = 0.0
    h: float = 0.0
    extra: dict = field(default_factory=dict)


def input_hash(cur: Sequence[MItem], prev: Sequence[MItem], th: Thresholds) -> str:
    def side(items):
        return [[it.idx, it.heading_norm, it.body, it.number] for it in items]
    blob = json.dumps({"ver": MATCHER_VER, "th": th.fingerprint(), "cur": side(cur), "prev": side(prev)},
                      ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def containment(a: str, b: str, min_run: int = CONTAIN_MIN_RUN) -> float:
    """Share of normalised ``a`` covered by common runs (>= min_run chars) with normalised ``b``."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    return sum(blk.size for blk in sm.get_matching_blocks() if blk.size >= min_run) / len(na)


class _Scorer:
    def __init__(self, cur: Sequence[MItem], prev: Sequence[MItem], th: Thresholds):
        self.th = th
        self.body = Tfidf([it.body for it in (*cur, *prev)], th.ngram_min, th.ngram_max)
        self.head = Tfidf([it.heading_norm for it in (*cur, *prev)], th.ngram_min, th.ngram_max)
        self.cb = [self.body.vector(it.body) for it in cur]
        self.pb = [self.body.vector(it.body) for it in prev]
        self.ch = [self.head.vector(it.heading_norm) for it in cur]
        self.ph = [self.head.vector(it.heading_norm) for it in prev]
        self.cur, self.prev = cur, prev

    def combine(self, b: float, h: float, has_heads: bool) -> float:
        return self.th.weight_body * b + self.th.weight_head * h if has_heads else b

    def pair(self, i: int, j: int) -> tuple[float, float, float]:
        b, h = cosine(self.cb[i], self.pb[j]), cosine(self.ch[i], self.ph[j])
        has = bool(self.cur[i].heading_norm and self.prev[j].heading_norm)
        return self.combine(b, h, has), b, h

    def group(self, ci: Sequence[int], pj: Sequence[int]) -> tuple[float, float, float]:
        """Scores of a 1:n / n:1 pair: body of the concatenated sides, best heading similarity."""
        if len(ci) == 1 and len(pj) == 1:
            return self.pair(ci[0], pj[0])
        b = self.body.similarity("".join(self.cur[i].body for i in ci), "".join(self.prev[j].body for j in pj))
        h = max(cosine(self.ch[i], self.ph[j]) for i in ci for j in pj)
        has = any(self.cur[i].heading_norm for i in ci) and any(self.prev[j].heading_norm for j in pj)
        return self.combine(b, h, has), b, h


def match(cur_items: Iterable[Any], prev_items: Iterable[Any], th: Thresholds | None = None) -> MatchResult:
    th = th or Thresholds()
    cur = [MItem.coerce(x) for x in cur_items]
    prev = [MItem.coerce(x) for x in prev_items]
    sc = _Scorer(cur, prev, th)
    groups: list[_Group] = []
    cur_g: dict[int, _Group] = {}
    prev_g: dict[int, _Group] = {}

    def add(g: _Group) -> None:
        groups.append(g)
        for i in g.cur:
            cur_g[i] = g
        for j in g.prev:
            prev_g[j] = g

    # 1. anchors: unique normalised heading on both sides + enough body similarity
    cnt_c = Counter(it.heading_norm for it in cur if it.heading_norm)
    cnt_p = Counter(it.heading_norm for it in prev if it.heading_norm)
    prev_pos = {it.heading_norm: j for j, it in enumerate(prev) if cnt_p[it.heading_norm] == 1}
    for i, it in enumerate(cur):
        j = prev_pos.get(it.heading_norm)
        if j is None or cnt_c[it.heading_norm] != 1:
            continue
        s, b, h = sc.pair(i, j)
        if b >= th.anchor_body_sim_min:
            add(_Group([i], [j], "anchor", False, s, b, h))

    # 2. Hungarian assignment on the rest
    ri = [i for i in range(len(cur)) if i not in cur_g]
    cj = [j for j in range(len(prev)) if j not in prev_g]
    if ri and cj:
        scores = [[sc.pair(i, j) for j in cj] for i in ri]
        rows, cols = linear_sum_assignment([[t[0] for t in r] for r in scores], maximize=True)
        for r, c in zip(rows, cols):
            s, b, h = scores[r][c]
            if s >= th.review_min:
                add(_Group([ri[r]], [cj[c]], "assign", s < th.accept_min, s, b, h))

    # 3. containment: split (unmatched cur inside a prev item), then merge (unmatched prev inside a cur item)
    grown: set[int] = set()
    for i in range(len(cur)):
        if i in cur_g:
            continue
        best = max(((containment(cur[i].body, prev[j].body), j) for j in range(len(prev))
                    if len(prev_g[j].prev if j in prev_g else [j]) == 1), default=(0.0, -1))
        if best[0] < th.contain_min:
            continue
        j = best[1]
        g = prev_g.get(j)
        if g is None:
            g = _Group([], [j], "contain")
            add(g)
        g.cur.append(i)
        g.cur.sort()
        cur_g[i] = g
        grown.add(id(g))
    for j in range(len(prev)):
        if j in prev_g:
            continue
        best = max(((containment(prev[j].body, cur[i].body), i) for i in range(len(cur))
                    if len(cur_g[i].cur if i in cur_g else [i]) == 1), default=(0.0, -1))
        if best[0] < th.contain_min:
            continue
        i = best[1]
        g = cur_g.get(i)
        if g is None:
            g = _Group([i], [], "contain")
            add(g)
        g.prev.append(j)
        g.prev.sort()
        prev_g[j] = g
        grown.add(id(g))
    for g in groups:
        if id(g) in grown:
            g.stage = "contain"
            g.s, g.b, g.h = sc.group(g.cur, g.prev)
            g.review = g.s < th.accept_min

    # 5. moved: LIS over current positions of the pairs in previous order
    order = sorted(groups, key=lambda g: (min(g.prev), min(g.cur)))
    moved = {id(order[k]) for k in not_in_lis([min(g.cur) for g in order])}

    pairs: list[tuple[tuple, Pair]] = []
    for g in groups:
        ci, pj = g.cur, g.prev
        if len(ci) > 1:
            label = "split"
        elif len(pj) > 1:
            label = "merged"
        else:
            c, p = cur[ci[0]], prev[pj[0]]
            if id(g) in moved:
                label = "moved"
            elif c.heading_norm != p.heading_norm:
                label = "retitled"
            elif normalize(c.body) != normalize(p.body):
                label = "modified"
            else:
                label = "same"
        renum = (len(ci) == 1 and len(pj) == 1 and cur[ci[0]].number is not None
                 and prev[pj[0]].number is not None and cur[ci[0]].number != prev[pj[0]].number)
        pairs.append(((min(ci), 0, min(pj)), Pair(
            [cur[i].idx for i in ci], [prev[j].idx for j in pj], label, g.stage,
            round(g.s, 6), round(g.b, 6), round(g.h, 6), g.review, renum)))
    for i in range(len(cur)):
        if i not in cur_g:
            pairs.append(((i, 0, -1), Pair([cur[i].idx], [], "added", None)))
    # a deleted item is listed right after the pair holding the nearest earlier previous item
    for j in range(len(prev)):
        if j in prev_g:
            continue
        anchor = next((prev_g[k] for k in range(j - 1, -1, -1) if k in prev_g), None)
        key = (max(anchor.cur) if anchor else -1, 1, j)
        pairs.append((key, Pair([], [prev[j].idx], "deleted", None)))
    pairs.sort(key=lambda t: t[0])
    return MatchResult([p for _, p in pairs], input_hash(cur, prev, th), th.version,
                       n_cur=len(cur), n_prev=len(prev))
