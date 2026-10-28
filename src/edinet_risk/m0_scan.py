"""M0 step 3 (M0-8): full-window document-list scan and formCode crosstab vs the official code table.

- Fetches documents.json?type=2 for every date in [start, end] (cached in cache/doclist, so resumable).
- Per-date retry with backoff; dates that still fail are recorded in out/m0/doclist_scan_failures.json.
- Crosstab docTypeCode x ordinanceCode x formCode over all cached days in the window, cross-checked
  against the official 様式コードリスト (ESE140327.xlsx, API仕様書 別紙1).
"""
from __future__ import annotations

import csv
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

from .client import EdinetAuthError, EdinetClient, EdinetError, EdinetNotFound
from .m0 import daterange

log = logging.getLogger(__name__)

FORMCODE_TABLE_URL = "https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140327.xlsx"
WATCH_FORMCODES = ("030000", "030001", "702001")

# 書類種別コード (API仕様書 書類種別コード一覧). The form-code table gives 書類種別 by name only.
DOC_TYPE_BY_NAME = {
    "有価証券通知書": "010", "変更通知書（有価証券通知書）": "020", "有価証券届出書": "030",
    "訂正有価証券届出書": "040", "届出の取下げ願い": "050", "発行登録通知書": "060",
    "変更通知書（発行登録通知書）": "070", "発行登録書": "080", "訂正発行登録書": "090",
    "発行登録追補書類": "100", "発行登録取下届出書": "110", "有価証券報告書": "120",
    "訂正有価証券報告書": "130", "確認書": "135", "訂正確認書": "136", "四半期報告書": "140",
    "訂正四半期報告書": "150", "半期報告書": "160", "訂正半期報告書": "170", "臨時報告書": "180",
    "訂正臨時報告書": "190", "親会社等状況報告書": "200", "訂正親会社等状況報告書": "210",
    "自己株券買付状況報告書": "220", "訂正自己株券買付状況報告書": "230", "内部統制報告書": "235",
    "訂正内部統制報告書": "236", "公開買付届出書": "240", "訂正公開買付届出書": "250",
    "公開買付撤回届出書": "260", "公開買付報告書": "270", "訂正公開買付報告書": "280",
    "意見表明報告書": "290", "訂正意見表明報告書": "300", "対質問回答報告書": "310",
    "訂正対質問回答報告書": "320", "別途買付け禁止の特例を受けるための申出書": "330",
    "訂正別途買付け禁止の特例を受けるための申出書": "340", "大量保有報告書": "350",
    "訂正大量保有報告書": "360", "基準日の届出書": "370", "変更の届出書": "380",
}


# -- fetch -----------------------------------------------------------------------
def fetch_window(client: EdinetClient, start: date, end: date, *, attempts: int = 4,
                 retry_base: float = 15.0, sleep=time.sleep) -> tuple[list[str], list[dict]]:
    """Fetch (or load from cache) every day list. Returns (ok_dates, failures). Auth errors abort."""
    ok, failures = [], []
    for d in daterange(start, end):
        ds = d.isoformat()
        cached = client.doclist_path(ds).exists()
        for i in range(attempts):
            try:
                obj = client.list_documents(ds)
                ok.append(ds)
                if not cached:
                    log.info("%s: %d docs (fetched)", ds, len(obj.get("results", [])))
                break
            except EdinetAuthError:
                raise
            except EdinetNotFound as e:
                failures.append({"date": ds, "error": str(e), "attempts": i + 1})
                break
            except EdinetError as e:
                log.warning("%s: attempt %d failed: %s", ds, i + 1, e)
                if i + 1 == attempts:
                    failures.append({"date": ds, "error": str(e), "attempts": attempts})
                else:
                    sleep(retry_base * (2 ** i))
    return ok, failures


