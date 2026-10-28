"""`resolve --fy --as-of`: adopted version, previous period and reasons for a set of targets (plan §3-2, §6-5).

Targets are the population (JPX prime x EDINET codes, ``population`` table), ``--entity`` codes, or a
targets file (config/transport7.toml; ``m1_resolve`` goes through here too). Offline: input is
``doclist_raw`` / ``edinet_codes`` / ``code_aggregation`` / ``successor_map`` / ``fetches`` / ``extracts``
and cached type=1 ZIPs; the network is never used unless the caller passes a fetching ``text_of``.

Per run: entity_id via resolve.entity (documents.entity_id is rewritten), T-state via merge_state,
version groups, resolve_entity per entity, resolve.reasons.classify, one ``filing_selection`` cur/prev
pair per entity (one transaction per entity, §6-3). Records without edinetCode are skipped and counted.
"""
from __future__ import annotations

import calendar
import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from ..config import Resolve, Window
from ..ingest import doclist
from ..store import db
from .entity import EntityMap, update_documents_entity
from .period import Resolution, resolve_entity, save_resolution
from .reasons import Context, Target, classify
from .versions import PROCESSOR_VERSION, FilingFilter, build_groups, merge_state

FLAGS = ("irregular", "prev_gap", "correction_unparsed", "correction_partial", "risk_changed_by_correction")


def fy_window(fy: str) -> Window:
    """'2026-03' -> periodEnd 2025-04-01..2026-03-31, submit 2025-04-01..(periodEnd window end + 6 months)."""
    y, m = (int(x) for x in fy.split("-"))
    end = date(y, m, calendar.monthrange(y, m)[1])
    start = date(y - 1, m, 1) if m == 12 else date(y - 1, m + 1, 1)
    sy, sm = (y, m + 6) if m <= 6 else (y + 1, m - 6)
    return Window(start, end, start, date(sy, sm, calendar.monthrange(sy, sm)[1]))


# ---------------------------------------------------------------- targets
def population_targets(conn: sqlite3.Connection, as_of_month: str | None = None) -> tuple[str, list[Target]]:
    if as_of_month is None:
        as_of_month = conn.execute("SELECT max(as_of_month) FROM population").fetchone()[0]
        if as_of_month is None:
            raise ValueError("population is empty; run `population load` first")
    rows = conn.execute("SELECT sec_code4, name, edinet_code FROM population WHERE as_of_month = ? "
                        "ORDER BY sec_code4", (as_of_month,)).fetchall()
    if not rows:
        raise ValueError(f"no population rows for {as_of_month}")
    return as_of_month, [Target(r["sec_code4"], r["edinet_code"], r["name"], f"population:{as_of_month}")
                         for r in rows]


def code_targets(conn: sqlite3.Connection, codes: Iterable[str], source: str = "entity") -> list[Target]:
    names = {r[0]: r[1] for r in conn.execute("SELECT edinet_code, name FROM edinet_codes")}
    return [Target(c, c, names.get(c), source) for c in codes]


# ---------------------------------------------------------------- context
def scanned_dates(conn: sqlite3.Connection, cache_dir: Path | None) -> set[str]:
    """Doclist days known to be scanned: doclist_raw file dates + cached day files (empty days have no rows)."""
    days = {r[0] for r in conn.execute("SELECT DISTINCT file_date FROM doclist_raw")}
    if cache_dir is not None and (Path(cache_dir) / "doclist").is_dir():
        days |= {p.stem for p in (Path(cache_dir) / "doclist").glob("????-??-??.json")}
    return days


def fiscal_year_ends(cache_dir: Path | None) -> dict[str, str]:
    """決算日 per code from the cached Edinetcode.zip (``codes sync``); {} when absent."""
    p = Path(cache_dir) / "codes" / "Edinetcode.zip" if cache_dir is not None else None
    if p is None or not p.exists():
        return {}
    from ..population.edinet_codes import fiscal_year_ends as parse
    return parse(p.read_bytes())


def _fetch_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM fetches WHERE type = 1")}


def _extract_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT doc_id, extractor_ver, char_len, hit_count FROM extracts ORDER BY extractor_ver"):
        out[r["doc_id"]] = dict(r)       # latest extractor_ver wins
    return out


