"""M1 step F: ``compare`` — extracted text -> segment -> match -> diff for the 7 transport filers, offline.

The document pair per filer is the adopted current / previous version in ``resolve_transport7.json`` (§6-5).
Texts are the M0 extracts ``out/m0/{docID}_business_risks.txt``; a version missing there (SUBARU's
correction S100YF9D) is extracted from the cached type=1 ZIP (``m1_resolve.TextSource``, no network).

Writes ``{out}/run_{run_id}/compare.json``: per filer the leaf items of both sides, the match pairs and,
per pair, the sentence blocks of the character diff (``diff.chardiff``) plus the sentence-level change
counts (``diff.sentences``). ``report`` renders it; it holds no timestamps, so the same input gives the same
file apart from ``run_id``.

With ``db_path`` (an existing database whose ``documents`` holds both docIDs, e.g. ``out/m1/m1.sqlite`` from
``m1_resolve``) the same results also go to SQLite (§5): ``extracts`` (when the type=1 ZIP is cached),
``items`` (every heading node; leaves are the items whose next item is not deeper), ``matches`` and
``sentence_changes``, one transaction per filer, replacing the rows of the same processor versions.
None of these rows carries a timestamp or run_id, so a re-run writes the same content (M1 acceptance 5).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import load_thresholds
from .diff import chardiff, minor, sentences
from .extract import EXTRACTOR_VER, extract_from_xbrl_zip, html_to_text
from .m1_resolve import TextSource
from .match.matcher import MATCHER_VER, match
from .segment.segmenter import SEGMENTER_VER, segment
from .store import db
from .store.db import JST

log = logging.getLogger(__name__)

COMPARE_VER = "compare-1"


def new_run_id() -> str:
    return datetime.now(JST).strftime("%Y%m%dT%H%M%S")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Texts:
    """docID -> BusinessRisks text: the M0 extract if present, else the cached type=1 ZIP (offline)."""

    def __init__(self, m0_dir: Path, cache_dir: Path):
        self.m0_dir = Path(m0_dir)
        self.zip = TextSource(cache_dir, fetch=False)
        self.source: dict[str, str] = {}

    def __call__(self, doc_id: str) -> str | None:
        p = self.m0_dir / f"{doc_id}_business_risks.txt"
        if p.exists():
            self.source[doc_id] = "m0_txt"
            return p.read_text(encoding="utf-8")
        text = self.zip(doc_id)
        if text is not None:
            self.source[doc_id] = "cache_zip"
        return text

    def missing(self, doc_id: str) -> str:
        return self.zip.missing.get(doc_id, "text not found")


def _side(doc_id: str, text: str, period_end: str | None, source: str, seg=None) -> dict:
    seg = seg or segment(text)
    return {"doc_id": doc_id, "period_end": period_end, "text_sha256": _sha(text), "chars": len(text),
            "source": source, "segmentation": seg.segmentation, "preamble_chars": len(seg.preamble),
            "lossless": seg.preamble + "".join(text[it.start_off:it.end_off] for it in seg.items) == text
            if seg.items else seg.preamble == text,
            "items": [asdict(it) for it in seg.leaves]}


def _seg_row(s) -> list:
    return [s.op, s.text]


def _sent(s) -> dict | None:
    return None if s is None else {"text": s.text, "start": s.start, "end": s.end}


def _cmp_text(items: list[dict]) -> str:
    return "\n".join(it["heading_norm"] + "\n" + it["body"] for it in items)


def diff_pair(pair_idx: int, p, cur_side: dict, prev_side: dict, cur_text: str, prev_text: str,
              sink: list | None = None) -> dict:
    """One pair of compare.json; ``sink`` (if given) collects its ``sentence_changes`` rows."""
    by_c = {it["idx"]: it for it in cur_side["items"]}
    by_p = {it["idx"]: it for it in prev_side["items"]}
    ci, pi = [by_c[i] for i in p.cur_items], [by_p[i] for i in p.prev_items]
    cs, ps = sentences.item_sentences(cur_text, ci), sentences.item_sentences(prev_text, pi)
    rows = sentences.align(ps, cs)
    bl = chardiff.blocks(ps, cs, rows)
    ch = sentences.changes(ps, cs, cur_doc=cur_side["doc_id"], prev_doc=prev_side["doc_id"], rows=rows)
    if sink is not None:
        sink.extend(c.to_row(cur_side["doc_id"], prev_side["doc_id"], pair_idx) for c in ch)
    cur_src = "".join(cur_text[it["start_off"]:it["end_off"]] for it in ci)
    prev_src = "".join(prev_text[it["start_off"]:it["end_off"]] for it in pi)
    # 変更あり／なし: normalised heading (numbering removed; renumbering is its own column) + body
    kind = minor.classify(_cmp_text(pi), _cmp_text(ci)) if ci and pi else None
    last = {"prev": None, "cur": None}

    def gap(side: str, s, text: str) -> str:
        """Whitespace between this sentence and the previous one on the same side (keeps the line breaks)."""
        if s is None:
            return ""
        g = "" if last[side] is None else text[last[side]:s.start]
        last[side] = s.end
        return g if not g.strip() else "\n"   # non-adjacent items of a 1:n pair: just a line break

    out_blocks = [{"op": b.op, "minor_kind": b.minor_kind, "prev": _sent(b.prev), "cur": _sent(b.cur),
                   "gap_prev": gap("prev", b.prev, prev_text), "gap_cur": gap("cur", b.cur, cur_text),
                   "segments": [_seg_row(s) for s in b.segments]} for b in bl]
    return {"pair_idx": pair_idx, **p.to_row(),
            "change": "none" if kind == "identical" else ("minor" if kind else "changed"),
            "minor_kind": kind if kind not in (None, "identical") else None,
            "cur_src": cur_src, "prev_src": prev_src,
            "blocks": out_blocks,
            "n_changes": len(ch), "n_changes_llm": len(sentences.for_llm(ch))}


def run(resolve_path: Path, m0_dir: Path, cache_dir: Path, thresholds_path: Path, out_dir: Path,
        run_id: str | None = None, db_path: Path | None = None, *, own_run: bool = True) -> dict:
    """``own_run=False``: the caller (``run``) owns the ``runs`` row of ``run_id``."""
    run_id = run_id or new_run_id()
    th = load_thresholds(thresholds_path)
    resolved = json.loads(Path(resolve_path).read_text(encoding="utf-8"))
    texts = Texts(m0_dir, cache_dir)
    filers, work = [], []
    for r in resolved["rows"]:
        cur_id, prev_id = r.get("cur_doc_id"), r.get("prev_doc_id")
        rec = {"label": r["label"], "edinetCode": r.get("edinetCode"), "cur_doc": cur_id, "prev_doc": prev_id,
               "cur_kind": r.get("cur_kind"), "prev_kind": r.get("prev_kind"),
               "cur_original": r.get("cur_original"), "prev_original": r.get("prev_original"),
               "flags": sorted(set(r.get("cur_flags", []) + r.get("prev_flags", []))),
               "no_prev_reason": r.get("no_prev_reason"), "skip": None, "cur": None, "prev": None, "pairs": []}
        filers.append(rec)
        if not cur_id or not prev_id:
            rec["skip"] = f"no pair (no_prev_reason={r.get('no_prev_reason')})"
            continue
        ct, pt = texts(cur_id), texts(prev_id)
        if ct is None or pt is None:
            rec["skip"] = "text missing: " + "; ".join(f"{d}: {texts.missing(d)}" for d, t in
                                                       ((cur_id, ct), (prev_id, pt)) if t is None)
            continue
        cseg, pseg = segment(ct), segment(pt)
        rec["cur"] = _side(cur_id, ct, r.get("cur_period_end"), texts.source[cur_id], cseg)
        rec["prev"] = _side(prev_id, pt, r.get("prev_period_end"), texts.source[prev_id], pseg)
        m = match(rec["cur"]["items"], rec["prev"]["items"], th)
        changes: list[dict] = []
        rec |= {"matcher_ver": m.matcher_ver, "input_sha256": m.input_sha256,
                "pairs": [diff_pair(k, p, rec["cur"], rec["prev"], ct, pt, changes)
                          for k, p in enumerate(m.pairs)]}
        work.append({"label": rec["label"], "docs": {cur_id: (ct, cseg, rec["cur"]["source"]),
                                                     prev_id: (pt, pseg, rec["prev"]["source"])},
                     "cur_doc": cur_id, "prev_doc": prev_id, "match": m, "changes": changes})
    data = {"run_id": run_id, "compare_ver": COMPARE_VER, "segmenter_ver": SEGMENTER_VER,
            "matcher_ver": MATCHER_VER, "differ_ver": sentences.DIFFER_VER, "minor_ver": minor.MINOR_VER,
            "thresholds_version": th.version, "thresholds_fingerprint": th.fingerprint(),
            "as_of": resolved.get("as_of"), "resolve_sha256": _sha(Path(resolve_path).read_text(encoding="utf-8")),
            "filers": filers}
    run_dir = Path(out_dir) / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "compare.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    saved = save_db(db_path, data, work, texts.zip, own_run=own_run) if db_path else None
    return {"run_dir": run_dir, "data": data, "db": saved}


def load(run_dir: Path) -> dict:
    return json.loads((Path(run_dir) / "compare.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- SQLite (§5)

def _extract_row(doc_id: str, text: str, source: str, zips: TextSource) -> dict | None:
    """``extracts`` row; needs the cached type=1 ZIP (input_sha256 is the ZIP's hash), else None."""
    p = zips.zip_path(doc_id)
    if not p.exists():
        return None
    tb = extract_from_xbrl_zip(p)
    warnings = [f"text_from={source}"]
    if html_to_text(tb.html)[0] != text:
        warnings.append("text_differs_from_zip_extract")
    return {"doc_id": doc_id, "extractor_ver": EXTRACTOR_VER,
            "input_sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "source": "xbrl",
            "raw_html_sha": _sha(tb.html), "text": text, "char_len": len(text),
            "img_count": tb.html.lower().count("<img"), "hit_count": tb.count, "warnings_json": db.dumps(warnings)}


def _item_rows(doc_id: str, text: str, seg) -> list[dict]:
    return [{"doc_id": doc_id, "segmenter_ver": seg.segmenter_ver, "item_idx": it.idx, "input_sha256": _sha(text),
             "level": it.level, "heading_raw": it.heading_raw, "heading_norm": it.heading_norm, "body": it.body,
             "start_off": it.start_off, "end_off": it.end_off, "segmentation": it.segmentation} for it in seg.items]


def _match_rows(cur_doc: str, prev_doc: str, m) -> list[dict]:
    return [{"cur_doc": cur_doc, "prev_doc": prev_doc, "matcher_ver": m.matcher_ver, "pair_idx": k,
             "input_sha256": m.input_sha256, "cur_items": json.dumps(p.cur_items),
             "prev_items": json.dumps(p.prev_items), "label": p.label, "stage": p.stage, "score_s": p.score_s,
             "body_sim": p.body_sim, "head_sim": p.head_sim, "review_flag": int(p.review_flag),
             "renumbered": int(p.renumbered)} for k, p in enumerate(m.pairs)]


def save_db(db_path: Path, data: dict, work: list[dict], zips: TextSource, *, own_run: bool = True) -> dict | None:
    """Write extracts / items / matches / sentence_changes of each compared filer (needs ``documents`` rows)."""
    if not Path(db_path).exists():
        log.warning("db %s not found; compare results not saved", db_path)
        return None
    counts = {"extracts": 0, "items": 0, "matches": 0, "sentence_changes": 0, "filers": 0, "skipped": []}
    conn = db.open_db(db_path)
    try:
        run_id = data["run_id"]
        if own_run:
            db.upsert(conn, "runs", {
                "run_id": run_id, "started_at": db.now_iso(), "finished_at": None, "cmd": "compare",
                "as_of": data.get("as_of"), "code_version": COMPARE_VER, "status": "running",
                "args_json": db.dumps({k: data[k] for k in (
                    "segmenter_ver", "matcher_ver", "differ_ver", "minor_ver", "thresholds_version",
                    "thresholds_fingerprint", "resolve_sha256")})}, key=["run_id"])
        status = "failed"
        try:
            for w in work:
                docs = list(w["docs"])
                known = {x[0] for x in conn.execute(
                    f"SELECT doc_id FROM documents WHERE doc_id IN ({', '.join('?' for _ in docs)})", docs)}
                if known != set(docs):
                    log.warning("%s: documents row missing for %s; not saved", w["label"], sorted(set(docs) - known))
                    counts["skipped"].append(w["label"])
                    continue
                with db.transaction(conn):
                    for doc_id, (text, seg, source) in w["docs"].items():
                        ex = _extract_row(doc_id, text, source, zips)
                        if ex is not None:
                            db.upsert(conn, "extracts", ex, key=("doc_id", "extractor_ver"))
                            counts["extracts"] += 1
                        conn.execute("DELETE FROM items WHERE doc_id = ? AND segmenter_ver = ?",
                                     (doc_id, seg.segmenter_ver))
                        for row in _item_rows(doc_id, text, seg):
                            db.upsert(conn, "items", row, key=("doc_id", "segmenter_ver", "item_idx"))
                            counts["items"] += 1
                    pair = (w["cur_doc"], w["prev_doc"])
                    conn.execute("DELETE FROM matches WHERE cur_doc = ? AND prev_doc = ? AND matcher_ver = ?",
                                 (*pair, w["match"].matcher_ver))
                    for row in _match_rows(*pair, w["match"]):
                        db.upsert(conn, "matches", row, key=("cur_doc", "prev_doc", "matcher_ver", "pair_idx"))
                        counts["matches"] += 1
                    conn.execute("DELETE FROM sentence_changes WHERE cur_doc = ? AND prev_doc = ? AND differ_ver = ?",
                                 (*pair, sentences.DIFFER_VER))
                    for row in w["changes"]:
                        db.upsert(conn, "sentence_changes", row, key=("change_id",))
                        counts["sentence_changes"] += 1
                counts["filers"] += 1
            status = "ok"
        finally:
            if own_run:
                db.finish_run(conn, run_id, status)
    finally:
        conn.close()
    return counts
