"""Doclist ingestion and ``doclist sync`` (plan §3 ingest/doclist.py, §3-2, §6-1, §6-4, §6-5).

Saved ``documents.json`` day files (``cache/doclist/YYYY-MM-DD.json``, written by
``net.edinet_api.EdinetClient.list_documents``) are appended to ``doclist_raw`` and merged into
``documents``. ``sync`` fetches the days that are missing or not yet final, imports every cached day
of the range first (so a re-fetch never loses the older snapshot), and records each call in
``api_calls``.

- ``doclist_raw`` is append-only; the key is (file_date, seqNumber, fetched_at) with
  fetched_at = the response's ``metadata.processDateTime`` (JST), so re-importing the
  same file is a no-op and a re-fetched file adds a new snapshot.
- ``documents`` holds the merged latest state per docID (resolve.versions.merge_state: max
  opeDateTime, then seqNumber; docInfoEditStatus=1 re-listings win over the original).
- ``state_as_of(conn, T)``: the state visible at base date T, rebuilt from doclist_raw (§6-5).
- A cached day is *final* once it was fetched after that day ended (processDateTime date > file
  date): "当日分が取れたら前日までが確定" (§6-1). Non-final days are fetched again.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from ..net.edinet_api import JST, DoclistQuietHours, EdinetAuthError, EdinetError, in_quiet_hours
from ..resolve.entity import EntityMap
from ..resolve.versions import merge_state
from ..store.db import insert_ignore, transaction, upsert

log = logging.getLogger(__name__)

PROCESSOR_VERSION = "doclist-0.2"


def _fetched_at(obj: dict) -> str:
    """metadata.processDateTime ('YYYY-MM-DD HH:MM', JST) -> ISO-8601 with +09:00."""
    pdt = str((obj.get("metadata") or {}).get("processDateTime") or "")
    if len(pdt) >= 16:
        return f"{pdt[:10]}T{pdt[11:16]}:00+09:00"
    raise ValueError(f"doclist response without metadata.processDateTime: {pdt!r}")


def import_day(conn: sqlite3.Connection, obj: dict, file_date: str) -> int:
    """Append one day file to doclist_raw (insert-or-ignore). Returns rows offered."""
    fetched_at = _fetched_at(obj)
    n = 0
    for r in obj.get("results") or []:
        insert_ignore(conn, "doclist_raw", {
            "file_date": file_date, "seq_number": int(r["seqNumber"]), "fetched_at": fetched_at,
            "doc_id": r["docID"], "ope_date_time": r.get("opeDateTime"),
            "record_json": json.dumps(r, ensure_ascii=False, sort_keys=True)},
            key=["file_date", "seq_number", "fetched_at"])
        n += 1
    return n


def _imported(conn: sqlite3.Connection, file_date: str, fetched_at: str) -> bool:
    return conn.execute("SELECT 1 FROM doclist_raw WHERE file_date = ? AND fetched_at = ? LIMIT 1",
                        (file_date, fetched_at)).fetchone() is not None


def import_cache(conn: sqlite3.Connection, cache_dir: str | Path, dates: Iterable[str] | None = None) -> dict:
    """Import ``{cache_dir}/doclist/*.json`` (or only ``dates``), then rebuild ``documents``."""
    ddir = Path(cache_dir) / "doclist"
    files = sorted(ddir.glob("*.json")) if dates is None else [ddir / f"{d}.json" for d in dates]
    stats = {"files": 0, "records": 0, "missing": []}
    for f in files:
        if not f.exists():
            stats["missing"].append(f.stem)
            continue
        obj = json.loads(f.read_text(encoding="utf-8"))
        with transaction(conn):
            stats["records"] += import_day(conn, obj, f.stem)
        stats["files"] += 1
    stats["documents"] = merge_documents(conn)
    return stats


def load_records(conn: sqlite3.Connection, edinet_codes: Iterable[str] | None = None) -> list[dict]:
    """doclist_raw rows as record dicts (+ ``_file_date``/``_fetched_at``).

    With ``edinet_codes``: every record of any docID that was ever listed under one of
    the codes (masked records of withdrawn filings carry no edinetCode, so filtering per
    record would lose them).
    """
    sql = "SELECT file_date, fetched_at, record_json FROM doclist_raw"
    args: list = []
    if edinet_codes is not None:
        codes = sorted(set(edinet_codes))
        ph = ", ".join("?" for _ in codes)
        sql += (f" WHERE doc_id IN (SELECT doc_id FROM doclist_raw "
                f"WHERE json_extract(record_json, '$.edinetCode') IN ({ph}))")
        args = codes
    out = []
    for fd, fa, rj in conn.execute(sql + " ORDER BY fetched_at, file_date, seq_number", args):
        r = json.loads(rj)
        r["_file_date"], r["_fetched_at"] = fd, fa
        out.append(r)
    return out


def merge_documents(conn: sqlite3.Connection, entity_of=None) -> int:
    """Rebuild ``documents`` from all of doclist_raw (latest merged state, no base date).

    ``entity_of`` defaults to the code aggregation list + successor map in the database (resolve.entity).
    """
    entity_of = entity_of or EntityMap.from_db(conn)
    states = merge_state(load_records(conn))
    with transaction(conn):
        for doc_id, s in sorted(states.items()):
            upsert(conn, "documents", {
                "doc_id": doc_id, "edinet_code": s.get("edinetCode"),
                "entity_id": entity_of(s["edinetCode"]) if s.get("edinetCode") else None,
                "sec_code": s.get("secCode"), "filer_name": s.get("filerName"),
                "doc_description": s.get("docDescription"), "doc_type_code": s.get("docTypeCode"),
                "ordinance_code": s.get("ordinanceCode"), "form_code": s.get("formCode"),
                "period_start": s.get("periodStart"), "period_end": s.get("periodEnd"),
                "submit_dt": s.get("submitDateTime"), "parent_doc_id": s.get("parentDocID"),
                "xbrl_flag": s.get("xbrlFlag"), "csv_flag": s.get("csvFlag"),
                "withdrawal_status": s.get("withdrawalStatus"), "disclosure_status": s.get("disclosureStatus"),
                "legal_status": s.get("legalStatus"), "doc_info_edit_status": s.get("docInfoEditStatus"),
                "last_ope_dt": s.get("opeDateTime"), "last_seq_number": s.get("seqNumber"),
                "last_seen_file_date": s.get("_file_date")}, key=["doc_id"])
    return len(states)


def state_as_of(conn: sqlite3.Connection, as_of, edinet_codes: Iterable[str] | None = None) -> dict[str, dict]:
    """Per-docID state as visible at base date T (§6-5): records with opeDateTime <= T only, and
    withdrawals whose 取下書 was submitted after T not applied. See resolve.versions.merge_state."""
    return merge_state(load_records(conn, edinet_codes), as_of=as_of)


# ---------------------------------------------------------------- doclist sync (§3-2, §6-1)
def today_jst() -> date:
    return datetime.now(JST).date()


def date_range(d0: date, d1: date) -> list[str]:
    if d0 > d1:
        raise ValueError(f"--from {d0} is after --to {d1}")
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def daily_dates(today: date) -> list[str]:
    """--daily: the previous day and the current day (§6-1)."""
    return date_range(today - timedelta(days=1), today)


def window_dates(windows: Iterable, today: date) -> list[str]:
    """--rescan-month: every submit date of the given year windows (union), up to today."""
    out: set[str] = set()
    for w in windows:
        if w.submit_from <= today:
            out.update(date_range(w.submit_from, min(w.submit_to, today)))
    return sorted(out)


def cached_fetched_at(cache_dir: str | Path, file_date: str) -> str | None:
    """fetched_at of the cached day file, or None if there is none (or it is unreadable)."""
    f = Path(cache_dir) / "doclist" / f"{file_date}.json"
    if not f.exists():
        return None
    try:
        return _fetched_at(json.loads(f.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        log.warning("unreadable cache file %s: %s", f.name, e)
        return None


def is_final(file_date: str, fetched_at: str | None) -> bool:
    return fetched_at is not None and fetched_at[:10] > file_date


@dataclass
class SyncPlan:
    dates: list[str]
    to_fetch: list[str]                 # network calls (one per day; retries not counted)
    cached_final: list[str]
    cached_stale: list[str]             # cached but fetched on/before that day: fetched again
    future: list[str]                   # after today (JST): skipped

    @property
    def calls(self) -> int:
        return len(self.to_fetch)


def plan_sync(cache_dir: str | Path, dates: Iterable[str], today: date, *, refresh: bool = False) -> SyncPlan:
    """Which days need a call. ``refresh`` (--rescan-month) fetches every day again."""
    p = SyncPlan([], [], [], [], [])
    for d in dates:
        p.dates.append(d)
        if d > today.isoformat():
            p.future.append(d)
            continue
        fa = cached_fetched_at(cache_dir, d)
        if fa is not None:
            (p.cached_final if is_final(d, fa) else p.cached_stale).append(d)
        if refresh or not is_final(d, fa):
            p.to_fetch.append(d)
    return p


@dataclass
class SyncResult:
    plan: SyncPlan
    imported_days: int = 0              # non-empty day files newly appended to doclist_raw
    raw_rows: int = 0                   # rows offered to doclist_raw (insert-or-ignore)
    fetched: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)   # needed a call, but cache-only
    stopped: str | None = None          # auth / quiet_hours: the run was stopped
    documents: int = 0


def _import_file(conn: sqlite3.Connection, f: Path, res: SyncResult) -> None:
    obj = json.loads(f.read_text(encoding="utf-8"))
    if not obj.get("results") or _imported(conn, f.stem, _fetched_at(obj)):
        return                           # empty day (weekend / holiday) or snapshot already appended
    with transaction(conn):
        res.raw_rows += import_day(conn, obj, f.stem)
    res.imported_days += 1


def sync(conn: sqlite3.Connection, cache_dir: str | Path, plan: SyncPlan, client=None) -> SyncResult:
    """Import cached days of the plan, fetch ``plan.to_fetch`` with ``client`` (None = cache only),
    then rebuild ``documents``.

    401/403 (HTTP or in-body) and the quiet hours stop the run at once; any other EdinetError
    (incl. metadata.status != 200 after the client's retries) marks that day failed and goes on.
    """
    res = SyncResult(plan)
    ddir = Path(cache_dir) / "doclist"
    for d in plan.dates:                 # older snapshots first, before a re-fetch replaces the file
        if d not in plan.future and (ddir / f"{d}.json").exists():
            _import_file(conn, ddir / f"{d}.json", res)
    for i, d in enumerate(plan.to_fetch):
        if client is None:
            res.missing.append(d)
            continue
        try:
            client.list_documents(d, refresh=True)
        except EdinetAuthError as e:
            res.stopped, res.failed[d] = "auth", str(e)
            log.error("stopping: %s", e)
            break
        except DoclistQuietHours as e:
            res.stopped, res.failed[d] = "quiet_hours", str(e)
            log.error("stopping: %s", e)
            break
        except EdinetError as e:
            res.failed[d] = str(e)
            log.error("doclist %s failed: %s", d, e)
            continue
        res.fetched.append(d)
        _import_file(conn, ddir / f"{d}.json", res)
        if (i + 1) % 50 == 0:
            log.info("doclist sync: %d/%d days fetched", i + 1, len(plan.to_fetch))
    if res.stopped is not None:
        res.missing.extend(d for d in plan.to_fetch if d not in res.fetched and d not in res.failed)
    res.documents = merge_documents(conn)
    return res


def quiet_now(start: str, end: str, now: datetime | None = None) -> bool:
    return in_quiet_hours(now or datetime.now(JST), start, end)
