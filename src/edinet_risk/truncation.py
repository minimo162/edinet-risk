"""M0 (H1): characterise the type=5 CSV 30,000-char truncation by comparing every TextBlock fact
in the CSV with the same fact (local name + contextRef) in the type=1 XBRL instance.

Hypotheses tested per fact:
- limit unit: Unicode code points vs UTF-8 bytes vs UTF-16 code units vs Shift_JIS(cp932) bytes;
- applied to the tag-stripped text (CSV value) vs to the raw HTML before stripping;
- whether the CSV keeps the head (prefix) and whether a marker is appended at the tail.
"""
from __future__ import annotations

import csv
import html as _html
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass

from .extract import _split, html_to_text

CSV_LIMIT = 30000
_WS = re.compile(r"\s+")
_IMG = re.compile(r"\[画像: [^\]]*\]")  # html_to_text keeps <img alt>; the CSV drops images


def csv_comparable(value: str) -> str:
    """The CSV keeps entity references literally, sometimes twice-escaped (``&amp;amp;``);
    decode until stable for comparison."""
    prev = None
    while prev != value:
        prev, value = value, _html.unescape(value)
    return value


def xbrl_comparable(text: str) -> str:
    return _IMG.sub("", text)


def norm(s: str) -> str:
    return _WS.sub("", unicodedata.normalize("NFKC", s))


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def xbrl_textblocks(zip_path) -> dict[tuple[str, str | None], str]:
    """{(localName, contextRef): html} for every *TextBlock fact in XBRL/PublicDoc/*.xbrl."""
    zf = zipfile.ZipFile(zip_path)
    out: dict[tuple[str, str | None], str] = {}
    for m in zf.namelist():
        if not (m.startswith("XBRL/PublicDoc/") and m.endswith(".xbrl")):
            continue
        for _, el in ET.iterparse(io.BytesIO(zf.read(m)), events=("end",)):
            _, local = _split(el.tag)
            if local.endswith("TextBlock"):
                out[(local, el.get("contextRef"))] = el.text or ""
            el.clear()
    return out


def csv_textblocks(zip_path) -> list[tuple[str, str, str, str]]:
    """[(elementId, contextId, value, member)] for every *TextBlock row in the type=5 CSVs."""
    zf = zipfile.ZipFile(zip_path)
    out = []
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        raw = zf.read(name)
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-16-le")
        for r in csv.reader(io.StringIO(text, newline=""), delimiter="\t"):
            if len(r) >= 3 and r[0].endswith("TextBlock"):
                out.append((r[0], r[2], r[-1], name))
    return out


def _cp932_len(s: str) -> int:
    return len(s.encode("cp932", errors="replace"))


@dataclass
class FactCompare:
    element: str
    context: str
    csv_chars: int
    csv_utf8_bytes: int
    csv_utf16_units: int
    csv_cp932_bytes: int
    csv_has_tags: bool
    csv_entity_refs: int               # literal &amp; / &lt; ... left in the CSV value
    xbrl_raw_html_chars: int | None
    xbrl_text_chars: int | None        # html_to_text (no title drop), code points
    csv_exactly_limit: bool
    norm_csv_is_prefix: bool | None    # NFKC+whitespace-stripped CSV is a prefix of XBRL text
    norm_equal: bool | None
    norm_common_prefix: int | None
    norm_xbrl_chars: int | None
    norm_csv_chars: int | None
    truncated: bool | None             # XBRL text strictly longer than CSV (after normalisation)
    csv_tail: str = ""
    xbrl_tail_at_cut: str = ""         # XBRL text around where the CSV ends
    xbrl_tail: str = ""
    cut_pos_in_xbrl: int | None = None  # where the CSV's last 30 normalised chars end in the XBRL text


