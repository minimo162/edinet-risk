"""entity_id = EDINET code after the code aggregation list and the manual successor map (plan §6-5).

- ``code_aggregation`` (ESE140190, abolished -> continuing) is applied first, then ``successor_map``
  (config/successor_map.csv, prev -> next). Both are followed until a fixed point (chains A->B->C).
- A cycle stops at the first repeated code; a prev code with several next codes (split) is not mapped.
  Both are reported in ``notes``.
- Aggregation targets missing from ``edinet_codes`` (e.g. E38680 -> E38709 in the 2026-10 lists) are
  still followed: the aggregation list is EDINET's own record that the codes are one filer, while the
  code list only covers codes that are still registered. They are listed in ``notes`` as
  ``aggregation_target_not_in_codes`` so a human can check them; population rows never carry such a
  code (population is joined through edinet_codes), so they cannot change the 突合 count.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Mapping

from ..store.db import transaction

CODE_RE = re.compile(r"^E\d{5}$")
SUCCESSOR_COLUMNS = ["prev_code", "next_code", "reason", "registered_by", "registered_at"]


class EntityError(Exception):
    pass


class EntityMap:
    """Callable: EDINET code -> entity_id. ``basis(code)`` gives the chain that was followed."""

    def __init__(self, aggregation: Mapping[str, str] | None = None,
                 successors: Mapping[str, list[str]] | None = None, known_codes: set[str] | None = None):
        self.aggregation = dict(aggregation or {})
        self.successors = {k: sorted(set(v)) for k, v in (successors or {}).items()}
        self.known_codes = known_codes
        self.notes: list[dict] = []
        for old, new in sorted(self.aggregation.items()):
            if known_codes is not None and new not in known_codes:
                self.notes.append({"note": "aggregation_target_not_in_codes", "old_code": old, "new_code": new,
                                   "decision": "followed (entity_id = new_code)"})
        for prev, nexts in sorted(self.successors.items()):
            if len(nexts) > 1:
                self.notes.append({"note": "successor_ambiguous", "prev_code": prev, "next_codes": nexts,
                                   "decision": "not mapped"})
        self._memo: dict[str, tuple[str, list[dict]]] = {}

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "EntityMap":
        agg = {r[0]: r[1] for r in conn.execute("SELECT old_code, new_code FROM code_aggregation")}
        succ: dict[str, list[str]] = defaultdict(list)
        for p, n in conn.execute("SELECT prev_code, next_code FROM successor_map"):
            succ[p].append(n)
        known = {r[0] for r in conn.execute("SELECT edinet_code FROM edinet_codes")}
        return cls(agg, succ, known or None)

    def _resolve(self, code: str) -> tuple[str, list[dict]]:
        if code in self._memo:
            return self._memo[code]
        chain, seen, cur = [], {code}, code
        while True:
            if cur in self.aggregation:
                nxt, via = self.aggregation[cur], "code_aggregation"
            elif len(self.successors.get(cur, [])) == 1:
                nxt, via = self.successors[cur][0], "successor_map"
            else:
                break
            if nxt in seen:
                chain.append({"from": cur, "to": nxt, "via": via, "stopped": "cycle"})
                break
            chain.append({"from": cur, "to": nxt, "via": via})
            seen.add(nxt)
            cur = nxt
        self._memo[code] = (cur, chain)
        return cur, chain

    def __call__(self, code: str) -> str:
        return self._resolve(code)[0]

    def basis(self, code: str) -> list[dict]:
        return self._resolve(code)[1]

    def members(self, codes) -> dict[str, list[str]]:
        """entity_id -> sorted codes that resolve to it (only for ``codes``)."""
        out: dict[str, set[str]] = defaultdict(set)
        for c in codes:
            out[self(c)].add(c)
        return {k: sorted(v) for k, v in out.items()}


# ---------------------------------------------------------------- config/successor_map.csv
def read_successor_csv(path: str | Path) -> list[dict]:
    """Rows of the successor map. Blank lines and lines starting with '#' are ignored."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    rows = list(csv.DictReader(lines))
    if lines and [c.strip() for c in next(csv.reader(lines[:1]))] != SUCCESSOR_COLUMNS:
        raise EntityError(f"{path}: header must be {','.join(SUCCESSOR_COLUMNS)}")
    out, bad = [], []
    for i, r in enumerate(rows, start=2):
        r = {k: (v or "").strip() for k, v in r.items() if k}
        p, n = r.get("prev_code", ""), r.get("next_code", "")
        if not CODE_RE.match(p) or not CODE_RE.match(n) or p == n:
            bad.append(f"row {i}: prev_code={p!r} next_code={n!r}")
            continue
        at = r.get("registered_at") or None
        if at:
            try:
                date.fromisoformat(at)
            except ValueError:
                bad.append(f"row {i}: registered_at={at!r} is not YYYY-MM-DD")
                continue
        out.append({"prev_code": p, "next_code": n, "reason": r.get("reason") or None,
                    "registered_by": r.get("registered_by") or None, "registered_at": at})
    if bad:
        raise EntityError(f"{path}: " + "; ".join(bad))
    return out


def load_successor_map(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Replace ``successor_map`` with the CSV (the CSV is the source of truth). Returns counts."""
    rows = read_successor_csv(path)
    with transaction(conn):
        before = conn.execute("SELECT count(*) FROM successor_map").fetchone()[0]
        conn.execute("DELETE FROM successor_map")
        conn.executemany("INSERT OR REPLACE INTO successor_map (prev_code, next_code, reason, registered_by, "
                         "registered_at) VALUES (:prev_code, :next_code, :reason, :registered_by, :registered_at)",
                         rows)
    return {"path": str(path), "rows": len(rows), "replaced": before}


def update_documents_entity(conn: sqlite3.Connection, emap: EntityMap) -> int:
    """Rewrite ``documents.entity_id`` with ``emap``. Returns the number of codes mapped elsewhere."""
    codes = [r[0] for r in conn.execute("SELECT DISTINCT edinet_code FROM documents WHERE edinet_code IS NOT NULL")]
    moved = {c: emap(c) for c in codes if emap(c) != c}
    with transaction(conn):
        conn.execute("UPDATE documents SET entity_id = edinet_code "
                     "WHERE edinet_code IS NOT NULL AND entity_id IS NOT edinet_code")
        conn.executemany("UPDATE documents SET entity_id = ? WHERE edinet_code = ?",
                         [(e, c) for c, e in sorted(moved.items())])
    return len(moved)
