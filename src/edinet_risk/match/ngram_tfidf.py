"""Character n-gram TF-IDF and cosine similarity on dict sparse vectors (plan §3 match/ngram_tfidf.py, §7).

Texts are normalised only for comparison (plan §3-1): NFKC and all whitespace removed. Weights follow the
smoothed scikit-learn convention so the numbers are easy to check by hand:

    tf(g, d)  = raw count of n-gram g in d
    idf(g)    = ln((1 + N) / (1 + df(g))) + 1      (N = documents in the fitted corpus)
    vector(d) = tf * idf, L2-normalised            → cosine = dot product

An n-gram unseen at fit time gets df = 0. An empty text has the empty vector (cosine 0 with anything).
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Iterable, Mapping, Sequence

Vector = dict[str, float]

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Comparison form: NFKC, whitespace removed."""
    return _WS.sub("", unicodedata.normalize("NFKC", text or ""))


def ngrams(text: str, nmin: int = 2, nmax: int = 3) -> Counter:
    """Counts of character n-grams (nmin..nmax) of the normalised text. Texts shorter than nmin give
    their whole string as a single gram so that short headings still compare."""
    t = normalize(text)
    c: Counter = Counter()
    for n in range(nmin, nmax + 1):
        for i in range(len(t) - n + 1):
            c[t[i:i + n]] += 1
    if not c and t:
        c[t] += 1
    return c


def cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Dot product of two L2-normalised sparse vectors, clamped to [0, 1]."""
    if len(a) > len(b):
        a, b = b, a
    s = sum(w * b.get(g, 0.0) for g, w in a.items())
    return min(1.0, max(0.0, s))


class Tfidf:
    def __init__(self, docs: Iterable[str], nmin: int = 2, nmax: int = 3):
        if not 1 <= nmin <= nmax:
            raise ValueError("need 1 <= nmin <= nmax")
        self.nmin, self.nmax = nmin, nmax
        df: Counter = Counter()
        n = 0
        for d in docs:
            df.update(ngrams(d, nmin, nmax).keys())
            n += 1
        self.n_docs = n
        self.df = df

    def idf(self, gram: str) -> float:
        return math.log((1 + self.n_docs) / (1 + self.df.get(gram, 0))) + 1.0

    def vector(self, text: str) -> Vector:
        v = {g: tf * self.idf(g) for g, tf in ngrams(text, self.nmin, self.nmax).items()}
        norm = math.sqrt(sum(w * w for w in v.values()))
        return {g: w / norm for g, w in v.items()} if norm else {}

    def similarity(self, a: str, b: str) -> float:
        return cosine(self.vector(a), self.vector(b))


def similarity_matrix(rows: Sequence[str], cols: Sequence[str], nmin: int = 2, nmax: int = 3,
                      model: Tfidf | None = None) -> list[list[float]]:
    """cosine(rows[i], cols[j]) with a model fitted on rows + cols (unless one is given)."""
    model = model or Tfidf(list(rows) + list(cols), nmin, nmax)
    rv = [model.vector(t) for t in rows]
    cv = [model.vector(t) for t in cols]
    return [[cosine(r, c) for c in cv] for r in rv]
