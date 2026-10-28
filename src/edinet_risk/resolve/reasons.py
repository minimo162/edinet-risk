"""突合 / 前期なし reasons with the data they were decided on (plan §6-5 table, M2-1 / M2-3).

``classify(res, target, ctx)`` refines a ``period.Resolution`` in place:

- ``res.cur_reason`` (no usable current filing, 突合):
  ``code_mismatch`` (population.edinet_code empty or not in edinet_codes) > ``form_out_of_scope``
  (a 120/130 of the entity outside the filing filter in the periodEnd window) > ``not_filed`` / ``late``
  (no in-scope 120 by T; deadline = expected periodEnd + 3 months, before T -> late; no earlier 有報 or
  unscanned days -> unknown) > ``xbrl_missing``
  (only xbrlFlag=0 versions) > ``fetch_failed`` (fetches.status=failed|retry, or an empty extract).
- ``res.no_prev_reason`` (current filing found, no previous one): ``prev_missing_year`` / ``prev_gap``
  (period.resolve_entity; only when the in-between year was scanned) > ``prev_not_fetched`` (group without usable version, fetch failed, empty
  extract) > ``successor_suspected`` (an in-scope 120 for the previous period under another entity with
  the same secCode / 法人番号 / filer name, or a 組織再編成 有価証券届出書 of the entity) > ``new_listing`` (a 有価証券届出書 of the entity submitted
  after the previous periodEnd minus ``NEW_LISTING_LEAD_DAYS``; formCode 024000 新規公開時 only) > ``unknown``.
- ``unknown`` always carries ``unknown_cause`` in the basis; ``doclist_not_scanned`` means the days on
  which the previous filing was due are not in the scanned doclist (so its absence is not established).

Every decision writes the values it used to ``res.basis["reason_basis"]`` (-> reason_basis_json).
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Mapping

from ..config import Window
from .period import Resolution
from .versions import FilingFilter, t_bound

CUR_REASONS = ("code_mismatch", "form_out_of_scope", "not_filed", "late", "xbrl_missing", "fetch_failed", "unknown")
PREV_REASONS = ("prev_missing_year", "prev_not_fetched", "successor_suspected", "new_listing", "unknown")
# 有価証券届出書（新規公開時）: docTypeCode 030 / formCode 024000 (docDescription in the 2025-26 doclists).
# 参照方式 (023000) etc. are filed by listed companies (stock plans, offerings) and are no evidence of a listing.
REGISTRATION_FORMS = (("030", "024000"),)
# 有価証券届出書（組織再編成・上場 027000／組織再編成 026000）: a holding company formation / merger. The
# previous 有報 is under the predecessor's code -> successor_suspected (to be put in successor_map.csv).
REORGANIZATION_FORMS = (("030", "027000"), ("030", "026000"))
REGISTRATION_TYPES = ("030", "040")        # 届出書 / 訂正届出書: not evidence that the filer already existed
NEW_LISTING_LEAD_DAYS = 120                # 届出書 may precede the listing (and the prev periodEnd) a little
SUCCESSOR_LOOKBACK_DAYS = 400              # previous-period 120 of another entity: periodEnd within this
_NAME_SUFFIX_RE = re.compile(r"(株式会社|\(株\)|ホールディングス|グループ本社|グループ|HD|HOLDINGS|GROUP)", re.I)


def add_months(d: date, n: int) -> date:
    """Month arithmetic keeping month ends (2025-03-31 + 3 -> 2025-06-30, 2025-02-28 + 12 -> 2026-02-28)."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    last = calendar.monthrange(y, m)[1]
    is_end = d.day == calendar.monthrange(d.year, d.month)[1]
    return date(y, m, last if is_end else min(d.day, last))


def norm_name(name: str | None) -> str:
    s = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or ""))
    return _NAME_SUFFIX_RE.sub("", s).upper()


def _withdrawn(s: Mapping) -> bool:
    return s.get("withdrawalStatus") in ("1", "2") or s.get("disclosureStatus") == "2"


@dataclass
class Target:
    key: str                    # sec_code4 (population) / label (targets file) / code (--entity)
    edinet_code: str | None
    name: str | None = None
    source: str = ""


