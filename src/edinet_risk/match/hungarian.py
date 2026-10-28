"""Pure-Python Hungarian method (plan §3 match/hungarian.py, §7): O(n^2 m) for an n x m matrix, n <= m.

Shortest augmenting paths with potentials (Kuhn–Munkres / Jonker–Volgenant style). Same contract as
``scipy.optimize.linear_sum_assignment`` (scipy is only a test oracle, never a runtime dependency):
rectangular matrices are allowed, every row (or column, whichever is fewer) is assigned, and the result
is ``(row_ind, col_ind)`` sorted by row.
"""
from __future__ import annotations

import math
from typing import Sequence

INF = math.inf


def linear_sum_assignment(cost: Sequence[Sequence[float]], maximize: bool = False) -> tuple[list[int], list[int]]:
    n = len(cost)
    m = len(cost[0]) if n else 0
    if any(len(r) != m for r in cost):
        raise ValueError("cost matrix must be rectangular")
    if n == 0 or m == 0:
        return [], []
    a = [[float(x) for x in r] for r in cost]
    if any(not math.isfinite(x) for r in a for x in r):
        raise ValueError("cost matrix must be finite")
    if maximize:
        a = [[-x for x in r] for r in a]
    transposed = n > m
    if transposed:
        a = [list(c) for c in zip(*a)]
        n, m = m, n

    # 1-based potentials u (rows) / v (cols); p[j] = row matched to column j (0 = free).
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            row = a[i0 - 1]
            ui0 = u[i0]
            delta, j1 = INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = row[j - 1] - ui0 - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    pairs = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j]]
    if transposed:
        pairs = [(c, r) for r, c in pairs]
    pairs.sort()
    return [r for r, _ in pairs], [c for _, c in pairs]


def assignment_value(cost: Sequence[Sequence[float]], rows: Sequence[int], cols: Sequence[int]) -> float:
    return sum(cost[r][c] for r, c in zip(rows, cols))
