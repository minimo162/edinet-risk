"""M1 step D: match the items of the current vs previous period for the 7 transport filers, offline.

The document pair per filer is the adopted current / previous version from ``resolve_transport7.json``
(§6-5; SUBARU's previous period is the correction S100YF9D). Items are the leaves in
``segments_transport7.json``; a version missing there (not part of the M0 texts) is extracted from the
cached type=1 ZIP and segmented on the spot (no network). Writes ``match_summary.md``,
``matches_transport7.json`` and, if the documents are present, the ``matches`` table (§5).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .config import Thresholds, load_thresholds
from .m1_resolve import TextSource
from .match.matcher import LABELS, MATCHER_VER, MatchResult, match
from .segment.segmenter import segment
from .store import db

log = logging.getLogger(__name__)


def _leaves_from_segments(segments_path: Path) -> dict[str, list[dict]]:
    if not Path(segments_path).exists():
        return {}
    dump = json.loads(Path(segments_path).read_text(encoding="utf-8"))
    return {d["docID"]: [it for it in d["items"] if it["is_leaf"]] for d in dump}


def run(resolve_path: Path, segments_path: Path, cache_dir: Path, thresholds_path: Path, out_dir: Path,
        db_path: Path | None = None) -> dict:
    th = load_thresholds(thresholds_path)
    rows = json.loads(Path(resolve_path).read_text(encoding="utf-8"))["rows"]
    leaves = _leaves_from_segments(segments_path)
    texts = TextSource(cache_dir, fetch=False)
    sources: dict[str, str] = {}

    def items_of(doc_id: str) -> list[dict] | None:
        if doc_id in leaves:
            sources[doc_id] = "segments_json"
            return leaves[doc_id]
        text = texts(doc_id)
        if text is None:
            return None
        sources[doc_id] = "cache_zip"
        leaves[doc_id] = [asdict(it) for it in segment(text).leaves]
        return leaves[doc_id]

    results = []
    for r in rows:
        cur_id, prev_id = r.get("cur_doc_id"), r.get("prev_doc_id")
        rec = {"label": r["label"], "cur_doc": cur_id, "prev_doc": prev_id, "prev_kind": r.get("prev_kind"),
               "prev_original": r.get("prev_original"), "skip": None, "result": None}
        if not cur_id or not prev_id:
            rec["skip"] = f"no pair (no_prev_reason={r.get('no_prev_reason')})"
        else:
            cur, prev = items_of(cur_id), items_of(prev_id)
            if cur is None or prev is None:
                rec["skip"] = "text missing: " + "; ".join(f"{k}: {v}" for k, v in texts.missing.items()
                                                           if k in (cur_id, prev_id))
            else:
                rec["result"] = match(cur, prev, th)
        results.append(rec)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = []
    for rec in results:
        m: MatchResult | None = rec["result"]
        dump.append({k: rec[k] for k in ("label", "cur_doc", "prev_doc", "prev_kind", "skip")} | (
            {"matcher_ver": m.matcher_ver, "thresholds_version": m.thresholds_version,
             "input_sha256": m.input_sha256, "pairs": [p.to_row() for p in m.pairs]} if m else {}))
    (out_dir / "matches_transport7.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1) + "\n",
                                                    encoding="utf-8")
    write_md(out_dir / "match_summary.md", results, leaves, sources, th)
    saved = save_db(db_path, results) if db_path else 0
    return {"results": results, "sources": sources, "saved_rows": saved, "thresholds": th}


def save_db(db_path: Path, results: list[dict]) -> int:
    """Replace the ``matches`` rows of each document pair (needs both docs in ``documents``)."""
    if not Path(db_path).exists():
        log.warning("db %s not found; matches not saved", db_path)
        return 0
    conn = db.open_db(db_path)
    n = 0
    try:
        for rec in results:
            m: MatchResult | None = rec["result"]
            if m is None:
                continue
            known = {x[0] for x in conn.execute("SELECT doc_id FROM documents WHERE doc_id IN (?, ?)",
                                                (rec["cur_doc"], rec["prev_doc"]))}
            if known != {rec["cur_doc"], rec["prev_doc"]}:
                log.warning("%s: documents row missing for %s; matches not saved", rec["label"],
                            {rec["cur_doc"], rec["prev_doc"]} - known)
                continue
            with db.transaction(conn):
                conn.execute("DELETE FROM matches WHERE cur_doc = ? AND prev_doc = ? AND matcher_ver = ?",
                             (rec["cur_doc"], rec["prev_doc"], m.matcher_ver))
                for k, p in enumerate(m.pairs):
                    db.upsert(conn, "matches", {
                        "cur_doc": rec["cur_doc"], "prev_doc": rec["prev_doc"], "matcher_ver": m.matcher_ver,
                        "pair_idx": k, "input_sha256": m.input_sha256,
                        "cur_items": json.dumps(p.cur_items), "prev_items": json.dumps(p.prev_items),
                        "label": p.label, "stage": p.stage, "score_s": p.score_s, "body_sim": p.body_sim,
                        "head_sim": p.head_sim, "review_flag": int(p.review_flag), "renumbered": int(p.renumbered),
                    }, key=("cur_doc", "prev_doc", "matcher_ver", "pair_idx"))
                    n += 1
    finally:
        conn.close()
    return n


def _cell(s: str) -> str:
    return s.replace("|", "\\|")


def _f(x: float | None) -> str:
    return "-" if x is None else f"{x:.3f}"


def write_md(path: Path, results: list[dict], leaves: dict[str, list[dict]], sources: dict[str, str],
             th: Thresholds) -> None:
    L = [f"# M1 対応付けの結果（輸送用機器7社、当期 2026年3月期 × 前期 2025年3月期、`{MATCHER_VER}`）", "",
         "入力: 採用版・前期の docID は `out/m1/resolve_transport7.json`（§6-5）、項目は最下位の見出し"
         "（`out/m1/segments_transport7.json`。そこに無い版は cache の type=1 ZIP から抽出・分割、オフライン）。",
         "方式: §6-6 の二段方式（アンカー → ハンガリアン法 → 包含率で分割・統合 → 新設・削除 → LISで移動）。"
         "ラベルの優先順は 移動 > 改題 > 修正 > 一致（1対1）。番号変更は別列。",
         "正解（手作業Excel）による評価は M1 の対象外のため、この表は適合率・再現率の評価ではなく分布の確認用。", "",
         f"閾値: `config/thresholds.toml` version=`{th.version}` fingerprint=`{th.fingerprint()}` — "
         f"anchor 本文類似≥{th.anchor_body_sim_min}、S={th.weight_body}×本文+{th.weight_head}×見出し、"
         f"対応 S≥{th.accept_min}、要確認 {th.review_min}≤S<{th.accept_min}、包含率≥{th.contain_min}、"
         f"文字{th.ngram_min}〜{th.ngram_max}gram TF-IDF。", "",
         "## ラベル分布", "",
         "| 会社 | 当期 | 前期（版） | 当期項目 | 前期項目 | " + " | ".join(LABELS)
         + " | 要確認 | 番号変更 | anchor/assign/contain |",
         "|---|---|---|---|---|" + "---|" * len(LABELS) + "---|---|---|"]
    tot_lab: Counter = Counter()
    tot_stage: Counter = Counter()
    tot = Counter()
    for rec in results:
        m: MatchResult | None = rec["result"]
        prev = f"{rec['prev_doc']}（{rec['prev_kind']}）" if rec["prev_kind"] != "original" else rec["prev_doc"]
        if m is None:
            L.append(f"| {rec['label']} | {rec['cur_doc']} | {prev} | - | - | " + " | ".join("-" for _ in LABELS)
                     + f" | - | - | {rec['skip']} |")
            continue
        lab, st = m.label_counts(), m.stage_counts()
        rv, rn = sum(p.review_flag for p in m.pairs), sum(p.renumbered for p in m.pairs)
        tot_lab.update(lab)
        tot_stage.update(st)
        tot.update({"cur": m.n_cur, "prev": m.n_prev, "review": rv, "renum": rn})
        L.append(f"| {rec['label']} | {rec['cur_doc']} | {prev} | {m.n_cur} | {m.n_prev} | "
                 + " | ".join(str(lab.get(k, 0)) for k in LABELS)
                 + f" | {rv} | {rn} | {st.get('anchor', 0)}/{st.get('assign', 0)}/{st.get('contain', 0)} |")
    L.append(f"| **計** | | | {tot['cur']} | {tot['prev']} | " + " | ".join(str(tot_lab.get(k, 0)) for k in LABELS)
             + f" | {tot['review']} | {tot['renum']} | {tot_stage.get('anchor', 0)}/{tot_stage.get('assign', 0)}"
               f"/{tot_stage.get('contain', 0)} |")

    L += ["", "## 一致・修正以外のペアと要確認（自動検出）", ""]
    notes = []
    for rec in results:
        m = rec["result"]
        if m is None:
            notes.append(f"- {rec['label']}: 対応付けなし（{rec['skip']}）")
            continue
        hc = {it["idx"]: it["heading_raw"] for it in leaves[rec["cur_doc"]]}
        hp = {it["idx"]: it["heading_raw"] for it in leaves[rec["prev_doc"]]}
        for p in m.pairs:
            if p.label in ("same", "modified") and not p.review_flag:
                continue
            prev_h = " ＋ ".join(hp[k] for k in p.prev_items) or "（なし）"
            cur_h = " ＋ ".join(hc[k] for k in p.cur_items) or "（なし）"
            extra = f"（{p.stage}、S={_f(p.score_s)}、本文={_f(p.body_sim)}、見出し={_f(p.head_sim)}" \
                    f"{'、要確認' if p.review_flag else ''}）" if p.stage else ""
            notes.append(f"- {rec['label']}: **{p.label}** 「{_cell(prev_h)}」→「{_cell(cur_h)}」{extra}")
    L += notes or ["- なし"]

    L += ["", "## ペアの一覧（会社別、当期の順。削除は前期で直前の項目の対応先の後ろ）", ""]
    for rec in results:
        m = rec["result"]
        if m is None:
            continue
        src = ", ".join(f"{d}={sources.get(d, '?')}" for d in (rec["cur_doc"], rec["prev_doc"]))
        L += [f"### {rec['label']}（当期 {rec['cur_doc']} × 前期 {rec['prev_doc']}）", "",
              f"項目の出所: {src}。input_sha256=`{m.input_sha256[:16]}`", "",
              "| # | ラベル | 段階 | S | 本文類似 | 見出し類似 | 要確認 | 番号変更 | 前期の項目 | 当期の項目 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        hc = {it["idx"]: it["heading_raw"] for it in leaves[rec["cur_doc"]]}
        hp = {it["idx"]: it["heading_raw"] for it in leaves[rec["prev_doc"]]}
        for k, p in enumerate(m.pairs):
            L.append(f"| {k} | {p.label} | {p.stage or '-'} | {_f(p.score_s)} | {_f(p.body_sim)} | "
                     f"{_f(p.head_sim)} | {'○' if p.review_flag else ''} | {'○' if p.renumbered else ''} | "
                     f"{_cell(' ＋ '.join(hp[i] for i in p.prev_items) or '-')} | "
                     f"{_cell(' ＋ '.join(hc[i] for i in p.cur_items) or '-')} |")
        L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
