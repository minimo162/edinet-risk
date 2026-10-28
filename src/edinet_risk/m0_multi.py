"""M0 Step 2: BusinessRisks for several filers x several fiscal periods (resumable, cache-first).

Filers are matched by edinetCode; filings by periodEnd (not by submit year). Day lists already in
the cache are scanned first (free); uncached days in each window are fetched only until every
target filer has been found for that period.
"""
from __future__ import annotations

import json
import logging
import re
import tomllib
from collections import Counter
from datetime import date
from pathlib import Path

from .client import EdinetClient, EdinetError
from .extract import extract_from_xbrl_zip
from .m0 import daterange, load_filter, matches_filter, process_doc

log = logging.getLogger(__name__)

# Line-level markers that indicate list structure in the extracted text.
_BULLET_RE = re.compile(r"^[・●○■□◆◇▪•※]")
_NUMBERED_RE = re.compile(r"^(?:[（(][0-9０-９一二三四五六七八九十]+[）)]|[①-⑳]|[0-9０-９]+[.．、]|[a-zａ-ｚA-ZＡ-Ｚ][.．)）])")


def load_targets(path: Path) -> tuple[list[dict], list[dict]]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    return cfg["filer"], cfg["period"]


def structure_stats(html: str, text: str) -> dict:
    tags = Counter(t.lower() for t in re.findall(r"<\s*([a-zA-Z][a-zA-Z0-9]*)", html))
    lines = text.splitlines()
    return {
        "html_tables": tags["table"], "html_tr": tags["tr"], "html_td_th": tags["td"] + tags["th"],
        "html_ul_ol": tags["ul"] + tags["ol"], "html_li": tags["li"], "html_img": tags["img"],
        "html_rowspan_colspan": len(re.findall(r"\b(?:rowspan|colspan)\s*=\s*[\"']?(?!1\b)\d", html, re.I)),
        "html_img_without_alt": sum(1 for m in re.finditer(r"<img\b[^>]*>", html, re.I)
                                    if not re.search(r"\balt\s*=\s*[\"'][^\"']+[\"']", m.group(0), re.I)),
        "text_lines": len(lines),
        "text_tab_rows": sum(1 for ln in lines if "\t" in ln),
        "text_bullet_lines": sum(1 for ln in lines if _BULLET_RE.match(ln)),
        "text_numbered_lines": sum(1 for ln in lines if _NUMBERED_RE.match(ln)),
        "bullet_marks": dict(Counter(ln[0] for ln in lines if _BULLET_RE.match(ln))),
    }


def find_filings(client: EdinetClient, filers: list[dict], period: dict, flt: dict) -> tuple[dict, dict, list[str]]:
    """Return ({edinetCode: [records]}, {edinetCode: [correction records]}, scanned_days)."""
    pe = period["periodEnd"]
    days = [d.isoformat() for d in daterange(date.fromisoformat(period["scan_from"]),
                                             date.fromisoformat(period["scan_to"]))]
    cached = [d for d in days if client.doclist_path(d).exists()]
    order = cached + [d for d in days if d not in cached]
    codes = {f["edinetCode"] for f in filers}
    hits: dict[str, list[dict]] = {c: [] for c in codes}
    corrections: dict[str, list[dict]] = {c: [] for c in codes}
    scanned = []
    for ds in order:
        if all(hits[c] for c in codes):
            break
        obj = client.list_documents(ds)
        scanned.append(ds)
        for r in obj.get("results", []):
            c = r.get("edinetCode")
            if c not in codes or r.get("withdrawalStatus") not in (None, "0"):
                continue
            if r.get("docTypeCode") == "130":
                corrections[c].append(r)
            if matches_filter(r, flt) and r.get("periodEnd") == pe:
                hits[c].append(r)
        log.info("period %s: scanned %s; found %d/%d filers", pe, ds, sum(1 for c in codes if hits[c]), len(codes))
    return hits, corrections, scanned


