"""M1 step I: ``run`` — resolve -> fetch -> compare -> report in one go (plan §3-2, §4 M1-5/6).

One ``runs`` row (cmd='run') owns the whole pipeline; every stage writes under ``{out}/run_{run_id}/``:
``resolve_transport7.{json,md}``, ``compare.json``, the report files and ``fingerprint.json``.

- fetch: the adopted current / previous type=1 ZIPs (and, during resolve, the texts of correction groups)
  are taken from ``cache/docs`` and only fetched when missing (needs EDINET_API_KEY; every network call goes to ``api_calls`` with this run_id). A re-run
  therefore makes no document API call. With ``offline=True`` the whole run is inside
  ``net.http.forbid_network()``: nothing is fetched and any attempt raises ``NetworkForbidden``.
- fingerprint (M1-5): (a) per table, the hash of its rows without the run_id column — tables with a run_id
  column contribute only this run's rows; ``runs`` / ``api_calls`` / ``metrics`` are left out —, (b) the
  hash of the Excel cell value + style snapshot (``report.excel.snapshot``, §8-2), (c) the hash of every
  HTML page; the run_id is masked (plain string replace, so it must be distinctive — the default is a
  timestamp) in (b), (c) and the other text outputs. The xlsx binary is never hashed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import m1_compare, m1_report, m1_resolve
from .config import Resolve, Window, api_key
from .net.http import forbid_network
from .report import excel
from .store import db

log = logging.getLogger(__name__)

RUN_VER = "run-1"
EXCLUDED_TABLES = ("runs", "api_calls", "metrics")
FINGERPRINT = "fingerprint.json"
RUN_MASK = "<RUN>"


def new_run_id() -> str:
    """JST timestamp + a short random suffix (two runs in the same second must not collide)."""
    return f"{m1_compare.new_run_id()}-{uuid.uuid4().hex[:4]}"


@dataclass
class Inputs:
    targets: Path = Path("config/transport7.toml")
    cache_dir: Path = Path("cache")
    filter_path: Path = Path("config/filing_filter.toml")
    thresholds: Path = Path("config/thresholds.toml")
    m0_dir: Path = Path("out/m0")
    out_dir: Path = Path("out/m1")
    db_path: Path = Path("out/m1/m1.sqlite")
    gold: Path | None = None
    as_of: date = date(2026, 9, 30)
    fy: str | None = "2026-03"
    window: Window = field(default_factory=Window)
    resolve: Resolve = field(default_factory=Resolve)


# ---------------------------------------------------------------- fetch

def fetch_documents(doc_ids, cache_dir: Path, conn, *, client=None, offline: bool = False) -> dict:
    """Ensure ``cache/docs/{docID}_type1.zip`` for each docID; fetch only what is missing."""
    out = {"cached": [], "fetched": [], "missing": {}}
    for d in sorted(set(filter(None, doc_ids))):
        p = Path(cache_dir) / "docs" / f"{d}_type1.zip"
        if p.exists():
            out["cached"].append(d)
            continue
        if offline:
            out["missing"][d] = "type=1 not in cache (--offline)"
            continue
        if client is None:
            out["missing"][d] = "type=1 not in cache and EDINET_API_KEY is not set"
            continue
        from .net.edinet_api import EdinetError
        try:
            p = client.get_document(d, 1)
        except EdinetError as e:  # key-masked
            out["missing"][d] = f"fetch failed: {e}"
            _fetch_row(conn, d, "failed", error=str(e))
            continue
        out["fetched"].append(d)
        _fetch_row(conn, d, "ok", path=p)
    return out


def _fetch_row(conn, doc_id: str, status: str, *, path: Path | None = None, error: str | None = None) -> None:
    if conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone() is None:
        return  # fetches.doc_id references documents
    body = path.read_bytes() if path is not None else None
    with db.transaction(conn):
        prev = conn.execute("SELECT attempts FROM fetches WHERE doc_id = ? AND type = 1", (doc_id,)).fetchone()
        db.upsert(conn, "fetches", {
            "doc_id": doc_id, "type": 1, "status": status, "path": str(path) if path else None,
            "sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
            "size": len(body) if body is not None else None, "attempts": (prev[0] if prev else 0) + 1,
            "last_error": error, "fetched_at": db.now_iso() if status == "ok" else None},
            key=("doc_id", "type"))


# ---------------------------------------------------------------- fingerprint (M1-5)

def _sha(b: bytes | str) -> str:
    return hashlib.sha256(b.encode("utf-8") if isinstance(b, str) else b).hexdigest()


def table_hashes(conn, run_id: str) -> dict[str, dict]:
    """{table: {rows, sha256}} over every table but runs / api_calls / metrics, run_id column dropped."""
    out = {}
    for t in db.tables(conn):
        if t in EXCLUDED_TABLES:
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        keep = [c for c in cols if c != "run_id"]
        sql = f"SELECT {', '.join(keep)} FROM {t}"
        params: tuple = ()
        if "run_id" in cols:
            sql, params = sql + " WHERE run_id = ?", (run_id,)
        sql += " ORDER BY " + ", ".join(str(i + 1) for i in range(len(keep)))
        h, n = hashlib.sha256(), 0
        for r in conn.execute(sql, params):
            h.update(json.dumps(list(r), ensure_ascii=False).encode("utf-8") + b"\n")
            n += 1
        out[t] = {"rows": n, "sha256": h.hexdigest()}
    return out


def output_hashes(run_dir: Path, run_id: str) -> dict:
    """(b) Excel snapshot, (c) HTML pages and the other text outputs, run_id masked."""
    run_dir = Path(run_dir)
    xlsx = run_dir / m1_report.XLSX_NAME
    snap = excel.snapshot(xlsx, {run_id: RUN_MASK}) if xlsx.exists() else None
    html = {p.relative_to(run_dir).as_posix(): _sha(p.read_text(encoding="utf-8").replace(run_id, RUN_MASK))
            for p in sorted((run_dir / "html").glob("*.html"))}
    files = {}
    for name in ("compare.json", "resolve_transport7.json", "resolve_transport7.md", "reconcile.csv",
                 "reconcile.md"):
        p = run_dir / name
        if p.exists():
            files[name] = _sha(p.read_text(encoding="utf-8-sig").replace(run_id, RUN_MASK))
    return {"excel": {"cells": len(snap or []),
                      "sha256": _sha(json.dumps(snap, ensure_ascii=False, default=str)) if snap is not None else None},
            "html": html, "files": files}


def fingerprint(conn, run_id: str, run_dir: Path) -> dict:
    tables = table_hashes(conn, run_id)
    outs = output_hashes(run_dir, run_id)
    digest = {"tables": _sha(json.dumps(tables, sort_keys=True)), "excel": outs["excel"]["sha256"],
              "html": _sha(json.dumps(outs["html"], sort_keys=True)),
              "files": _sha(json.dumps(outs["files"], sort_keys=True))}
    return {"run_id": run_id, "digest": digest, "tables": tables, **outs}


def diff_fingerprints(a: dict, b: dict) -> list[str]:
    """Names of the compared items that differ (empty = idempotent)."""
    bad = [f"table:{t}" for t in sorted(set(a["tables"]) | set(b["tables"]))
           if a["tables"].get(t) != b["tables"].get(t)]
    if a["excel"] != b["excel"]:
        bad.append("excel")
    bad += [f"html:{n}" for n in sorted(set(a["html"]) | set(b["html"])) if a["html"].get(n) != b["html"].get(n)]
    bad += [f"file:{n}" for n in sorted(set(a["files"]) | set(b["files"])) if a["files"].get(n) != b["files"].get(n)]
    return bad


def previous_fingerprint(out_dir: Path, run_id: str) -> dict | None:
    """fingerprint.json of the newest other run under ``out_dir`` (run ids sort by time)."""
    fps = [p for p in sorted(Path(out_dir).glob(f"run_*/{FINGERPRINT}")) if p.parent.name != f"run_{run_id}"]
    return json.loads(fps[-1].read_text(encoding="utf-8")) if fps else None


# ---------------------------------------------------------------- run

def doc_api_calls(conn, run_id: str) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT endpoint, count(*) FROM api_calls WHERE run_id = ? GROUP BY endpoint", (run_id,))}


def run(inp: Inputs, *, offline: bool = False, run_id: str | None = None, client=None) -> dict:
    """resolve -> fetch -> compare -> report. ``client``: an EdinetClient to use instead of a new one (tests)."""
    run_id = run_id or new_run_id()
    run_dir = Path(inp.out_dir) / f"run_{run_id}"
    with forbid_network() if offline else contextlib.nullcontext():
        conn = db.open_db(inp.db_path)
        try:
            db.start_run(conn, run_id, "run", code_version=RUN_VER, fy=inp.fy, as_of=inp.as_of.isoformat(),
                         args={"offline": offline, "targets": str(inp.targets), "m0": str(inp.m0_dir),
                               "cache": str(inp.cache_dir), "thresholds": str(inp.thresholds)})
            status = "failed"
            try:
                if offline:
                    client = None
                elif client is None and api_key():
                    from .net.edinet_api import EdinetClient
                    client = EdinetClient(cache_dir=inp.cache_dir, audit_hook=db.api_call_recorder(conn, run_id))
                res = m1_resolve.run(inp.targets, inp.cache_dir, inp.filter_path, run_dir, inp.db_path,
                                     as_of=inp.as_of, window=inp.window, cfg=inp.resolve,
                                     fetch=client is not None, run_id=run_id, client=client)
                with db.transaction(conn):
                    conn.execute("UPDATE runs SET meta_synced_at = (SELECT max(fetched_at) FROM doclist_raw) "
                                 "WHERE run_id = ?", (run_id,))
                docs = [r[k] for r in res["rows"] for k in ("cur_doc_id", "prev_doc_id")]
                fetched = fetch_documents(docs, inp.cache_dir, conn, client=client, offline=offline)
                for d in res["texts"].fetched:   # fetched by resolve (version texts), same audited client
                    _fetch_row(conn, d, "ok", path=res["texts"].zip_path(d))
                    if d in fetched["cached"]:
                        fetched["cached"].remove(d)
                    fetched["fetched"] = sorted(set(fetched["fetched"]) | {d})
                cmp = m1_compare.run(run_dir / "resolve_transport7.json", inp.m0_dir, inp.cache_dir, inp.thresholds,
                                     inp.out_dir, run_id, db_path=inp.db_path, own_run=False)
                rep = m1_report.run(run_dir, gold_path=inp.gold)
                status = "ok"
            finally:
                db.finish_run(conn, run_id, status)
            calls = doc_api_calls(conn, run_id)
            fp = fingerprint(conn, run_id, run_dir)
        finally:
            conn.close()
    (run_dir / FINGERPRINT).write_text(json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                       encoding="utf-8")
    return {"run_id": run_id, "run_dir": run_dir, "resolve": res, "fetch": fetched, "compare": cmp,
            "report": rep, "api_calls": calls, "doc_api_calls": calls.get("documents", 0),
            "network_calls": getattr(client, "calls", 0) if client is not None else 0, "fingerprint": fp}