@dataclass
class Outcome:
    run_id: str
    results: list[tuple[Target, Resolution]] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)   # targets mapped to an entity already resolved
    skipped_no_edinet_code: int = 0
    entity_notes: list[dict] = field(default_factory=list)
    documents_remapped: int = 0


def resolve_targets(conn: sqlite3.Connection, targets: list[Target], *, run_id: str, as_of: date, window: Window,
                    cfg: Resolve | None = None, flt: FilingFilter | None = None, cache_dir: Path | None = None,
                    text_of: Callable | None = None, available: Callable | None = None,
                    limit_records: bool = False) -> Outcome:
    """Resolve ``targets`` and write ``filing_selection`` for ``run_id`` (the caller owns the runs row).

    ``limit_records``: load only the doclist records of the targets' entities (M1: 7 filers); the
    successor search then only sees those entities.
    """
    cfg, flt = cfg or Resolve(), flt or FilingFilter()
    emap = EntityMap.from_db(conn)
    out = Outcome(run_id, entity_notes=emap.notes)
    out.documents_remapped = update_documents_entity(conn, emap)
    codes = None
    if limit_records:
        ents = {emap(t.edinet_code) for t in targets if t.edinet_code}
        known = _doclist_codes(conn) | {t.edinet_code for t in targets if t.edinet_code}
        codes = sorted(c for c in known if emap(c) in ents)
    states = merge_state(doclist.load_records(conn, codes), as_of=as_of)
    out.skipped_no_edinet_code = sum(1 for s in states.values() if not s.get("edinetCode"))
    groups = build_groups(states, flt, entity_of=emap)
    known_codes = {r[0] for r in conn.execute("SELECT edinet_code FROM edinet_codes")} or None
    ctx = Context(as_of, window, flt, emap, states, known_codes=known_codes,
                  scanned=scanned_dates(conn, cache_dir), fetch_status=_fetch_rows(conn),
                  extracts=_extract_rows(conn), gap_max_days=cfg.prev_gap_max_days,
                  fiscal_year_end=fiscal_year_ends(cache_dir))
    seen: dict[str, str] = {}
    for t in targets:
        if t.edinet_code:
            entity_id = emap(t.edinet_code)
        else:
            entity_id = f"nocode:{t.key}"
        if entity_id in seen:
            out.duplicates.append({"key": t.key, "edinet_code": t.edinet_code, "entity_id": entity_id,
                                   "first_key": seen[entity_id]})
            continue
        seen[entity_id] = t.key
        res = resolve_entity(groups, entity_id, window, as_of, cfg, text_of=text_of, available=available)
        classify(res, t, ctx)
        with db.transaction(conn):
            save_resolution(conn, run_id, res)
        out.results.append((t, res))
    return out


def _doclist_codes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT edinet_code FROM documents WHERE edinet_code IS NOT NULL")}


# ---------------------------------------------------------------- summary / outputs
def row(t: Target, res: Resolution) -> dict:
    cur, prev = res.cur, res.prev
    rb = res.basis.get("reason_basis", {})
    return {"key": t.key, "name": t.name, "edinet_code": t.edinet_code, "entity_id": res.entity_id,
            "cur_doc_id": cur.doc_id if cur else None, "cur_period_end": cur.period_end if cur else None,
            "cur_kind": cur.version_kind if cur else None, "cur_flags": sorted(cur.flags) if cur else [],
            "cur_reason": res.cur_reason,
            "prev_doc_id": prev.doc_id if prev else None, "prev_period_end": prev.period_end if prev else None,
            "prev_kind": prev.version_kind if prev else None,
            "prev_flags": sorted(set((prev.flags if prev else []) + res.flags)),
            "no_prev_reason": res.no_prev_reason, "gap_days": res.basis.get("gap_days"),
            "unknown_cause": rb.get("unknown_cause")}