def run(targets_path: Path, out_dir: Path, cache_dir: Path, filter_path: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    flt = load_filter(filter_path)
    filers, periods = load_targets(targets_path)
    client = EdinetClient(cache_dir=cache_dir)
    rows = []
    all_corrections = []
    for period in periods:
        hits, corrections, scanned = find_filings(client, filers, period, flt)
        for c, recs in corrections.items():
            all_corrections += recs
        for f in filers:
            recs = hits[f["edinetCode"]]
            row = {"label": f["label"], "edinetCode": f["edinetCode"], "period": period["periodEnd"],
                   "docID": None, "periodEnd": None, "chars": None, "status": None, "structure": None,
                   "filerName": None, "submitDateTime": None, "csv_len": None}
            if not recs:
                row["status"] = f"NOT FOUND (scanned {len(scanned)} day(s) {period['scan_from']}..{period['scan_to']})"
                rows.append(row)
                continue
            if len(recs) > 1:  # keep the latest submission, record ambiguity
                recs = sorted(recs, key=lambda r: r.get("submitDateTime") or "")
                log.warning("%s %s: %d matching filings %s; using latest", f["label"], period["periodEnd"],
                            len(recs), [r["docID"] for r in recs])
            rec = recs[-1]
            row.update(docID=rec["docID"], periodEnd=rec.get("periodEnd"), filerName=rec.get("filerName"),
                       submitDateTime=rec.get("submitDateTime"))
            try:
                meta = process_doc(client, rec, out_dir)
            except (EdinetError, ValueError, OSError) as e:  # EdinetError messages are key-masked
                row["status"] = f"FAILED: {type(e).__name__}: {e}"
                rows.append(row)
                continue
            text = (out_dir / f"{rec['docID']}_business_risks.txt").read_text(encoding="utf-8")
            html = extract_from_xbrl_zip(client.document_path(rec["docID"], 1)).html
            st = structure_stats(html, text)
            meta["structure"] = st
            (out_dir / f"{rec['docID']}_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                              encoding="utf-8")
            row.update(chars=meta["char_count"], structure=st, csv_len=meta["csv"].get("csv_len"),
                       status="OK" + (f" ({len(recs)} candidates, latest used)" if len(recs) > 1 else ""))
            rows.append(row)
    write_summary(out_dir, rows, all_corrections)
    write_structure_note(out_dir, rows)
    return {"rows": rows, "network_calls": client.calls, "corrections": all_corrections}


def write_summary(out: Path, rows: list[dict], corrections: list[dict]):
    L = ["# 輸送用機器7社 × 2期 BusinessRisksTextBlock 取得結果（M0 Step 2）", "",
         "- 絞り込み: docTypeCode=120, ordinanceCode=010, formCode=030000, xbrlFlag=1, 取下げなし。"
         "提出者は edinetCode、期は periodEnd で照合（config/transport7.toml）。",
         "- chars = 表題行除去後のテキスト文字数（`len()`、Unicode コードポイント数）。csv_len = type=5 CSV 値の文字数。",
         "- 本文: `out/m0/<docID>_business_risks.txt`、メタ: `out/m0/<docID>_meta.json`。", "",
         "| 会社 | edinetCode | 対象期 | docID | periodEnd | 提出日時 | chars | csv_len | status |",
         "|---|---|---|---|---|---|---:|---:|---|"]
    for r in rows:
        L.append(f"| {r['label']} | {r['edinetCode']} | {r['period']} | {r['docID'] or '-'} | {r['periodEnd'] or '-'} "
                 f"| {r['submitDateTime'] or '-'} | {r['chars'] if r['chars'] is not None else '-'} "
                 f"| {r['csv_len'] if r['csv_len'] is not None else '-'} | {r['status']} |")
    ok = [r for r in rows if r["status"].startswith("OK")]
    L += ["", f"成功 {len(ok)}/{len(rows)}。"]
    if ok:
        cs = [r["chars"] for r in ok]
        L.append(f"文字数レンジ: {min(cs):,}〜{max(cs):,}（30,000 字超: {sum(1 for c in cs if c > 30000)} 件）。")
    if corrections:
        L += ["", "## 走査範囲内で見つかった訂正報告書（docTypeCode 130、本表には未反映）", "",
              "| docID | 提出者 | formCode | parentDocID | 提出日時 | 書類名 |", "|---|---|---|---|---|---|"]
        for c in sorted(corrections, key=lambda r: r.get("submitDateTime") or ""):
            L.append(f"| {c['docID']} | {c.get('filerName')} | {c.get('formCode')} | {c.get('parentDocID') or '-'} "
                     f"| {c.get('submitDateTime')} | {c.get('docDescription')} |")
    corrected = {c.get("parentDocID") for c in corrections}
    hit = [r for r in rows if r["docID"] in corrected]
    if hit:
        L += ["", "**注意**: " + ", ".join(f"{r['label']} {r['docID']}" for r in hit)
              + " には訂正報告書がある。訂正が事業等のリスクに及ぶかは未確認"
              "（M2 の訂正有報の扱い〔H2〕で、訂正後本文への差し替え要否を判断）。"]
    (out / "transport7_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def write_structure_note(out: Path, rows: list[dict]):
    ok = [r for r in rows if r["structure"]]
    L = ["# BusinessRisks 内の表・箇条書き構造と構造保持に関するメモ（M0 Step 2）", "",
         "集計対象: transport7_summary.md の取得成功分。HTML 列は XBRL 要素値（エスケープ解除後の HTML）のタグ数、"
         "テキスト列は `html_to_text` 出力の行数。",
         "bullet = 行頭が ・●○■□◆◇▪•※、numbered = 行頭が (1)/（１）/①/1./a) 等。", "",
         "| 会社 | 期 | docID | table | tr | 結合セル | ul/ol | li | img(alt無) | テキスト行 | TAB行(表の行) | bullet行 | numbered行 | bullet記号 |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in ok:
        s = r["structure"]
        marks = " ".join(f"{k}{v}" for k, v in s["bullet_marks"].items()) or "-"
        L.append(f"| {r['label']} | {r['period']} | {r['docID']} | {s['html_tables']} | {s['html_tr']} | {s['html_rowspan_colspan']} "
                 f"| {s['html_ul_ol']} | {s['html_li']} | {s['html_img']}({s['html_img_without_alt']}) | {s['text_lines']} | {s['text_tab_rows']} "
                 f"| {s['text_bullet_lines']} | {s['text_numbered_lines']} | {marks} |")
    with_tables = [r for r in ok if r["structure"]["html_tables"]]
    with_ul = [r for r in ok if r["structure"]["html_ul_ol"]]
    with_bullets = [r for r in ok if r["structure"]["text_bullet_lines"]]
    with_img = [r for r in ok if r["structure"]["html_img"]]
    fmt = lambda rs: ", ".join(f"{r['label']} {r['period'][:4]}" for r in rs) or "なし"
    L += ["", "## 所見", "",
          f"- `<table>` を含む: {len(with_tables)}/{len(ok)}（{fmt(with_tables)}）",
          f"- HTML リスト `<ul>/<ol>` を含む: {len(with_ul)}/{len(ok)}（{fmt(with_ul)}）",
          f"- 行頭記号による疑似箇条書きを含む: {len(with_bullets)}/{len(ok)}（{fmt(with_bullets)}）",
          f"- `<img>` を含む: {len(with_img)}/{len(ok)}（{fmt(with_img)}）",
          "", "## 観察（実データ）", "",
          "- 表は SUBARU の両期のみ（気候変動の移行/物理リスク・機会の表 2 個、計 11 行）。`rowspan` による結合セルがあり、"
          "`html_to_text` の TAB 区切りでは結合セルを持たない行の列が左にずれる（例: 1 行目は 4 列、続く行は 2〜3 列）。"
          "縦書き風のセル「移 行 リ ス ク」も文字間に空白が入る。",
          "- `<ul>/<ol>/<li>` は 14 件中 0 件。箇条書きはすべて `<p>` 内の行頭記号（日産・マツダは「・」、SUBARU は「※」注記）で、"
          "テキストに残るため行単位で保持されている。",
          "- リスク項目の見出しは (1)/① 等の番号付き段落で、全社にある（numbered 行 17〜28）。項目分割の手がかりになる。",
          "- `<img>` は SUBARU・スズキの両期に各 1 個で、**alt が空**。現行の `html_to_text` は alt があるときだけ `[画像: alt]` を出すため、"
          "画像（リスクマップ等の図とみられる）は**跡を残さず消えている**。",
          "- 同じ SUBARU の表でも 2025 年版は「浸水によ る」「場合、 ＳＵＢＡＲＵ」のように、原文 HTML の改行由来の空白が和文中に入る。"
          "年度間で文言が同じでも空白だけの差分が出る。",
          "", "## 構造保持への対応案（M1 以降）", "",
          "- 段落（ブロック要素）= 1 行の現行方式を維持する。箇条書きは行頭記号がテキストに残るので追加処理は不要。",
          "- 表: 差分では TAB 行を「表の行」として本文段落と分け、行テキストの集合として比較する。"
          "rowspan を展開して列を揃える（結合セルの値を下の行に複製する）処理を `html_to_text` に入れるかどうかは M1 で判断する"
          "（7 社では SUBARU だけなので優先度は中）。",
          "- 画像: alt が空でも `[画像]` プレースホルダを出力し、図が存在したことを残す（差分で図の追加・削除を検知するため）。",
          "- 比較用の正規化: NFKC に加え、和文文字間の空白を除去してから差分を取る（保存テキスト自体は原文寄りのまま）。",
          ]
    (out / "structure_note.md").write_text("\n".join(L) + "\n", encoding="utf-8")