@dataclass
class Context:
    as_of: date
    window: Window
    flt: FilingFilter
    entity_of: object                      # resolve.entity.EntityMap (callable)
    states: Mapping[str, Mapping]          # T-states of all loaded docIDs (merge_state(as_of=T))
    known_codes: set[str] | None = None    # edinet_codes (None: list not loaded, code check skipped)
    scanned: set[str] = field(default_factory=set)       # doclist file dates scanned (YYYY-MM-DD)
    fetch_status: Mapping[str, Mapping] = field(default_factory=dict)   # doc_id -> fetches row (type=1)
    extracts: Mapping[str, Mapping] = field(default_factory=dict)       # doc_id -> latest extracts row
    gap_max_days: int = 31                 # Resolve.prev_gap_max_days (recorded with prev_missing_year)
    fiscal_year_end: Mapping[str, str] = field(default_factory=dict)  # code -> 決算日 'MM-DD' (Edinetcode.zip)
    by_entity: dict = field(default_factory=dict, init=False)
    annual_index: dict = field(default_factory=dict, init=False)

    def __post_init__(self):
        self.T = t_bound(self.as_of)
        by_entity, idx = defaultdict(list), defaultdict(list)
        for doc_id, s in self.states.items():
            code = s.get("edinetCode")
            if not code:
                continue
            ent = self.entity_of(code)
            by_entity[ent].append((doc_id, s))
            if self.flt.is_original(s) and s.get("periodEnd") and not _withdrawn(s):
                for k in self._keys(s):
                    idx[k].append((ent, s["periodEnd"], doc_id, code))
        self.by_entity, self.annual_index = dict(by_entity), dict(idx)

    @staticmethod
    def _keys(s: Mapping) -> list[tuple[str, str]]:
        keys = []
        if s.get("secCode"):
            keys.append(("secCode", s["secCode"]))
        if s.get("JCN"):
            keys.append(("JCN", s["JCN"]))
        if norm_name(s.get("filerName")):
            keys.append(("name", norm_name(s.get("filerName"))))
        return keys

    def missing_days(self, lo: date, hi: date) -> list[str]:
        """Weekdays in [lo, hi] (capped at T) whose doclist was not scanned."""
        hi = min(hi, self.as_of)
        out, d = [], lo
        while d <= hi:
            if d.weekday() < 5 and d.isoformat() not in self.scanned:
                out.append(d.isoformat())
            d += timedelta(days=1)
        return out


def ranges(days: list[str]) -> list[str]:
    """['2026-07-01', '2026-07-02', '2026-07-06'] -> ['2026-07-01..2026-07-06'] (weekend gaps merged)."""
    out: list[list[date]] = []
    for s in days:
        d = date.fromisoformat(s)
        if out and (d - out[-1][1]).days <= 3:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [f"{a}..{b}" if a != b else f"{a}" for a, b in out]


def _in_scope_period_ends(ctx: Context, entity_id: str) -> list[str]:
    return sorted({s["periodEnd"] for _, s in ctx.by_entity.get(entity_id, [])
                   if ctx.flt.is_version(s) and s.get("periodEnd") and not _withdrawn(s)})


def _out_of_scope(ctx: Context, entity_id: str) -> list[dict]:
    lo, hi = ctx.window.period_end_from.isoformat(), ctx.window.period_end_to.isoformat()
    out = []
    for doc_id, s in ctx.by_entity.get(entity_id, []):
        if s.get("docTypeCode") not in ("120", "130") or ctx.flt.is_version(s) or _withdrawn(s):
            continue
        pe = s.get("periodEnd")
        if pe and lo <= pe <= hi and (s.get("submitDateTime") or "") <= ctx.T:
            out.append({"doc_id": doc_id, "docTypeCode": s.get("docTypeCode"), "ordinanceCode": s.get("ordinanceCode"),
                        "formCode": s.get("formCode"), "periodEnd": pe, "submitDateTime": s.get("submitDateTime")})
    return sorted(out, key=lambda r: r["doc_id"])


def _fetch_problem(ctx: Context, doc_id: str | None) -> dict | None:
    if not doc_id:
        return None
    f = ctx.fetch_status.get(doc_id)
    if f and f.get("status") in ("failed", "retry"):   # retry: transient failure still queued
        return {"doc_id": doc_id, "fetch_status": f["status"], "attempts": f.get("attempts"),
                "last_error": f.get("last_error")}
    e = ctx.extracts.get(doc_id)
    if e and (not e.get("char_len") or not e.get("hit_count")):
        return {"doc_id": doc_id, "empty_extract": True, "char_len": e.get("char_len"), "hit_count": e.get("hit_count")}
    return None


def _fye_in_window(ctx: Context, mmdd: str) -> date | None:
    """Latest date with 決算日 ``mmdd`` inside the periodEnd window (month ends kept: 02-28 -> 02-29)."""
    mm, dd = (int(x) for x in mmdd.split("-"))
    for y in (ctx.window.period_end_to.year, ctx.window.period_end_to.year - 1):
        last = calendar.monthrange(y, mm)[1]
        d = date(y, mm, last if dd >= 28 and dd >= calendar.monthrange(2023, mm)[1] else min(dd, last))
        if ctx.window.period_end_from <= d <= ctx.window.period_end_to:
            return d
    return None


