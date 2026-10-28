"""Version groups and the adopted version per (entity_id, periodEnd) (plan §6-5, minimal M1).

Input is plain doclist records (``documents.json`` results, optionally with ``_fetched_at`` /
``_file_date`` from ``doclist_raw``); nothing here touches the network or the database.

Observed in the M0 cache (docs/spike-findings.md) and handled here:
- 訂正有報 (130/030001) carry ``periodStart``/``periodEnd`` = null. The period is taken from
  the parentDocID chain, else parsed from docDescription ("(2024/04/01－2025/03/31)").
- Withdrawn filings are re-listed with withdrawalStatus=2 and masked metadata (nulls, no
  opeDateTime). The withdrawal time is the submitDateTime of the 取下書 (withdrawalStatus=1,
  parentDocID = the withdrawn docID). Older unmasked records of the same docID in
  doclist_raw supply the metadata.
"""
from __future__ import annotations

import re
import tomllib
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping

PROCESSOR_VERSION = "resolve-0.1"

FORM_KIND = {"030000": "original", "030001": "correction", "702001": "xbrl_fix"}
# Fields that masked (withdrawn / expired) records blank out; filled from older records.
_META = ("edinetCode", "secCode", "JCN", "filerName", "ordinanceCode", "formCode", "docTypeCode",
         "periodStart", "periodEnd", "submitDateTime", "docDescription", "parentDocID",
         "xbrlFlag", "pdfFlag", "csvFlag")
_DESC_PERIOD_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*[－\-–~～]\s*(\d{4})/(\d{2})/(\d{2})")


# ---------------------------------------------------------------- filing filter (config/filing_filter.toml)
@dataclass(frozen=True)
class FilingFilter:
    doc_types: frozenset = frozenset({"120"})
    ordinances: frozenset = frozenset({"010"})
    forms: frozenset = frozenset({"030000"})
    correction_doc_types: frozenset = frozenset({"130"})
    extra_forms: frozenset = frozenset({"030001", "702001"})

    @classmethod
    def from_toml(cls, path: str | Path) -> "FilingFilter":
        with open(path, "rb") as f:
            d = tomllib.load(f)
        ar, cand = d.get("annual_report", {}), d.get("candidates", {})
        return cls(frozenset(ar.get("docTypeCode", ["120"])), frozenset(ar.get("ordinanceCode", ["010"])),
                   frozenset(ar.get("formCode", ["030000"])),
                   frozenset(cand.get("correction_docTypeCode", ["130"])),
                   frozenset(cand.get("formCode_extra", ["030001", "702001"])))

    def is_original(self, r: Mapping) -> bool:
        return (r.get("docTypeCode") in self.doc_types and r.get("ordinanceCode") in self.ordinances
                and r.get("formCode") in self.forms)

    def is_version(self, r: Mapping) -> bool:
        """Original, or a correction / XBRL fix of one (member of a version group)."""
        if self.is_original(r):
            return True
        return (r.get("ordinanceCode") in self.ordinances and r.get("formCode") in self.extra_forms
                and r.get("docTypeCode") in (self.doc_types | self.correction_doc_types))


# ---------------------------------------------------------------- base date T
def t_bound(as_of) -> str:
    """Base date T as an inclusive 'YYYY-MM-DD HH:MM' bound (EDINET timestamps are JST minutes)."""
    if isinstance(as_of, datetime):
        return as_of.strftime("%Y-%m-%d %H:%M")
    if isinstance(as_of, date):
        return f"{as_of.isoformat()} 23:59"
    s = str(as_of).replace("T", " ")
    return f"{s} 23:59" if len(s) == 10 else s[:16]


def _slot(r: Mapping) -> tuple[str, int]:
    """A record's place in the doclist: (file_date, seqNumber). Re-fetches give new snapshots of a slot."""
    return (r.get("_file_date") or "", int(r.get("seqNumber") or 0))


def _ordered(recs: list[Mapping]) -> list[Mapping]:
    """Oldest -> newest records of one docID (plan §6-1: max opeDateTime, then seqNumber).

    opeDateTime is only set on docInfoEditStatus=1 records (the edited re-listing); originals and
    masked records carry none. A slot's time is the max opeDateTime over its snapshots, so a later
    snapshot of an edited record that EDINET blanked (withdrawal) keeps its place. Ties: file_date,
    seqNumber, then fetched_at (a later snapshot of the same slot wins).
    """
    slot_ope: dict[tuple[str, int], str] = {}
    for r in recs:
        if r.get("opeDateTime"):
            k = _slot(r)
            slot_ope[k] = max(slot_ope.get(k, ""), r["opeDateTime"])

    def key(r):
        s = _slot(r)
        return (slot_ope.get(s, ""), s[0], s[1], r.get("_fetched_at") or "")
    return sorted(recs, key=key)


