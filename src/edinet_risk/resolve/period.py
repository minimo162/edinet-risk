"""Current / previous period per entity (plan §6-5, minimal M1).

- current: the version group whose periodEnd falls in the current window (latest if several).
- previous: the group with the largest periodEnd < current.periodStart (no 12-month assumption).
- gap = current.periodStart - previous.periodEnd: 1 day normal, 2..prev_gap_max_days -> prev_gap
  (still compared), larger -> no comparison, no_prev_reason = prev_missing_year.
- irregular: fiscal period length outside [period_min_days, period_max_days].
- No previous group -> no_prev_reason = unknown; resolve.reasons refines it with the population,
  code lists and doclist coverage (new_listing / successor_suspected / ...). Group present but no
  usable version -> prev_not_fetched.
- filing_selection: the 突合 reason of the cur row and the 前期なし reason of the prev row are both
  in ``reason_basis_json.reason`` (the prev one also in the ``no_prev_reason`` column).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from ..config import Resolve, Window
from ..store.db import dumps, upsert
from .versions import Selection, TextOf, Version, select_version


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def period_days(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    return (_d(end) - _d(start)).days + 1


def mark_irregular(sel: Selection, cfg: Resolve) -> None:
    n = period_days(sel.period_start, sel.period_end)
    sel.basis["period_days"] = n
    if n is not None and not (cfg.period_min_days <= n <= cfg.period_max_days):
        sel.flag("irregular")


@dataclass
class Resolution:
    entity_id: str
    cur: Selection | None
    prev: Selection | None = None
    no_prev_reason: str | None = None
    flags: list[str] = field(default_factory=list)   # pair-level: prev_gap
    basis: dict = field(default_factory=dict)
    cur_reason: str | None = None    # 突合 reason when there is no usable current filing (resolve.reasons)


def resolve_entity(groups: dict[tuple[str, str], list[Version]], entity_id: str, window: Window, as_of,
                   cfg: Resolve | None = None, *, text_of: TextOf | None = None,
                   available=None) -> Resolution:
    cfg = cfg or Resolve()
    mine = {pe: vs for (e, pe), vs in groups.items() if e == entity_id}
    lo, hi = window.period_end_from.isoformat(), window.period_end_to.isoformat()
    in_window = sorted(pe for pe in mine if lo <= pe <= hi)
    res = Resolution(entity_id, None, basis={"window": [lo, hi], "period_ends": sorted(mine)})
    if not in_window:
        res.basis["cur_missing"] = True
        return res
    if len(in_window) > 1:
        res.basis["cur_candidates"] = in_window
    cur = select_version(mine[in_window[-1]], as_of, text_of=text_of, partial_ratio=cfg.partial_ratio,
                         available=available)
    mark_irregular(cur, cfg)
    res.cur = cur
    if not cur.period_start:
        res.no_prev_reason = "unknown"
        res.basis["cur_period_start_missing"] = True
        return res

    earlier = sorted(pe for pe in mine if pe < cur.period_start)
    if not earlier:
        res.no_prev_reason = "unknown"
        return res
    pe = earlier[-1]
    gap = (_d(cur.period_start) - _d(pe)).days
    res.basis.update(prev_period_end=pe, gap_days=gap)
    prev = select_version(mine[pe], as_of, text_of=text_of, partial_ratio=cfg.partial_ratio,
                         available=available)
    mark_irregular(prev, cfg)
    if gap > cfg.prev_gap_max_days:
        res.no_prev_reason = "prev_missing_year"   # never compare with a filing two years back
        res.basis["skipped_prev_doc_id"] = prev.doc_id
        return res
    if gap > cfg.prev_gap_ok_days:
        res.flags.append("prev_gap")
    res.prev = prev
    if prev.doc_id is None:
        res.no_prev_reason = "prev_not_fetched"
    return res


def save_resolution(conn: sqlite3.Connection, run_id: str, res: Resolution) -> None:
    """Write the cur / prev rows of ``filing_selection`` (caller owns the transaction)."""
    rows = [("cur", res.cur, None), ("prev", res.prev, res.no_prev_reason)]
    for role, sel, reason in rows:
        flags = sorted(set((sel.flags if sel else []) + (res.flags if role == "prev" else [])))
        basis = dict(sel.basis) if sel else {}
        if role == "prev" or sel is None:
            basis["pair"] = {k: v for k, v in res.basis.items() if k != "reason_basis"}
        if "reason_basis" in res.basis:
            basis["reason_basis"] = res.basis["reason_basis"]
        basis["reason"] = res.cur_reason if role == "cur" else reason
        upsert(conn, "filing_selection", {
            "run_id": run_id, "entity_id": res.entity_id, "role": role,
            "doc_id": sel.doc_id if sel else None, "original_doc_id": sel.original_doc_id if sel else None,
            "period_end": sel.period_end if sel else res.basis.get("prev_period_end"),
            "version_kind": sel.version_kind if sel else None, "flags_json": dumps(flags),
            "no_prev_reason": reason, "reason_basis_json": dumps(basis)},
            key=["run_id", "entity_id", "role"])