def _no_filing_reason(ctx: Context, entity_id: str, group_pe: str | None, code: str | None = None) -> tuple[str, dict]:
    """not_filed / late: expected periodEnd -> deadline (+3 months) vs T.

    The expected periodEnd comes from the excluded group itself, else the latest earlier 有報 + 12 months,
    else the 決算日 of the code list (only if the entity has a doclist record on/before that date: a holding
    company set up later has no fiscal year end in the window). Otherwise the deadline is unknown ->
    ``unknown`` (no_annual_report_history). The absence
    of a filing must be established by the scanned doclist: [periodEnd+1, T] for not_filed,
    [periodEnd+1, deadline] for late; otherwise ``unknown`` (doclist_not_scanned).
    """
    if group_pe:
        exp, how = date.fromisoformat(group_pe), "group_period_end"
    else:
        before = [pe for pe in _in_scope_period_ends(ctx, entity_id) if pe < ctx.window.period_end_from.isoformat()]
        fye = ctx.fiscal_year_end.get(code or entity_id)
        first = min((s.get("submitDateTime") or "9" for _, s in ctx.by_entity.get(entity_id, [])
                     if s.get("docTypeCode") not in REGISTRATION_TYPES), default=None)
        exp = _fye_in_window(ctx, fye) if fye else None
        if not before and exp is not None and first and first[:10] <= exp.isoformat():
            before, how = None, f"edinet_codes_決算日:{fye} (first doclist record {first[:10]})"
        elif not before:
            return "unknown", {"fiscal_year_end": fye, "first_doclist_submit": first,"unknown_cause": "no_annual_report_history", "doc_type_120_by_T": False,
                               "registration_statements": _registrations(ctx, entity_id, ctx.window.period_end_from),
                               "reorganization_statements": _registrations(ctx, entity_id, ctx.window.period_end_from,
                                                                           REORGANIZATION_FORMS),
                               "doc_types_seen": sorted({s.get("docTypeCode") or "-"
                                                         for _, s in ctx.by_entity.get(entity_id, [])})}
        if before:
            exp, how = add_months(date.fromisoformat(before[-1]), 12), f"previous_period_end+12m:{before[-1]}"
    deadline = add_months(exp, 3)
    reason = "not_filed" if ctx.as_of <= deadline else "late"
    missing = ctx.missing_days(exp + timedelta(days=1), min(deadline, ctx.as_of))
    basis = {"expected_period_end": exp.isoformat(), "expected_basis": how, "deadline": deadline.isoformat(),
             "as_of": ctx.as_of.isoformat(), "doc_type_120_by_T": False,
             "unscanned_after_deadline": ranges(ctx.missing_days(deadline + timedelta(days=1), ctx.as_of))}
    if missing:
        basis.update(unknown_cause="doclist_not_scanned", would_be=reason, unscanned_days=ranges(missing))
        return "unknown", basis
    return reason, basis