def summarize(out: Outcome) -> dict:
    rows = [row(t, r) for t, r in out.results]
    matched = [r for r in rows if r["cur_doc_id"] and not r["cur_reason"]]
    cur_reasons = Counter(r["cur_reason"] for r in rows if r["cur_reason"])
    with_cur = [r for r in rows if r["cur_doc_id"]]
    prev_reasons = Counter(r["no_prev_reason"] for r in with_cur if r["no_prev_reason"])
    flags = Counter()
    for r in rows:
        for f in set(r["cur_flags"]) | set(r["prev_flags"]):
            flags[f] += 1
    listed = {f: [r["key"] for r in rows if f in r["cur_flags"] or f in r["prev_flags"]] for f in FLAGS}
    unknown = [r for r in rows if r["cur_reason"] == "unknown" or (r["cur_doc_id"] and r["no_prev_reason"] == "unknown")]
    return {"targets": len(rows) + len(out.duplicates), "entities": len(rows), "duplicates": out.duplicates,
            "matched": len(matched), "with_prev": sum(1 for r in with_cur if r["prev_doc_id"] and not r["no_prev_reason"]),
            "cur_reasons": dict(sorted(cur_reasons.items())), "no_prev_reasons": dict(sorted(prev_reasons.items())),
            "unknown": len(unknown), "unknown_causes": dict(sorted(Counter(r["unknown_cause"] for r in unknown).items(),
                                                                  key=lambda kv: str(kv[0]))),
            "flags": {f: flags.get(f, 0) for f in FLAGS}, "flag_lists": listed,
            "texts_pending": sum(1 for _, r in out.results
                                 if any(sel is not None and sel.basis.get("texts_pending") for sel in (r.cur, r.prev))),
            "out_of_scope_present": sum(1 for _, r in out.results if r.basis.get("reason_basis", {}).get("out_of_scope_docs")),
            "skipped_no_edinet_code": out.skipped_no_edinet_code, "documents_remapped": out.documents_remapped,
            "entity_notes": out.entity_notes, "rows": rows}


def write_outputs(out_dir: Path, summary: dict, meta: dict) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / "resolve.json", "csv": out_dir / "resolve.csv", "md": out_dir / "resolve.md"}
    paths["json"].write_text(json.dumps({**meta, **summary}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    cols = list(summary["rows"][0]) if summary["rows"] else []
    with open(paths["csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in summary["rows"]:
            w.writerow({k: ";".join(v) if isinstance(v, list) else v for k, v in r.items()})
    lines = [f"# resolve（{meta['scope']}、FY {meta['fy']}、基準日 T={meta['as_of']}）", "",
             f"run_id={meta['run_id']}、periodEnd 窓 {meta['window'][0]}〜{meta['window'][1]}。", "",
             "| 項目 | 件数 |", "|---|---|",
             f"| 対象 | {summary['targets']}（entity {summary['entities']}、重複 {len(summary['duplicates'])}） |",
             f"| 当期の採用版あり（突合） | {summary['matched']} |",
             f"| 前期あり | {summary['with_prev']} |",
             f"| unknown（突合＋前期なし） | {summary['unknown']} |",
             f"| edinetCode なしでスキップした書類 | {summary['skipped_no_edinet_code']} |",
             f"| 訂正あり・本文未取得で correction_partial／risk_changed_by_correction が未判定 | "
             f"{summary['texts_pending']} |", "",
             "## 突合の理由", "", "| 理由 | 件数 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["cur_reasons"].items()] or ["| - | 0 |"]
    lines += ["", "## 前期なしの理由（当期の採用版がある会社）", "", "| 理由 | 件数 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["no_prev_reasons"].items()] or ["| - | 0 |"]
    lines += ["", "## unknown の内訳（unknown_cause）", "", "| cause | 件数 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["unknown_causes"].items()] or ["| - | 0 |"]
    lines += ["", "## フラグ", "", "| flag | 社数 | 会社 |", "|---|---|---|"]
    lines += [f"| {f} | {summary['flags'][f]} | {', '.join(summary['flag_lists'][f][:30])}"
              f"{' …' if len(summary['flag_lists'][f]) > 30 else ''} |" for f in FLAGS]
    if summary["entity_notes"]:
        lines += ["", "## entity_id の注記", ""] + [f"- {json.dumps(n, ensure_ascii=False)}" for n in summary["entity_notes"]]
    paths["md"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


def start(conn: sqlite3.Connection, run_id: str, cmd: str, *, fy: str | None, as_of: date, args: dict) -> None:
    db.start_run(conn, run_id, cmd, code_version=PROCESSOR_VERSION, fy=fy, as_of=as_of.isoformat(), args=args)
    with db.transaction(conn):
        conn.execute("UPDATE runs SET meta_synced_at = (SELECT max(fetched_at) FROM doclist_raw) WHERE run_id = ?",
                     (run_id,))