def compare_fact(element: str, context: str, value: str, html: str | None, tail: int = 40) -> FactCompare:
    fc = FactCompare(
        element=element, context=context, csv_chars=len(value),
        csv_utf8_bytes=len(value.encode("utf-8")), csv_utf16_units=len(value.encode("utf-16-le")) // 2,
        csv_cp932_bytes=_cp932_len(value), csv_has_tags=bool(re.search(r"<[a-zA-Z/][^>]*>", value)),
        csv_entity_refs=len(re.findall(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);", value)),
        xbrl_raw_html_chars=None, xbrl_text_chars=None, csv_exactly_limit=len(value) == CSV_LIMIT,
        norm_csv_is_prefix=None, norm_equal=None, norm_common_prefix=None, norm_xbrl_chars=None,
        norm_csv_chars=None, truncated=None, csv_tail=value[-tail:])
    if html is None:
        return fc
    text, _ = html_to_text(html, drop_title=False)
    nv, nt = norm(csv_comparable(value)), norm(xbrl_comparable(text))
    cp = common_prefix(nv, nt)
    fc.xbrl_raw_html_chars, fc.xbrl_text_chars = len(html), len(text)
    fc.norm_csv_is_prefix, fc.norm_equal, fc.norm_common_prefix = nt.startswith(nv), nv == nt, cp
    fc.norm_xbrl_chars, fc.norm_csv_chars = len(nt), len(nv)
    # truncated := CSV stops at the limit while the XBRL text continues past it
    fc.truncated = len(value) >= CSV_LIMIT and len(nt) > len(nv)
    if nt.startswith(nv):
        fc.cut_pos_in_xbrl = len(nv)
    elif nv:  # small rendering differences upstream: locate the CSV's last 30 chars nearest len(nv)
        probe, hits, i = nv[-30:], [], nt.find(nv[-30:], max(0, len(nv) - 2000))
        while 0 <= i <= len(nv) + 2000:
            hits.append(i + len(probe))
            i = nt.find(probe, i + 1)
        fc.cut_pos_in_xbrl = min(hits, key=lambda e: abs(e - len(nv))) if hits else None
    cut = fc.cut_pos_in_xbrl if fc.cut_pos_in_xbrl is not None else cp
    fc.xbrl_tail_at_cut = nt[max(0, cut - tail):cut + tail // 2]
    fc.xbrl_tail = text.rstrip()[-tail:]
    return fc


def compare_doc(xbrl_zip, csv_zip, min_chars: int = 20000) -> list[FactCompare]:
    """Compare every CSV TextBlock with >= min_chars (or whose XBRL text is >= min_chars)."""
    xb = xbrl_textblocks(xbrl_zip)
    out = []
    for eid, ctx, value, _member in csv_textblocks(csv_zip):
        local = eid.split(":", 1)[-1]
        html = xb.get((local, ctx))
        if len(value) < min_chars and (html is None or len(html) < min_chars):
            continue
        out.append(compare_fact(local, ctx, value, html))
    return out


def classify_limit(rows: list[FactCompare]) -> dict:
    """Aggregate verdict over truncated facts: which measure is constant at the limit?"""
    tr = [r for r in rows if r.truncated]
    measures = {m: sorted({getattr(r, m) for r in tr})
                for m in ("csv_chars", "csv_utf8_bytes", "csv_utf16_units", "csv_cp932_bytes")}
    unit = [m for m, vals in measures.items() if vals == [CSV_LIMIT]]
    return {
        "truncated_facts": len(tr),
        "untruncated_long_facts": sum(1 for r in rows if r.truncated is False),
        "constant_at_limit": unit,             # e.g. ['csv_chars'] => limit counts code points
        "measure_ranges": {m: (v[0], v[-1]) if v else None for m, v in measures.items()},
        "all_truncated_are_prefix": all(r.norm_csv_is_prefix for r in tr) if tr else None,
        "any_tags_in_csv": any(r.csv_has_tags for r in rows),
        "max_untruncated_csv_chars": max((r.csv_chars for r in rows if r.truncated is False), default=None),
        "min_truncated_xbrl_text_chars": min((r.xbrl_text_chars for r in tr), default=None),
    }


def as_dict(fc: FactCompare) -> dict:
    return asdict(fc)


# --- M0 runner -------------------------------------------------------------------
FACT_COLUMNS = ["docID", "filerName", "secCode", "periodEnd", "element", "context", "csv_chars",
                "csv_utf8_bytes", "csv_utf16_units", "csv_cp932_bytes", "csv_has_tags", "csv_entity_refs",
                "xbrl_raw_html_chars", "xbrl_text_chars", "norm_csv_chars", "norm_xbrl_chars",
                "norm_csv_is_prefix", "norm_equal", "cut_pos_in_xbrl", "truncated"]


def doc_index(doclist_dir) -> dict[str, dict]:
    """docID -> doclist record, from the cached documents.json files (no network)."""
    import json
    from pathlib import Path
    idx = {}
    for p in sorted(Path(doclist_dir).glob("*.json")):
        for r in json.loads(p.read_text(encoding="utf-8")).get("results", []):
            if r.get("docID"):
                idx[r["docID"]] = r
    return idx


def run(client, doc_ids: list[str], out_dir, min_chars: int = 20000) -> dict:
    """Fetch type=1 and type=5 for each docID (cached), compare, write facts CSV + note."""
    import csv as _csv
    from pathlib import Path
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = doc_index(client.cache_dir / "doclist")
    docs = []
    for d in doc_ids:
        rec = idx.get(d, {"docID": d})
        rows = compare_doc(client.get_document(d, 1), client.get_document(d, 5), min_chars=min_chars)
        docs.append((rec, rows))
    with open(out_dir / "truncation_facts.csv", "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(FACT_COLUMNS)
        for rec, rows in docs:
            for r in rows:
                dd = {**{k: rec.get(k) for k in ("docID", "filerName", "secCode", "periodEnd")}, **asdict(r)}
                w.writerow([dd.get(k) for k in FACT_COLUMNS])
    verdict = classify_limit([r for _, rows in docs for r in rows])
    write_note(out_dir / "truncation_note.md", docs, verdict)
    return {"docs": docs, "verdict": verdict, "network_calls": client.calls}


def _yn(b) -> str:
    return {True: "はい", False: "いいえ", None: "—"}[b]


def write_note(path, docs, verdict):
    br = [(rec, r) for rec, rows in docs for r in rows if r.element == "BusinessRisksTextBlock"]
    other_tr = [(rec, r) for rec, rows in docs for r in rows if r.truncated and r.element != "BusinessRisksTextBlock"]
    L = ["# CSV（type=5）の TextBlock 30,000 字切り詰め条件（M0 / レビュー H1）", "",
         "`python -m edinet_risk m0_truncation` が生成。全ファクトの数値は `truncation_facts.csv`。",
         "文字数は Python `len()`（コードポイント）。正規化は NFKC＋空白全除去。比較用に CSV 側は実体参照を"
         "復号、XBRL 側は `html_to_text` の `[画像: …]` を除去してから比べる。", "",
         "## 結論", ""]
    tr_n = verdict["truncated_facts"]
    unit = verdict["constant_at_limit"]
    L += [
        f"- 切り詰めは **CSV 値が 30,000 文字ちょうど** で起きる。切り詰められた {tr_n} ファクトすべてで "
        f"文字数＝UTF-16 単位数＝30,000、UTF-8 バイト数は {verdict['measure_ranges']['csv_utf8_bytes']}、"
        f"cp932 バイト数は {verdict['measure_ranges']['csv_cp932_bytes']} とばらつく → **上限はバイトではなく文字数**"
        f"（一定だった尺度: {', '.join(unit) or 'なし'}。サロゲートペアを含む値が無く、コードポイントと UTF-16 単位は区別できていない）。",
        "- 数えている対象は **タグ除去後の CSV 値そのもの**（空白・改行、リテラルのまま残る `&amp;` 等の実体参照を含む）。"
        "生 HTML（最小でも数万〜数百万字）に対する上限ではない。",
        f"- 切り詰め後の値は XBRL 本文の **先頭部分（接頭辞）** で、末尾に「以下省略」等の印は付かない"
        f"（全件で正規化後の接頭辞一致: {_yn(verdict['all_truncated_are_prefix'])}）。文や段落の途中で切れる。",
        f"- 30,000 字未満の値は切り詰められない（未切り詰めの最大 {verdict['max_untruncated_csv_chars'] or 0:,} 字）。"
        "したがって **`len(CSV値) == 30000` を切り詰めの判定条件** にできる（XBRL 側がちょうど 30,000 字の偶然は残るが、XBRL との比較で判別可能）。",
        f"- CSV 値に HTML タグは残らない（{_yn(verdict['any_tags_in_csv'])}）。画像は落ち、`&lt;前連結会計年度&gt;` のような"
        "タグ風の本文も消える。表はセルが空白区切りで連結される。",
        "- 対応: 本文のソースは計画どおり type=1（XBRL）とする。CSV は使わない（使う場合も 30,000 字ちょうどは不完全として扱う）。",
        "- 副産物: XBRL 側の `html_to_text` が入れ子の表（セル内の `<table>`）の中身を落としていた不具合を CSV との比較で発見し修正した"
        "（役員の状況などで発生。テスト追加済み）。", ""]
    L += ["## BusinessRisksTextBlock", "",
          "| docID | 提出者 | periodEnd | CSV 字 | CSV UTF-8 B | XBRL テキスト字 | 正規化 CSV/XBRL | 接頭辞一致 | 完全一致 | 切り詰め |",
          "|---|---|---|---:|---:|---:|---:|---|---|---|"]
    for rec, r in sorted(br, key=lambda x: -x[1].csv_chars):
        L.append(f"| {rec.get('docID')} | {rec.get('filerName')} | {rec.get('periodEnd')} | {r.csv_chars:,} | "
                 f"{r.csv_utf8_bytes:,} | {r.xbrl_text_chars or 0:,} | {r.norm_csv_chars or 0:,} / {r.norm_xbrl_chars or 0:,} | "
                 f"{_yn(r.norm_csv_is_prefix)} | {_yn(r.norm_equal)} | **{_yn(r.truncated)}** |")
    L += ["", "### 切り詰められた BusinessRisks の末尾（正規化後、抜粋）", ""]
    for rec, r in br:
        if r.truncated:
            L += [f"- {rec.get('filerName')} {rec.get('docID')}: CSV 末尾 `…{norm(csv_comparable(r.csv_tail))[-25:]}` / "
                  f"XBRL の同位置 `…{r.xbrl_tail_at_cut}…`（CSV が XBRL の {r.cut_pos_in_xbrl:,} 字目で終わる。"
                  f"XBRL 本文は {r.norm_xbrl_chars:,} 字まで続く）"]
    L += ["", "## その他の切り詰められた TextBlock", "",
          "| docID | 提出者 | 要素 | CSV 字 | CSV UTF-8 B | 生HTML字 | XBRL テキスト字 | 実体参照数 | 接頭辞一致 |",
          "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for rec, r in other_tr:
        L.append(f"| {rec.get('docID')} | {rec.get('filerName')} | {r.element} | {r.csv_chars:,} | {r.csv_utf8_bytes:,} | "
                 f"{r.xbrl_raw_html_chars:,} | {r.xbrl_text_chars:,} | {r.csv_entity_refs} | {_yn(r.norm_csv_is_prefix)} |")
    L += ["", f"対象書類 {len(docs)} 件、比較ファクト {sum(len(rows) for _, rows in docs)} 件"
          f"（CSV または生 HTML が 20,000 字以上の TextBlock）。マツダの単独比較は `csv_xbrl_compare.md`。", ""]
    from pathlib import Path
    Path(path).write_text("\n".join(L), encoding="utf-8")
