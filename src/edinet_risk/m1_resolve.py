"""M1: adopted version + previous period for the 7 transport filers, from saved doclists (plan §4 M1).

Since M2 this is a thin wrapper over ``resolve.runner`` (the full-population ``resolve`` command) with the
targets file as the target set; the outputs (resolve_transport7.json / .md) are unchanged.

docIDs are not hard-coded: cached ``documents.json`` day files -> doclist_raw / documents ->
resolve.versions / resolve.period. BusinessRisks texts (for correction_partial and
risk_changed_by_correction) come from ``cache/docs/{docID}_type1.zip``; a missing ZIP is fetched
only with ``fetch=True`` and an API key, otherwise the text counts as unavailable.
"""
from __future__ import annotations

import json
import logging
import uuid
import zipfile
from datetime import date
from pathlib import Path

from .config import Resolve, Window, api_key
from .extract import extract_from_xbrl_zip, html_to_text
from .ingest import doclist
from .m0_multi import load_targets
from .resolve import runner
from .resolve.reasons import Target
from .resolve.versions import PROCESSOR_VERSION, FilingFilter, norm_for_compare
from .store import db

log = logging.getLogger(__name__)


class TextSource:
    """docID -> BusinessRisks text (as extracted, not normalised); remembers why a text is missing."""

    def __init__(self, cache_dir: Path, *, fetch: bool = False, client=None):
        self.cache_dir, self.fetch = Path(cache_dir), fetch
        self.missing: dict[str, str] = {}
        self.fetched: list[str] = []
        self._memo: dict[str, str | None] = {}
        self._client = client   # e.g. an EdinetClient with an api_calls audit hook (``run``)

    def zip_path(self, doc_id: str) -> Path:
        return self.cache_dir / "docs" / f"{doc_id}_type1.zip"

    def _ensure(self, doc_id: str) -> Path | None:
        p = self.zip_path(doc_id)
        if p.exists():
            return p
        if not self.fetch:
            self.missing[doc_id] = "type=1 not in cache (offline)"
            return None
        if self._client is None and not api_key():
            self.missing[doc_id] = "type=1 not in cache and EDINET_API_KEY is not set"
            return None
        from .net.edinet_api import EdinetClient, EdinetError
        try:
            self._client = self._client or EdinetClient(cache_dir=self.cache_dir)
            p = self._client.get_document(doc_id, 1)
        except EdinetError as e:  # message is key-masked
            self.missing[doc_id] = f"fetch failed: {e}"
            return None
        self.fetched.append(doc_id)
        return p

    def __call__(self, doc_id: str) -> str | None:
        if doc_id not in self._memo:
            p = self._ensure(doc_id)
            text = None
            if p is not None:
                try:
                    text, _ = html_to_text(extract_from_xbrl_zip(p).html)
                except (ValueError, zipfile.BadZipFile) as e:
                    self.missing[doc_id] = f"no TextBlock: {e}"
            self._memo[doc_id] = text
        return self._memo[doc_id]


