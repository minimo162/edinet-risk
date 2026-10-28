"""Longest strictly increasing subsequence (plan §3 match/lis.py): used to tell moved items apart.

Matched pairs are listed in previous-period order; the LIS of their current-period positions is the
largest set that kept its relative order. Pairs outside it are "moved". O(n log n), deterministic:
among equally long subsequences the one ending at the earliest-finishing tail is returned.
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Sequence


def lis_indices(seq: Sequence) -> list[int]:
    """Indices (ascending) of one longest strictly increasing subsequence of ``seq``."""
    tails: list = []        # tails[k] = smallest tail value of an increasing run of length k+1
    tail_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, x in enumerate(seq):
        k = bisect_left(tails, x)
        if k == len(tails):
            tails.append(x)
            tail_idx.append(i)
        else:
            tails[k] = x
            tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    out = []
    i = tail_idx[-1] if tail_idx else -1
    while i >= 0:
        out.append(i)
        i = prev[i]
    return out[::-1]


def not_in_lis(seq: Sequence) -> set[int]:
    keep = set(lis_indices(seq))
    return {i for i in range(len(seq)) if i not in keep}
