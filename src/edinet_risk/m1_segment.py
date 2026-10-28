"""M1 step C: segment the 7 transport filers x 2 periods (out/m0/*_business_risks.txt), offline.

Writes ``segment_summary.md`` (items per filer/period, first/last leaf headings, tree) and
``segments_transport7.json`` (all items with offsets) for the later match/diff steps.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .m0_multi import load_targets
from .segment.segmenter import SEGMENTER_VER, Segmentation, segment

PREAMBLE_WARN = 1000  # chars before the first heading worth a look


def load_docs(m0_dir: Path, targets_path: Path) -> list[dict]:
    """(meta, text) for every *_business_risks.txt with a meta.json, ordered as in the targets file."""
    filers, _ = load_targets(targets_path)
    order = {f["edinetCode"]: (i, f["label"]) for i, f in enumerate(filers)}
    docs = []
    for p in sorted(Path(m0_dir).glob("*_business_risks.txt")):
        meta_p = p.with_name(p.name.replace("_business_risks.txt", "_meta.json"))
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        rank, label = order.get(meta.get("edinetCode"), (len(order), meta.get("filerName", "?")))
        docs.append({"rank": rank, "label": label, "meta": meta, "text": p.read_text(encoding="utf-8")})
    docs.sort(key=lambda d: (d["rank"], d["meta"].get("periodEnd") or ""))
    return docs


def _period(pe: str | None) -> str:
    return f"{pe[:4]}年{int(pe[5:7])}月期" if pe else "?"


def _cell(s: str) -> str:
    return s.replace("|", "\\|")


def run(m0_dir: Path, targets_path: Path, out_dir: Path) -> list[dict]:
    rows = []
    for d in load_docs(m0_dir, targets_path):
        seg = segment(d["text"])
        rows.append({"label": d["label"], "docID": d["meta"]["docID"], "periodEnd": d["meta"].get("periodEnd"),
                     "chars": len(d["text"]), "seg": seg})
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_md(out_dir / "segment_summary.md", rows)
    dump = [{"label": r["label"], "docID": r["docID"], "periodEnd": r["periodEnd"],
             "segmenter_ver": r["seg"].segmenter_ver, "segmentation": r["seg"].segmentation,
             "families": r["seg"].families, "preamble": r["seg"].preamble,
             "items": [asdict(it) for it in r["seg"].items]} for r in rows]
    (out_dir / "segments_transport7.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
    return rows


def _tree(seg: Segmentation) -> list[str]:
    out = []
    for it in seg.items:
        mark = "" if it.is_leaf else " ＊"
        out.append(f"{'  ' * (it.level - 1)}- {it.heading_raw or '(段落)'}{mark}（{len(it.body)}字）")
    return out


def write_md(path: Path, rows: list[dict]) -> None:
    L = [f"# M1 項目分割の結果（輸送用機器7社×2期、`{SEGMENTER_VER}`）", "",
         "入力: `out/m0/*_business_risks.txt`（M0 抽出の本文、オフライン）。分割: `segment.segmenter`。",
         "項目数 = 最下位の見出し（子見出しを持たない見出し、計画 D2）の数。全見出し数は大見出し・グループ見出しを含む。",
         "前文 = 最初の見出しより前の文字数。体系 = 番号の種類を階層の上から順に（paren_num=(1)/（１）, "
         "circled=①, num_dot=１．）。", "",
         "| 会社 | 期 | docID | 字数 | 分割 | 体系 | 項目数 | 全見出し | 前文字数 | 先頭の項目 | 末尾の項目 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        s, lv = r["seg"], r["seg"].leaves
        first = lv[0].heading_raw if lv else "-"
        last = lv[-1].heading_raw if lv else "-"
        L.append(f"| {r['label']} | {_period(r['periodEnd'])} | {r['docID']} | {r['chars']} | {s.segmentation} | "
                 f"{' > '.join(s.families) or '-'} | {len(lv)} | {len(s.items)} | {len(s.preamble)} | "
                 f"{_cell(first)} | {_cell(last)} |")
    L += ["", "## 要確認（自動検出）", ""]
    notes = []
    for r in rows:
        s, tag = r["seg"], f"{r['label']} {_period(r['periodEnd'])}"
        if s.segmentation != "normal":
            notes.append(f"- {tag}: 見出しを検出できず {s.segmentation}（段落単位）")
        if len(s.preamble) >= PREAMBLE_WARN:
            notes.append(f"- {tag}: 前文が {len(s.preamble)} 字（番号なしの見出しの下にある本文が前文に入っている可能性）")
        for it in s.items:
            if not it.is_leaf and it.body.strip():
                notes.append(f"- {tag}: 子を持つ見出し「{_cell(it.heading_raw)}」に自身の本文 {len(it.body)} 字"
                             "（項目には含めず、親の導入文として別に保持）")
    L += notes or ["- なし"]
    L += ["", "## 見出しの木（＊ = 子を持つ見出し。本文字数は見出し行を除く自身の本文）", ""]
    for r in rows:
        L += [f"### {r['label']} {_period(r['periodEnd'])}（{r['docID']}）", ""] + _tree(r["seg"]) + [""]
    path.write_text("\n".join(L), encoding="utf-8")