# -- official code table -----------------------------------------------------------
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx_rows(path: Path) -> dict[str, list[list[str]]]:
    """Minimal stdlib xlsx reader: {sheet name: rows of cell strings}."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                # direct <t> or rich-text runs <r><t>; skip phonetic runs <rPh> (furigana)
                ts = si.findall("m:t", _NS) + si.findall("m:r/m:t", _NS)
                shared.append("".join(t.text or "" for t in ts))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        out = {}
        for sh in wb.find("m:sheets", _NS):
            target = targets[sh.get(rid_attr)].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            rows = []
            for row in ET.fromstring(z.read(target)).iter(f"{{{_NS['m']}}}row"):
                cells: dict[int, str] = {}
                for c in row.findall("m:c", _NS):
                    v = c.find("m:v", _NS)
                    if c.get("t") == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif c.get("t") == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
                    else:
                        val = v.text if v is not None else ""
                    cells[_col(c.get("r"))] = (val or "").strip()
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            out[sh.get("name")] = rows
    return out


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    for i, r in enumerate(rows[:30]):
        joined = [re.sub(r"\s", "", x) for x in r]
        idx = {}
        for j, h in enumerate(joined):
            if "様式コード" in h and "form" not in idx:
                idx["form"] = j
            elif "府令コード" in h and "ord" not in idx:
                idx["ord"] = j
            elif h.startswith("書類種別") and "doc" not in idx:
                idx["doc"] = j
            elif h.startswith("開示区分") and "disc" not in idx:
                idx["disc"] = j
            elif h in ("様式名", "様式名称") or ("様式名" in h and "name" not in idx):
                idx["name"] = j
        if "form" in idx and "ord" in idx:
            return i, idx
    return None


def load_formcode_table(path: Path) -> dict[tuple[str, str], dict]:
    """{(ordinanceCode, formCode): {"docTypeCodes": set, "name": str, "sheet": str}}."""
    table: dict[tuple[str, str], dict] = {}
    for sheet, rows in read_xlsx_rows(path).items():
        h = _find_header(rows)
        if not h:
            continue
        hi, idx = h
        last = {}
        for r in rows[hi + 1:]:
            get = lambda k: (r[idx[k]] if k in idx and idx[k] < len(r) else "")
            if get("ord"):  # merged cells: carry ordinance forward when blank
                last["ord"] = get("ord")
            fc = get("form")
            if not re.fullmatch(r"[0-9A-Z]{6}", fc):
                continue
            oc = (get("ord") or last.get("ord", "")).zfill(3)
            e = table.setdefault((oc, fc), {"docTypeCodes": set(), "docTypeNames": set(), "name": get("name"),
                                            "disclosure": get("disc"), "sheet": sheet})
            if get("doc"):
                e["docTypeNames"].add(get("doc"))
                if get("doc") in DOC_TYPE_BY_NAME:
                    e["docTypeCodes"].add(DOC_TYPE_BY_NAME[get("doc")])
        break  # first sheet with a header is the code list; later sheets are revision history
    return table


# -- crosstab ----------------------------------------------------------------------
def build_crosstab(client: EdinetClient, dates: list[str]):
    ct: Counter = Counter()
    examples: dict = {}
    hits: dict[str, list[dict]] = {fc: [] for fc in WATCH_FORMCODES}
    for ds in dates:
        p = client.doclist_path(ds)
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")).get("results", []):
            k = (r.get("docTypeCode"), r.get("ordinanceCode"), r.get("formCode"))
            ct[k] += 1
            examples.setdefault(k, (r.get("docID"), r.get("filerName"), r.get("docDescription")))
            if r.get("formCode") in hits:
                hits[r["formCode"]].append({x: r.get(x) for x in (
                    "docID", "docTypeCode", "ordinanceCode", "filerName", "submitDateTime",
                    "parentDocID", "xbrlFlag", "withdrawalStatus", "docDescription")} | {"listDate": ds})
    return ct, examples, hits


def write_report(out: Path, ct: Counter, examples: dict, hits: dict, table: dict | None,
                 start: date, end: date, ok_dates: list[str], failures: list[dict], table_src: str):
    out.mkdir(parents=True, exist_ok=True)
    key = lambda k: tuple(x or "" for x in k)
    rows = sorted(ct.items(), key=lambda kv: key(kv[0]))

    def status(k):
        dt, oc, fc = k
        if dt is None:
            return "メタデータ消去（取下げ・不開示）", ""
        if table is None:
            return "未照合", ""
        e = table.get((oc, fc))
        if e is None:
            return "表に無い(府令,様式)", ""
        if not e["docTypeCodes"]:
            return f"書類種別名をコード化できず({'/'.join(e['docTypeNames'])})", e["name"]
        if e["docTypeCodes"] and dt not in e["docTypeCodes"]:
            return f"書類種別不一致(表={'/'.join(sorted(e['docTypeCodes']))})", e["name"]
        return "OK", e["name"]

    with open(out / "formcode_crosstab.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["docTypeCode", "ordinanceCode", "formCode", "count", "official_check", "official_name",
                    "example_docID", "example_filerName", "example_docDescription"])
        for k, n in rows:
            w.writerow([*k, n, *status(k), *examples[k]])
    (out / "doclist_scan_failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=1) + "\n",
                                                    encoding="utf-8")
    hits_path = out / "formcode_watch_hits.json"
    hits_path.write_text(json.dumps(hits, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    total = sum(ct.values())
    n_days = (end - start).days + 1
    unexpected = [(k, n) for k, n in rows if status(k)[0] not in ("OK", "メタデータ消去（取下げ・不開示）")]
    L = ["# docTypeCode × ordinanceCode × formCode クロス集計（M0-8, 年度窓全体）", "",
         f"- 走査窓: 提出日 {start} 〜 {end}（{n_days} 日）。取得成功 {len(ok_dates)} 日、失敗 {len(failures)} 日"
         + (f"（{', '.join(x['date'] for x in failures)}、詳細 doclist_scan_failures.json）" if failures else ""),
         f"- 書類数（documents.json type=2 の results 全件、日付リスト間の重複含む）: {total}、組合せ数: {len(ct)}",
         f"- 公式様式コード表: {table_src}" + (f"（(府令,様式) {len(table)} 件を読込）" if table else "（未取得のため照合なし）"),
         "- 照合規則: (ordinanceCode, formCode) が表にあるか、表に書類種別コードがあれば docTypeCode が一致するか。",
         "- docTypeCode が空のレコードは取下げ・不開示等でメタデータが消されたもの（照合対象外）。",
         "- 一覧はキャッシュ取得時点のスナップショット（後日の取下げ・修正は未反映の可能性あり）。", ""]
    L += ["## 注目 formCode", ""]
    for fc in WATCH_FORMCODES:
        hs = hits[fc]
        if not hs:
            L.append(f"- **{fc}: 0 件（zero within period {start}..{end}）**")
            continue
        # The official name depends on (ordinanceCode, formCode): e.g. 030000 is 有価証券報告書 under 010
        # but 大量保有報告書（特例対象株券等） under 060, so label each docType/ordinance bucket separately.
        by = Counter((h["docTypeCode"], h["ordinanceCode"]) for h in hs)
        ex = {}
        for h in hs:
            ex.setdefault((h["docTypeCode"], h["ordinanceCode"]), h)

        def bucket(dt, oc, n):
            name = (table or {}).get((oc, fc), {}).get("name", "")
            e = ex[(dt, oc)]
            return (f"{dt}/{oc} {n}件{f'（公式: {name}）' if name else ''} 例 "
                    f"{e['docID']}({e['filerName']}, {e['listDate']})")
        L.append(f"- **{fc}**: {len(hs)} 件 — " + "; ".join(bucket(dt, oc, n) for (dt, oc), n in by.most_common()))
        if len({(table or {}).get((oc, fc), {}).get("name", "") for _, oc in by}) > 1:
            L.append(f"  - 注意: {fc} は府令・書類種別により別の様式を指す。formCode だけで絞り込まず、"
                     "docTypeCode と ordinanceCode を併用すること（有報は 120/010）。")
    L.append(f"- 全件は {hits_path.name} を参照。")
    L += ["", "## 表と照合して想定外・表に無い組合せ", ""]
    if table is None:
        L.append("- 公式表が未取得のため未照合。")
    elif not unexpected:
        L.append("- なし（メタデータ消去レコードを除き、全組合せが公式表と整合）。")
    else:
        L += ["| docTypeCode | ordinanceCode | formCode | 件数 | 判定 | 例 docID | 例 書類名 |", "|---|---|---|---:|---|---|---|"]
        for k, n in unexpected:
            ex = examples[k]
            L.append(f"| {k[0]} | {k[1]} | {k[2]} | {n} | {status(k)[0]} | {ex[0]} | {ex[2]} |")
    L += ["", "## 有報・訂正有報（docTypeCode 120/130）", "",
          "| docTypeCode | ordinanceCode | formCode | 件数 | 公式様式名 | 照合 | 例 docID | 例 提出者 |",
          "|---|---|---|---:|---|---|---|---|"]
    for k, n in rows:
        if k[0] in ("120", "130"):
            st, nm = status(k)
            L.append(f"| {k[0]} | {k[1]} | {k[2]} | {n} | {nm} | {st} | {examples[k][0]} | {examples[k][1]} |")
    L += ["", "## 全組合せ", "", "| docTypeCode | ordinanceCode | formCode | 件数 | 照合 | 公式様式名 | 例 docID | 例 書類名 |",
          "|---|---|---|---:|---|---|---|---|"]
    for k, n in rows:
        st, nm = status(k)
        L.append(f"| {k[0]} | {k[1]} | {k[2]} | {n} | {st} | {nm} | {examples[k][0]} | {examples[k][2]} |")
    (out / "formcode_crosstab.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return unexpected


def run(start: date, end: date, out_dir: Path, cache_dir: Path, table_path: Path | None,
        fetch: bool = True) -> dict:
    client = EdinetClient(cache_dir=cache_dir, backoff_base=10.0, max_retries=2)
    if fetch:
        ok, failures = fetch_window(client, start, end)
    else:
        ok = [d.isoformat() for d in daterange(start, end) if client.doclist_path(d.isoformat()).exists()]
        failures = [{"date": d.isoformat(), "error": "not cached (fetch skipped)", "attempts": 0}
                    for d in daterange(start, end) if not client.doclist_path(d.isoformat()).exists()]
    table = load_formcode_table(table_path) if table_path and table_path.exists() else None
    ct, examples, hits = build_crosstab(client, ok)
    src = f"{table_path} ({FORMCODE_TABLE_URL})" if table else FORMCODE_TABLE_URL
    unexpected = write_report(out_dir, ct, examples, hits, table, start, end, ok, failures, src)
    return {"ok": ok, "failures": failures, "crosstab": ct, "hits": hits, "unexpected": unexpected,
            "table_size": len(table) if table else 0, "network_calls": client.calls}
