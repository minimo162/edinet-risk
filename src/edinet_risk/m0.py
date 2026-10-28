"""M0 spike: find one company's 有報, extract BusinessRisks, compare with CSV, crosstab form codes."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import tomllib
import unicodedata
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from .client import EdinetClient
from .extract import extract_from_xbrl_zip, find_csv_facts, html_to_text

log = logging.getLogger(__name__)

META_KEYS = ["docID", "edinetCode", "secCode", "filerName", "periodStart", "periodEnd",
             "formCode", "docTypeCode", "ordinanceCode", "submitDateTime"]


def load_filter(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)["annual_report"]


def matches_filter(rec: dict, flt: dict) -> bool:
    return (rec.get("docTypeCode") in flt["docTypeCode"]
            and rec.get("ordinanceCode") in flt["ordinanceCode"]
            and rec.get("formCode") in flt["formCode"]
            and str(rec.get("xbrlFlag")) == str(flt["xbrlFlag"]))


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def scan_for_filer(client: EdinetClient, filer: str, start: date, end: date, flt: dict,
                   crosstab: Counter, examples: dict, scanned: list[str]) -> list[dict]:
    """Scan day lists start..end; stop after the first day with a match."""
    for d in daterange(start, end):
        ds = d.isoformat()
        obj = client.list_documents(ds)
        if ds not in scanned:
            scanned.append(ds)
            for r in obj.get("results", []):
                k = (r.get("docTypeCode"), r.get("ordinanceCode"), r.get("formCode"))
                crosstab[k] += 1
                examples.setdefault(k, (r.get("docID"), r.get("filerName"), r.get("docDescription")))
        hits = [r for r in obj.get("results", [])
                if filer in (r.get("filerName") or "") and matches_filter(r, flt)
                and r.get("withdrawalStatus") in (None, "0")]
        log.info("%s: %d docs, %d hit(s) for %s", ds, len(obj.get("results", [])), len(hits), filer)
        if hits:
            return hits
    return []


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def process_doc(client: EdinetClient, rec: dict, out: Path) -> dict:
    doc_id = rec["docID"]
    zpath = client.get_document(doc_id, 1)
    tb = extract_from_xbrl_zip(zpath)
    text, title_removed = html_to_text(tb.html)
    text_full, _ = html_to_text(tb.html, drop_title=False)
    (out / f"{doc_id}_business_risks.txt").write_text(text, encoding="utf-8")
    meta = {k: rec.get(k) for k in META_KEYS}
    meta.update({
        "char_count": len(text),
        "char_count_with_title": len(text_full),
        "raw_html_char_count": len(tb.html),
        "title_line_removed": title_removed,
        "contextRef": tb.context_ref,
        "xbrl_member": tb.member,
        "zip_sha256": hashlib.sha256(zpath.read_bytes()).hexdigest(),
    })
    # --- CSV (type=5) comparison
    cpath = client.get_document(doc_id, 5)
    facts = find_csv_facts(cpath)
    csv_info = {"facts_found": len(facts)}
    if facts:
        f = facts[0]
        v = f.value
        nv, nt = _norm(v), _norm(text_full)
        csv_text, _ = html_to_text(v, drop_title=False) if "<" in v else (v, False)
        csv_info.update({
            "csv_member": f.member, "csv_context_id": f.context_id, "csv_label": f.label,
            "csv_len": len(v), "csv_len_exactly_30000": len(v) == 30000,
            "csv_contains_html_tags": bool(re.search(r"<[a-zA-Z/][^>]*>", v)),
            "csv_utf8_bytes": len(v.encode("utf-8")), "csv_utf16_units": len(v.encode("utf-16-le")) // 2,
            "csv_head": v[:120], "csv_tail": v[-120:],
            "xbrl_text_tail": text_full[-120:],
            "norm_csv_len": len(nv), "norm_xbrl_text_len": len(nt),
            "norm_common_prefix": _common_prefix(nv, nt),
            "norm_csv_is_prefix_of_xbrl": nt.startswith(nv),
            "norm_equal": nv == nt,
            "csv_as_text_len": len(csv_text),
        })
    meta["csv"] = csv_info
    (out / f"{doc_id}_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def write_crosstab(out: Path, crosstab: Counter, examples: dict, scanned: list[str]):
    rows = sorted(crosstab.items(), key=lambda kv: tuple(x or "" for x in kv[0]))
    with open(out / "formcode_crosstab.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["docTypeCode", "ordinanceCode", "formCode", "count", "example_docID",
                    "example_filerName", "example_docDescription"])
        for (dt, oc, fc), n in rows:
            ex = examples[(dt, oc, fc)]
            w.writerow([dt, oc, fc, n, *ex])
    total = sum(crosstab.values())
    lines = [
        "# docTypeCode × ordinanceCode × formCode クロス集計（M0）", "",
        f"- 走査した提出日: {len(scanned)} 日（{', '.join(scanned)}）",
        f"- 書類数（type=2 の results 全件）: {total}",
        "- 注意: 年度窓1年分ではなく、マツダの有報探索で走査した日付だけの集計（M0-8 の完了には年度窓全体の走査が必要）。",
        "- docTypeCode が空のものは取下げ・不開示等でメタデータが消されたレコード。",
        *[f"- formCode {fc}: " + (", ".join(f"{k[0]}/{k[1]} {crosstab[k]}件 例 {examples[k][0]}"
                                          for k in crosstab if k[2] == fc) or "この走査範囲では出現せず")
          for fc in ("030001", "702001")],
        "",
        "## 有報・訂正有報（docTypeCode 120/130）", "",
        "| docTypeCode | ordinanceCode | formCode | 件数 | 例 docID | 例 提出者 | 例 書類名 |", "|---|---|---|---:|---|---|---|",
    ]
    for (dt, oc, fc), n in rows:
        if dt in ("120", "130"):
            ex = examples[(dt, oc, fc)]
            lines.append(f"| {dt} | {oc} | {fc} | {n} | {ex[0]} | {ex[1]} | {ex[2]} |")
    lines += ["", "## 全組合せ", "", "| docTypeCode | ordinanceCode | formCode | 件数 | 例 docID | 例 書類名 |",
              "|---|---|---|---:|---|---|"]
    for (dt, oc, fc), n in rows:
        ex = examples[(dt, oc, fc)]
        lines.append(f"| {dt} | {oc} | {fc} | {n} | {ex[0]} | {ex[2]} |")
    (out / "formcode_crosstab.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_truncation_note(out: Path, metas: list[dict]):
    L = ["# CSV（type=5）の BusinessRisksTextBlock と XBRL（type=1）抽出の比較", "",
         "文字数は Python の `len()`（Unicode コードポイント数）。`正規化` は NFKC＋空白全除去。", ""]
    for m in metas:
        c = m["csv"]
        L += [f"## {m['filerName']} {m['docID']}（periodEnd {m['periodEnd']}）", ""]
        if not c.get("facts_found"):
            L += ["CSV に jpcrp_cor:BusinessRisksTextBlock が見つからなかった。", ""]
            continue
        diff = m["char_count_with_title"] - c["csv_len"]
        L += [
            "| 項目 | 値 |", "|---|---:|",
            f"| XBRL 生HTML（要素値、エスケープ解除後）の文字数 | {m['raw_html_char_count']:,} |",
            f"| XBRL → テキスト化（表題行あり）の文字数 | {m['char_count_with_title']:,} |",
            f"| XBRL → テキスト化（表題行除去、保存版）の文字数 | {m['char_count']:,} |",
            f"| CSV 値の文字数 | {c['csv_len']:,} |",
            f"| CSV 値 UTF-8 バイト数 | {c['csv_utf8_bytes']:,} |",
            f"| CSV 値が 30,000 字ちょうどか | {'**はい**' if c['csv_len_exactly_30000'] else 'いいえ'} |",
            f"| CSV に HTML タグが残るか | {'はい' if c['csv_contains_html_tags'] else 'いいえ'} |",
            f"| テキスト化（表題あり）− CSV の差 | {diff:,} |",
            f"| 正規化後: XBRL / CSV の文字数 | {c['norm_xbrl_text_len']:,} / {c['norm_csv_len']:,} |",
            f"| 正規化後: 共通接頭辞の長さ | {c['norm_common_prefix']:,} |",
            f"| 正規化後: CSV は XBRL テキストの接頭辞か / 完全一致か | {c['norm_csv_is_prefix_of_xbrl']} / {c['norm_equal']} |",
            f"| contextRef（XBRL）/ コンテキストID（CSV） | {m['contextRef']} / {c['csv_context_id']} |",
            f"| CSV ファイル | `{c['csv_member']}` |", "",
            "CSV 末尾 120 字:", "", "```", c["csv_tail"], "```", "",
            "XBRL テキスト末尾 120 字:", "", "```", c["xbrl_text_tail"].rstrip("\n"), "```", "",
        ]
        if c["csv_len_exactly_30000"]:
            L += ["**判定**: CSV 値は 30,000 字ちょうどで、切り詰めが疑われる（レビュー H1）。"
                  f"XBRL 側は {m['char_count_with_title']:,} 字。", ""]
        elif m["char_count_with_title"] > 30000:
            L += ["**判定**: XBRL 本文は 30,000 字超だが CSV 値は 30,000 字ちょうどではない。条件の再確認が必要。", ""]
        else:
            L += ["**判定**: 本文が 30,000 字以下のため、この書類では切り詰めは発生しない（切り詰め条件の特定には 30,000 字超の書類が必要）。", ""]
    (out / "csv_xbrl_compare.md").write_text("\n".join(L), encoding="utf-8")


def run(filer: str, years: list[int], out_dir: Path, cache_dir: Path, filter_path: Path,
        window: tuple[int, int, int, int] = (6, 20, 6, 30)) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    flt = load_filter(filter_path)
    client = EdinetClient(cache_dir=cache_dir)
    crosstab: Counter = Counter()
    examples: dict = {}
    scanned: list[str] = []
    metas = []
    for y in years:
        m1, d1, m2, d2 = window
        hits = scan_for_filer(client, filer, date(y, m1, d1), date(y, m2, d2), flt, crosstab, examples, scanned)
        if len(hits) != 1:
            log.warning("FY%d: expected exactly 1 hit, got %d: %s", y, len(hits), [h.get("docID") for h in hits])
        for rec in hits:
            metas.append(process_doc(client, rec, out_dir))
    write_crosstab(out_dir, crosstab, examples, scanned)
    write_truncation_note(out_dir, metas)
    return {"metas": metas, "scanned": scanned, "crosstab": crosstab, "network_calls": client.calls}
