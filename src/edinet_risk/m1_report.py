"""M1 step F: ``report`` — render a ``compare`` run (compare.json) as Excel + HTML + 照合 (offline).

Outputs in ``out_dir`` (default: the run directory): ``risk_diff_transport7.xlsx`` (概要, one sheet per
filer, 照合, 出典), ``html/index.html`` + one full-text diff page per filer, and ``reconcile.csv`` /
``reconcile.md`` (the 照合 sheet as text, for review without Excel).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import m1_compare
from .report import excel, html, reconcile

XLSX_NAME = "risk_diff_transport7.xlsx"


def load_gold(path: Path | None) -> dict | None:
    """Gold JSON written by tools/gold_import.py ({"docs": {docID: {n_items, first, last}}}), if given."""
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8")).get("docs", {})


def run(run_dir: Path, out_dir: Path | None = None, *, gold_path: Path | None = None,
        cell_max: int = excel.CELL_MAX) -> dict:
    data = m1_compare.load(run_dir)
    out_dir = Path(out_dir or run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gold = load_gold(gold_path)
    names = html.write_html(out_dir / "html", data)
    res = excel.write_workbook(out_dir / XLSX_NAME, data, html_names={k: f"html/{v}" for k, v in names.items()},
                               gold=gold, cell_max=cell_max)
    rs = reconcile.rows(data, gold)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(reconcile.HEADER)
    w.writerows(rs)
    (out_dir / "reconcile.csv").write_text(buf.getvalue(), encoding="utf-8-sig")
    md = [f"# 照合（run_id={data['run_id']}）", "",
          "手作業 Excel による正解照合は M1 の対象外（不要）のため「手作業」列は空、判定は「対象外」。全文一致 = 抽出本文 → 項目分割 → "
          "差分表示で文字の欠落がない（比較時のみ NFKC・空白を正規化）。", "",
          "| " + " | ".join(reconcile.HEADER) + " |", "|" + "---|" * len(reconcile.HEADER)]
    md += ["| " + " | ".join("" if v is None else str(v).replace("|", "\\|") for v in r) + " |" for r in rs]
    (out_dir / "reconcile.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return {"out_dir": out_dir, "xlsx": out_dir / XLSX_NAME, "html": names, "reconcile": res["reconcile"],
            "run_id": data["run_id"]}