def run(targets_path: Path, cache_dir: Path, filter_path: Path, out_dir: Path, db_path: Path, *,
        as_of: date, window: Window, cfg: Resolve | None = None, fetch: bool = False,
        run_id: str | None = None, client=None) -> dict:
    """With ``run_id`` the caller owns the ``runs`` row (``run``); otherwise one is started here."""
    cfg = cfg or Resolve()
    filers, _ = load_targets(targets_path)
    conn = db.open_db(db_path)
    own_run = run_id is None
    try:
        stats = doclist.import_cache(conn, cache_dir)
        if own_run:
            run_id = f"m1-resolve-{uuid.uuid4().hex[:8]}"
            db.start_run(conn, run_id, "m1_resolve", code_version=PROCESSOR_VERSION, as_of=as_of.isoformat(),
                         args={"targets": str(targets_path), "fetch": fetch})
        texts = TextSource(cache_dir, fetch=fetch, client=client)
        targets = [Target(f["label"], f["edinetCode"], f["label"], f"targets:{Path(targets_path).name}")
                   for f in filers]
        out = runner.resolve_targets(conn, targets, run_id=run_id, as_of=as_of, window=window, cfg=cfg,
                                     flt=FilingFilter.from_toml(filter_path), cache_dir=cache_dir,
                                     text_of=texts, limit_records=True)
        by_key = {t.key: res for t, res in out.results}
        results = [(f, by_key[f["label"]]) for f in filers if f["label"] in by_key]
        if own_run:
            db.finish_run(conn, run_id, "ok")
    finally:
        conn.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [_row(f, res) for f, res in results]
    (out_dir / "resolve_transport7.json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "rows": rows, "text_missing": texts.missing},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_md(out_dir / "resolve_transport7.md", rows, as_of, stats)
    return {"run_id": run_id, "rows": rows, "results": results, "texts": texts, "stats": stats}


def _row(f: dict, res) -> dict:
    cur, prev = res.cur, res.prev
    return {"label": f["label"], "edinetCode": f["edinetCode"],
            "cur_doc_id": cur.doc_id if cur else None, "cur_original": cur.original_doc_id if cur else None,
            "cur_period_end": cur.period_end if cur else None, "cur_kind": cur.version_kind if cur else None,
            "cur_flags": cur.flags if cur else [],
            "prev_doc_id": prev.doc_id if prev else None, "prev_original": prev.original_doc_id if prev else None,
            "prev_period_end": prev.period_end if prev else None, "prev_kind": prev.version_kind if prev else None,
            "prev_flags": (prev.flags if prev else []) + res.flags, "no_prev_reason": res.no_prev_reason,
            "gap_days": res.basis.get("gap_days"),
            "versions": {"cur": cur.basis.get("versions") if cur else None,
                         "prev": prev.basis.get("versions") if prev else None}}


def _write_md(path: Path, rows: list[dict], as_of: date, stats: dict) -> None:
    lines = [f"# M1 版選択・前期特定（輸送用機器7社、基準日 T={as_of}）", "",
             f"保存済み doclist {stats['files']} 日分（{stats['records']} 件）から `resolve.versions`/`resolve.period` で選択。"
             "docID は手で固定していない。", "",
             "| 会社 | 当期 docID | 種類 | periodEnd | 版 | flags | 前期 docID | 種類 | periodEnd | 版 | flags | gap | 前期なし理由 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {label} | {cur_doc_id} | {cur_kind} | {cur_period_end} | {cv} | {cf} | {prev_doc_id} | "
                     "{prev_kind} | {prev_period_end} | {pv} | {pf} | {gap_days} | {npr} |".format(
                         **r, cv=", ".join(r["versions"]["cur"] or []), cf=", ".join(r["cur_flags"]) or "-",
                         pv=", ".join(r["versions"]["prev"] or []), pf=", ".join(r["prev_flags"]) or "-",
                         npr=r["no_prev_reason"] or "-"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_correction_note(path: Path, result: dict, *, label: str = "SUBARU", extra: list[str] = ()) -> dict:
    """Original vs adopted correction (risk_changed_by_correction) for one filer of ``run()``."""
    texts: TextSource = result["texts"]
    f, res = next((f, r) for f, r in result["results"] if f["label"] == label)
    sels = [s for s in (res.cur, res.prev) if s and len(s.basis.get("versions") or []) > 1]
    lines = [f"# {label} 訂正報告書の確認（risk_changed_by_correction）", "",
             f"対象: {f['label']}（{f['edinetCode']}）。版グループは保存済み doclist から `resolve.versions` で構成。",
             "本文は type=1 ZIP の `jpcrp_cor:BusinessRisksTextBlock` を `html_to_text` で変換し、"
             "比較時のみ NFKC＋空白除去で正規化して比較。", ""]
    summary = {}
    for s in sels:
        orig, cor_ids = s.original_doc_id, [d for d in s.basis["versions"] if d != s.original_doc_id]
        lines.append(f"## periodEnd {s.period_end}")
        lines.append(f"- 版: {', '.join(s.basis['versions'])}（原本 {orig}）／採用版 **{s.doc_id}**（{s.version_kind}）")
        lines.append(f"- flags: {', '.join(s.flags) or 'なし'}")
        t0 = texts(orig)
        for cid in cor_ids:
            t1 = texts(cid)
            if t0 is None or t1 is None:
                why = texts.missing.get(cid) or texts.missing.get(orig) or "text unavailable"
                lines.append(f"- {cid}: **比較不可** — {why}")
                summary[cid] = {"compared": False, "reason": why}
                continue
            n0, n1 = norm_for_compare(t0), norm_for_compare(t1)
            same = n0 == n1
            lines.append(f"- {cid}: 本文 原本 {len(t0)} 字 / 訂正 {len(t1)} 字（正規化後 {len(n0)} / {len(n1)}）、"
                         f"原文一致={t0 == t1}、正規化後一致={same} → "
                         f"**risk_changed_by_correction = {not same}**")
            summary[cid] = {"compared": True, "risk_changed_by_correction": not same,
                            "chars": [len(t0), len(t1)], "raw_equal": t0 == t1}
        lines.append("")
    if texts.fetched:
        lines.append(f"取得（type=1、今回の実行で API 呼び出し）: {', '.join(texts.fetched)}。キーはログ・出力に含めていない。")
    lines.extend(extra)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