def merge_state(records: Iterable[Mapping], as_of=None) -> dict[str, dict]:
    """Per-docID state. ``as_of=None``: latest known state (the ``documents`` merge, §6-1).

    With ``as_of`` (base date T, §6-5): records whose opeDateTime is after T are ignored, and a
    withdrawal whose 取下書 was submitted after T is not applied. ``_notes`` lists what was
    adjusted (``withdrawal_after_T``, ``withdrawal_time_unknown``).
    """
    T = t_bound(as_of) if as_of is not None else None
    by_doc: dict[str, list[Mapping]] = defaultdict(list)
    withdrawn_at: dict[str, str] = {}
    for r in records:
        if T is not None and r.get("opeDateTime") and r["opeDateTime"] > T:
            continue
        by_doc[r["docID"]].append(r)
        if r.get("withdrawalStatus") == "1" and r.get("parentDocID") and r.get("submitDateTime"):
            p = r["parentDocID"]
            withdrawn_at[p] = min(withdrawn_at.get(p, r["submitDateTime"]), r["submitDateTime"])
    out = {}
    for doc_id, recs in by_doc.items():
        recs = _ordered(recs)
        s = dict(recs[-1])
        for older in reversed(recs[:-1]):
            for k in _META:
                if s.get(k) is None and older.get(k) is not None:
                    s[k] = older[k]
        notes = []
        if s.get("withdrawalStatus") == "2":
            wt = withdrawn_at.get(doc_id)
            s["_withdrawn_at"] = wt
            if T is not None:
                if wt is None:
                    notes.append("withdrawal_time_unknown")
                elif wt > T:
                    # the masked record's statuses (legalStatus=0 ...) are post-withdrawal too
                    pre = [r for r in recs if r.get("withdrawalStatus") not in ("1", "2")]
                    if pre:
                        for k in ("legalStatus", "disclosureStatus", "docInfoEditStatus"):
                            s[k] = pre[-1].get(k)
                    else:
                        notes.append("pre_withdrawal_state_unknown")
                    s["withdrawalStatus"] = "0"
                    notes.append("withdrawal_after_T")
        s["_notes"] = notes
        out[doc_id] = s
    _fill_periods(out)
    return out


def _fill_periods(states: dict[str, dict]) -> None:
    """periodStart/End for records that lack them (corrections): parent chain, then docDescription."""
    for s in states.values():
        if s.get("periodEnd"):
            s.setdefault("_period_basis", "record")
            continue
        seen, p = set(), s.get("parentDocID")
        while p and p in states and p not in seen:
            seen.add(p)
            if states[p].get("periodEnd"):
                s["periodStart"] = s.get("periodStart") or states[p].get("periodStart")
                s["periodEnd"] = states[p]["periodEnd"]
                s["_period_basis"] = f"parent:{p}"
                break
            p = states[p].get("parentDocID")
        else:
            m = _DESC_PERIOD_RE.search(s.get("docDescription") or "")
            if m:
                g = m.groups()
                s["periodStart"], s["periodEnd"] = "-".join(g[:3]), "-".join(g[3:])
                s["_period_basis"] = "description"
            else:
                s["_period_basis"] = "unknown"


# ---------------------------------------------------------------- version groups
@dataclass(frozen=True)
class Version:
    doc_id: str
    entity_id: str
    edinet_code: str
    period_start: str | None
    period_end: str
    submit_dt: str
    form_code: str
    kind: str                 # original | correction | xbrl_fix
    xbrl: bool
    parent_doc_id: str | None
    state: Mapping = field(default_factory=dict, compare=False, repr=False)


def build_groups(states: Mapping[str, Mapping], flt: FilingFilter,
                 entity_of: Callable[[str], str] | None = None) -> dict[tuple[str, str], list[Version]]:
    """(entity_id, periodEnd) -> versions sorted by (submitDateTime, docID). parentDocID is not used."""
    entity_of = entity_of or (lambda code: code)
    groups: dict[tuple[str, str], list[Version]] = defaultdict(list)
    for doc_id, s in states.items():
        if not flt.is_version(s) or not s.get("edinetCode") or not s.get("periodEnd"):
            continue
        v = Version(doc_id, entity_of(s["edinetCode"]), s["edinetCode"], s.get("periodStart"), s["periodEnd"],
                    s.get("submitDateTime") or "", s.get("formCode") or "",
                    "original" if flt.is_original(s) else FORM_KIND.get(s.get("formCode"), "correction"),
                    s.get("xbrlFlag") == "1", s.get("parentDocID"), s)
        groups[(v.entity_id, v.period_end)].append(v)
    for vs in groups.values():
        vs.sort(key=lambda v: (v.submit_dt, v.doc_id))
    return dict(groups)


def exclusion_reason(v: Version, T: str) -> str | None:
    s = v.state
    if s.get("withdrawalStatus") in ("1", "2"):
        return "withdrawn"
    if s.get("disclosureStatus") == "2":
        return "not_disclosed"
    if s.get("legalStatus") == "0":
        return "legal_expired"
    if not v.submit_dt or v.submit_dt > T:
        return "submitted_after_T"
    return None