def classify(res: Resolution, target: Target, ctx: Context) -> Resolution:
    rb: dict = {"target": {"key": target.key, "edinet_code": target.edinet_code, "source": target.source}}
    res.basis["reason_basis"] = rb
    if target.edinet_code:
        chain = ctx.entity_of.basis(target.edinet_code) if hasattr(ctx.entity_of, "basis") else []
        if chain:
            rb["entity_chain"] = chain

    # ---------------------------------------------------------------- 突合 (current filing)
    if not target.edinet_code or (ctx.known_codes is not None and target.edinet_code not in ctx.known_codes):
        res.cur_reason = "code_mismatch"
        rb["code_mismatch"] = {"population_edinet_code": target.edinet_code,
                               "in_edinet_codes": bool(target.edinet_code) and ctx.known_codes is not None
                               and target.edinet_code in ctx.known_codes}
        return res
    oos = _out_of_scope(ctx, res.entity_id)
    if oos:
        rb["out_of_scope_docs"] = oos
    cur = res.cur
    if cur is None or (cur.doc_id is None and "no_valid_version" in cur.flags):
        if oos:
            res.cur_reason = "form_out_of_scope"
            return res
        res.cur_reason, rb["no_filing"] = _no_filing_reason(ctx, res.entity_id, cur.period_end if cur else None,
                                                             target.edinet_code)
        if res.cur_reason == "unknown":
            rb["unknown_cause"] = rb["no_filing"].pop("unknown_cause")
        if cur is not None:
            rb["no_filing"]["excluded_versions"] = cur.basis.get("excluded")
        return res
    if cur.doc_id is None:
        res.cur_reason = "xbrl_missing"
        rb["xbrl_missing"] = {"versions": cur.basis.get("versions"), "xbrl_flag": "0"}
        return res
    prob = _fetch_problem(ctx, cur.doc_id)
    if prob:
        res.cur_reason = "fetch_failed"
        rb["fetch_failed"] = prob

    # ---------------------------------------------------------------- 前期なし (previous filing)
    if res.no_prev_reason == "prev_missing_year" and cur.period_start:
        exp_pe = date.fromisoformat(cur.period_start) - timedelta(days=1)
        missing = ctx.missing_days(exp_pe + timedelta(days=1), add_months(exp_pe, 3))
        if missing:     # the year in between may simply not be scanned
            res.no_prev_reason = "unknown"
            rb.update(unknown_cause="doclist_not_scanned", would_be="prev_missing_year",
                      expected_prev_period_end=exp_pe.isoformat(), unscanned_days=ranges(missing),
                      older_period_end=res.basis.get("prev_period_end"), gap_days=res.basis.get("gap_days"))
            return res
        rb["prev_missing_year"] = {"gap_days": res.basis.get("gap_days"), "max_days": ctx.gap_max_days,
                                   "prev_period_end": res.basis.get("prev_period_end"),
                                   "skipped_prev_doc_id": res.basis.get("skipped_prev_doc_id")}
        return res
    if res.prev is not None:
        if res.prev.doc_id is None:
            res.no_prev_reason = "prev_not_fetched"
            rb["prev_not_fetched"] = {"flags": res.prev.flags, "excluded": res.prev.basis.get("excluded")}
        elif (prob := _fetch_problem(ctx, res.prev.doc_id)):
            res.no_prev_reason = "prev_not_fetched"
            rb["prev_not_fetched"] = prob
        return res
    if res.no_prev_reason != "unknown" or not cur.period_start:
        if not cur.period_start:
            rb["unknown_cause"] = "current_period_start_missing"
        return res

    exp_pe = date.fromisoformat(cur.period_start) - timedelta(days=1)
    rb["expected_prev_period_end"] = exp_pe.isoformat()
    cand = _successor_candidates(ctx, res.entity_id, cur, exp_pe)
    reorg = _registrations(ctx, res.entity_id, exp_pe, REORGANIZATION_FORMS)
    if cand or reorg:
        res.no_prev_reason = "successor_suspected"
        rb["successor_candidates"] = cand
        rb["reorganization_statements"] = reorg
        rb["action"] = "人が確認し、正しければ config/successor_map.csv に登録"
        return res
    regs = _registrations(ctx, res.entity_id, exp_pe)
    missing = ctx.missing_days(exp_pe + timedelta(days=1), add_months(exp_pe, 3))
    if regs:
        res.no_prev_reason = "new_listing"
        rb["new_listing"] = {"registration_statements": regs, "lead_days": NEW_LISTING_LEAD_DAYS,
                             "prev_filing_days_scanned": not missing}
        return res
    if missing:
        rb["unknown_cause"] = "doclist_not_scanned"
        rb["unscanned_days"] = ranges(missing)
    else:
        rb["unknown_cause"] = "no_basis_matched"
    names = sorted({s.get("filerName") for _, s in ctx.by_entity.get(res.entity_id, []) if s.get("filerName")})
    if len(names) > 1:
        rb["filer_names"] = names          # name change: a hint for the human check
    return res


def _successor_candidates(ctx: Context, entity_id: str, cur, exp_pe: date) -> list[dict]:
    s = ctx.states.get(cur.doc_id) or {}
    lo = (exp_pe - timedelta(days=SUCCESSOR_LOOKBACK_DAYS)).isoformat()
    found: dict[str, dict] = {}
    for kind, val in Context._keys(s):
        for ent, pe, doc_id, code in ctx.annual_index.get((kind, val), []):
            if ent == entity_id or not (lo <= pe < cur.period_start):
                continue
            c = found.setdefault(doc_id, {"doc_id": doc_id, "edinet_code": code, "entity_id": ent,
                                          "period_end": pe, "filer_name": (ctx.states[doc_id] or {}).get("filerName"),
                                          "matched_on": []})
            c["matched_on"].append(kind)
    return sorted(found.values(), key=lambda c: (c["period_end"], c["doc_id"]), reverse=True)


def _registrations(ctx: Context, entity_id: str, exp_pe: date, forms=REGISTRATION_FORMS) -> list[dict]:
    lo = (exp_pe - timedelta(days=NEW_LISTING_LEAD_DAYS)).isoformat()
    out = []
    for doc_id, s in ctx.by_entity.get(entity_id, []):
        sub = s.get("submitDateTime") or ""
        if ((s.get("docTypeCode"), s.get("formCode")) in forms and not _withdrawn(s)
                and lo <= sub[:10] and sub <= ctx.T):
            out.append({"doc_id": doc_id, "docTypeCode": s.get("docTypeCode"), "formCode": s.get("formCode"),
                        "submitDateTime": sub})
    return sorted(out, key=lambda r: r["submitDateTime"])
