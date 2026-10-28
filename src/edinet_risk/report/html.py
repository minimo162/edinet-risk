"""Full-text diff HTML (plan §3 report/html.py, §4 M1-4): every pair of every filer, no cell-size limit.

``index.html`` lists the filers; ``diff_{n}_{label}.html`` shows the pairs left (previous) / right (current)
with previous-only text in blue (``<del>``) and current-only text in red underline (``<ins>``). Output is
deterministic (no timestamps) so the idempotency test can compare the files byte for byte.
"""
from __future__ import annotations

import re
from html import escape
from pathlib import Path

from .excel import CHANGE_JA, LABEL_JA, SOURCE_NOTICE

CSS = """body{font-family:"Yu Gothic","Meiryo",sans-serif;font-size:14px;margin:1em}
table{border-collapse:collapse;width:100%;table-layout:fixed}
th,td{border:1px solid #bbb;padding:4px 6px;vertical-align:top}
th{background:#ddebf7}td.t{white-space:pre-wrap;word-break:break-all}
col.m{width:13em}del{color:#00f;text-decoration:none}ins{color:#f00;text-decoration:underline}
tr.review td{background:#fff8e0}.meta{color:#555;font-size:12px}"""


def file_name(n: int, label: str) -> str:
    return f"diff_{n:02d}_{re.sub(r'[^0-9A-Za-z一-龥ぁ-んァ-ヶー]+', '_', label)}.html"


def _side(blocks: list[dict], side: str) -> str:
    own, tag = ("prev_only", "del") if side == "prev" else ("cur_only", "ins")
    out = []
    for b in blocks:
        out.append(escape(b[f"gap_{side}"]))
        for op, t in b["segments"]:
            if op == "equal":
                out.append(escape(t))
            elif op == own:
                out.append(f"<{tag}>{escape(t)}</{tag}>")
    return "".join(out)


def _page(title: str, body: list[str]) -> str:
    return "\n".join(["<!DOCTYPE html>", '<html lang="ja"><head><meta charset="utf-8">',
                      f"<title>{escape(title)}</title><style>{CSS}</style></head><body>", *body,
                      "<hr><p class=\"meta\">" + "<br>".join(f"{escape(k)}: {escape(v)}" for k, v in SOURCE_NOTICE)
                      + "</p>", "</body></html>", ""])


def _f(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def filer_page(f: dict, data: dict) -> str:
    hp = {it["idx"]: it["heading_raw"] for it in f["prev"]["items"]}
    hc = {it["idx"]: it["heading_raw"] for it in f["cur"]["items"]}
    body = [f"<h1>{escape(f['label'])}「事業等のリスク」前期比較</h1>",
            f"<p class=\"meta\">前期 {escape(f['prev_doc'])}（{escape(str(f['prev_kind']))}）→ 当期 "
            f"{escape(f['cur_doc'])}（{escape(str(f['cur_kind']))}）。{escape(data['matcher_ver'])} / "
            f"{escape(data['differ_ver'])} / thresholds {escape(data['thresholds_version'])}。"
            "青字＝前期のみ、赤字下線＝当期のみ。</p>",
            '<p><a href="index.html">一覧へ</a></p>',
            '<table><colgroup><col style="width:3em"><col class="m"><col><col></colgroup>',
            "<tr><th>#</th><th>判定</th><th>前期</th><th>当期</th></tr>"]
    for p in f["pairs"]:
        meta = (f"{LABEL_JA[p['label']]}／{p['stage'] or '-'}<br>S={_f(p['score_s'])}<br>本文={_f(p['body_sim'])}"
                f"<br>見出し={_f(p['head_sim'])}<br>変更{CHANGE_JA[p['change']]}"
                + (f"（{escape(p['minor_kind'])}）" if p["minor_kind"] else "")
                + ("<br>要確認" if p["review_flag"] else "") + ("<br>番号変更" if p["renumbered"] else ""))
        ph = " ＋ ".join(hp[i] for i in p["prev_items"])
        ch = " ＋ ".join(hc[i] for i in p["cur_items"])
        body.append(f"<tr{' class=\"review\"' if p['review_flag'] else ''} id=\"p{p['pair_idx'] + 1}\">"
                    f"<td>{p['pair_idx'] + 1}</td><td>{meta}</td>"
                    f"<td class=\"t\"><b>{escape(ph)}</b>\n{_side(p['blocks'], 'prev')}</td>"
                    f"<td class=\"t\"><b>{escape(ch)}</b>\n{_side(p['blocks'], 'cur')}</td></tr>")
    body.append("</table>")
    return _page(f"{f['label']} 事業等のリスク 前期比較", body)


def write_html(out_dir: Path, data: dict) -> dict[str, str]:
    """Writes index.html + one page per filer; returns {label: file name}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names: dict[str, str] = {}
    rows = ["<tr><th>会社</th><th>前期</th><th>当期</th><th>ペア数</th><th>変更あり</th><th>要確認</th><th>備考</th></tr>"]
    for n, f in enumerate(data["filers"], 1):
        if f["cur"] is None:
            rows.append(f"<tr><td>{escape(f['label'])}</td><td>{escape(str(f['prev_doc']))}</td>"
                        f"<td>{escape(str(f['cur_doc']))}</td><td>-</td><td>-</td><td>-</td>"
                        f"<td>{escape(f['skip'] or '')}</td></tr>")
            continue
        name = names[f["label"]] = file_name(n, f["label"])
        (out_dir / name).write_text(filer_page(f, data), encoding="utf-8")
        ps = f["pairs"]
        rows.append(f"<tr><td><a href=\"{escape(name)}\">{escape(f['label'])}</a></td><td>{escape(f['prev_doc'])}</td>"
                    f"<td>{escape(f['cur_doc'])}</td><td>{len(ps)}</td>"
                    f"<td>{sum(p['change'] == 'changed' for p in ps)}</td><td>{sum(p['review_flag'] for p in ps)}</td>"
                    f"<td>{escape(', '.join(f['flags']))}</td></tr>")
    idx = _page("事業等のリスク 前期比較 一覧", ["<h1>事業等のリスク 前期比較（全文差分）</h1>",
                                         f"<p class=\"meta\">run_id={escape(data['run_id'])}</p>",
                                         "<table>", *rows, "</table>"])
    (out_dir / "index.html").write_text(idx, encoding="utf-8")
    return names