# ---------------------------------------------------------------- adopted version
def norm_for_compare(text: str) -> str:
    """Comparison-only normalisation (NFKC, no whitespace); stored text stays as-is (§3-1)."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


@dataclass
class Selection:
    entity_id: str
    period_end: str
    period_start: str | None = None
    doc_id: str | None = None
    original_doc_id: str | None = None
    version_kind: str | None = None
    flags: list[str] = field(default_factory=list)
    basis: dict = field(default_factory=dict)

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)


TextOf = Callable[[str], "str | None"]   # docID -> BusinessRisks text, None if not extractable


def select_version(group: list[Version], as_of, *, text_of: TextOf | None = None,
                   partial_ratio: float = 0.5, available: Callable[[str], bool] | None = None) -> Selection:
    """Adopted version of one group at base date T (§6-5 steps 1-4).

    1. drop withdrawn / not disclosed / expired / submitted after T;
    2-3. latest xbrlFlag=1 version (corrections included); a newer xbrlFlag=0 one -> correction_unparsed;
    3a. (``text_of``) a correction without TextBlock, or shorter than ``partial_ratio`` x original
        -> correction_partial, fall back to the latest older version with a full TextBlock;
    4. (``text_of``) adopted != original and texts differ -> risk_changed_by_correction.

    ``available`` (full-population runs): docID -> body obtainable without the network. Texts are only
    read when the group has several xbrlFlag=1 versions and all of them are available; otherwise steps
    3a/4 are skipped and ``texts_pending`` lists the versions still to fetch (a missing body is not
    evidence of a partial correction).
    """
    T = t_bound(as_of)
    first = group[0]
    sel = Selection(first.entity_id, first.period_end, first.period_start)
    excluded, valid = {}, []
    for v in group:
        why = exclusion_reason(v, T)
        if why:
            excluded[v.doc_id] = why
        else:
            valid.append(v)
    sel.basis = {"as_of": T, "versions": [v.doc_id for v in group], "excluded": excluded,
                 "state_notes": {v.doc_id: list(v.state.get("_notes", [])) for v in group if v.state.get("_notes")},
                 "period_basis": {v.doc_id: v.state.get("_period_basis") for v in group
                                  if v.state.get("_period_basis") not in (None, "record")}}
    if not valid:
        sel.flag("no_valid_version")
        return sel
    original = next((v for v in valid if v.kind == "original"), valid[0])
    sel.original_doc_id = original.doc_id
    sel.period_start = original.period_start or sel.period_start
    xbrl = [v for v in valid if v.xbrl]
    if not xbrl:
        sel.flag("no_xbrl")
        if len(valid) > 1:
            sel.flag("correction_unparsed")
        return sel
    adopted = xbrl[-1]
    newer_unparsed = [v.doc_id for v in valid if not v.xbrl and (v.submit_dt, v.doc_id) > (adopted.submit_dt, adopted.doc_id)]
    if newer_unparsed:
        sel.flag("correction_unparsed")
        sel.basis["unparsed_corrections"] = newer_unparsed

    if text_of is not None and available is not None:
        if len(xbrl) < 2:
            text_of = None          # nothing to compare: one parsed version
        else:
            pending = [v.doc_id for v in xbrl if not available(v.doc_id)]
            if pending:
                sel.basis["texts_pending"] = pending
                text_of = None
    if text_of is not None:
        orig_text = text_of(original.doc_id) if original.xbrl else None
        orig_len = len(norm_for_compare(orig_text)) if orig_text is not None else None
        sel.basis["original_chars"] = orig_len
        partial = []
        i = len(xbrl) - 1
        while xbrl[i].doc_id != original.doc_id and i > 0:
            t = text_of(xbrl[i].doc_id)
            n = len(norm_for_compare(t)) if t is not None else None
            if n is None or (orig_len is not None and n < partial_ratio * orig_len):
                partial.append({"doc_id": xbrl[i].doc_id, "chars": n})
                i -= 1
                continue
            break
        if partial:
            sel.flag("correction_partial")
            sel.basis["partial_corrections"] = partial   # kept as reference for the diff
        adopted = xbrl[i]
        if adopted.doc_id != original.doc_id:
            cur_text = text_of(adopted.doc_id)
            if orig_text is not None and cur_text is not None:
                changed = norm_for_compare(orig_text) != norm_for_compare(cur_text)
                sel.basis["risk_changed_by_correction"] = changed
                if changed:
                    sel.flag("risk_changed_by_correction")
            else:
                sel.basis["risk_changed_by_correction"] = None  # original text not available
    else:
        sel.basis["texts_checked"] = False

    sel.doc_id = adopted.doc_id
    sel.version_kind = adopted.kind
    sel.basis["adopted_submit_dt"] = adopted.submit_dt
    return sel
