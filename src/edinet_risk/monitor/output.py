"""Monitoring report files (M2-8): Excel (監視_概要 / 監視_詳細 sheets), CSV, JSON, Markdown.

Every string goes through ``scrub`` (key-masked URL params + the key string itself) before it is written.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..ingest.fetch import scrub
from .metrics import Detail, Metric, Report

SUMMARY_COLS = ("metric", "label", "phase", "value", "threshold", "breached", "note")
DETAIL_COLS = ("category", "metric", "entity_id", "key", "name", "role", "doc_id", "value", "threshold", "detail")
SUMMARY_JA = ("指標", "内容", "初出", "値", "閾値", "超過", "メモ")
DETAIL_JA = ("区分", "指標", "entity_id", "証券コード", "会社名", "cur/prev", "docID", "値", "閾値", "詳細")
_RED = PatternFill("solid", fgColor="FFC7CE")


def _clean(v, key: str | None):
    return scrub(v, key) if isinstance(v, str) else v


def _rows(objs: list[Metric] | list[Detail], cols, key):
    for o in objs:
        d = asdict(o)
        yield [_clean(int(d[c]) if isinstance(d[c], bool) else d[c], key) for c in cols]


def write(rep: Report, out_dir: Path, *, key: str | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"xlsx": out_dir / "monitor.xlsx", "summary_csv": out_dir / "monitor_summary.csv",
             "details_csv": out_dir / "monitor_details.csv", "json": out_dir / "monitor.json",
             "md": out_dir / "monitor.md"}
    summary = list(_rows(rep.metrics, SUMMARY_COLS, key))
    details = list(_rows(rep.details, DETAIL_COLS, key))
    for p, cols, rows in ((paths["summary_csv"], SUMMARY_COLS, summary), (paths["details_csv"], DETAIL_COLS, details)):
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "監視_概要"
    ws.append([f"monitor run_id={rep.run_id}  resolve run={rep.resolve_run}  scope={rep.meta.get('scope')}  "
               f"FY {rep.meta.get('fy')}  T={rep.meta.get('as_of')}  閾値 {rep.meta.get('config_version')}"])
    ws.append([f"閾値超過 {len(rep.breached)} 件: " + (", ".join(m.metric for m in rep.breached) or "なし")])
    ws.append(list(SUMMARY_JA))
    for c in ws[3]:
        c.font = Font(bold=True)
    for r in summary:
        ws.append(["○" if v == 1 and i == 5 else ("" if v == 0 and i == 5 else v) for i, v in enumerate(r)])
        if r[5]:
            for c in ws[ws.max_row]:
                c.fill = _RED
    for col, w in zip("ABCDEFG", (34, 52, 6, 12, 10, 6, 50)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    wd = wb.create_sheet("監視_詳細")
    wd.append(list(DETAIL_JA))
    for c in wd[1]:
        c.font = Font(bold=True)
    for r in details:
        wd.append(r)
    for col, w in zip("ABCDEFGHIJ", (16, 30, 10, 10, 28, 8, 11, 10, 8, 80)):
        wd.column_dimensions[col].width = w
    wd.freeze_panes = "A2"
    wd.auto_filter.ref = f"A1:J{max(1, wd.max_row)}"
    wb.save(paths["xlsx"])

    meta = {k: _clean(v, key) for k, v in rep.meta.items()}
    paths["json"].write_text(json.dumps(
        {"run_id": rep.run_id, **meta, "breached": [m.metric for m in rep.breached],
         "metrics": [dict(zip(SUMMARY_COLS, r)) for r in summary],
         "details": [dict(zip(DETAIL_COLS, r)) for r in details]},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# 監視レポート（run_id={rep.run_id}）", "",
             f"resolve run {rep.resolve_run}、{meta.get('scope')}、FY {meta.get('fy')}、T={meta.get('as_of')}、"
             f"閾値 {meta.get('config_version')}（{meta.get('config')}）。", "",
             f"**閾値超過 {len(rep.breached)} 件**: " + (", ".join(m.metric for m in rep.breached) or "なし"), "",
             "| 指標 | 内容 | 初出 | 値 | 閾値 | 超過 | メモ |", "|---|---|---|---|---|---|---|"]
    for r in summary:
        v = f"{r[3]:.4f}" if isinstance(r[3], float) and not float(r[3]).is_integer() else (
            "-" if r[3] is None else f"{r[3]:g}")
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {v} | {'-' if r[4] is None else f'{r[4]:g}'} | "
                     f"{'○' if r[5] else ''} | {r[6] or ''} |")
    lines += ["", f"詳細 {len(details)} 行は monitor_details.csv／Excel の「監視_詳細」シート。"]
    paths["md"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths
