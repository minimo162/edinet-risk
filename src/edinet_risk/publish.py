"""Weekly publish helpers (docs/publish_peer.spec.md §1): build publish/ under canonical names.

``python -m edinet_risk.publish build --out <out_dir> --work <publish_dir> [--dry-run]``
finds the newest run folders under ``out_dir`` and writes:
risk_diff.xlsx, monitor.xlsx (renamed copies), 社一覧.xlsx, 社別/<code>_<name>.md, 週次要約.md.
No network, no API key.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

CANONICAL = ("risk_diff.xlsx", "monitor.xlsx", "社一覧.xlsx", "社別", "週次要約.md", "templates")
TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "templates" / "yoY_report_template.xlsx"
LIST_COLS = ["証券コード", "社名", "EDINETコード", "当期書類", "当期期末", "前期書類", "前期期末", "更新日"]


def latest(out: Path, pattern: str) -> Path | None:
    hits = sorted((p for p in out.glob(pattern) if p.exists()), key=lambda p: (p.stat().st_mtime, str(p)))
    return hits[-1] if hits else None


def find_sources(out: Path) -> dict[str, Path | None]:
    return {"risk_diff": latest(out, "run_*/risk_diff*.xlsx"),
            "compare": latest(out, "run_*/compare.json"),
            "reconcile": latest(out, "run_*/reconcile.md"),
            "monitor": latest(out, "monitor_*/monitor.xlsx"),
            "monitor_md": latest(out, "monitor_*/monitor.md"),
            "resolve": latest(out, "resolve_*/resolve.csv")}


def read_targets(paths: list[Path]) -> set[str]:
    import tomllib
    codes: set[str] = set()
    for p in paths:
        for f in tomllib.loads(Path(p).read_text(encoding="utf-8")).get("filer", []):
            if f.get("edinetCode"):
                codes.add(f["edinetCode"])
    return codes


def company_rows(resolve_csv: Path, targets: set[str] | None, today: str) -> list[list[str]]:
    rows = []
    with open(resolve_csv, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if targets and r.get("edinet_code") not in targets:
                continue
            rows.append([r.get("key", ""), r.get("name", ""), r.get("edinet_code", ""), r.get("cur_doc_id", ""),
                         r.get("cur_period_end", ""), r.get("prev_doc_id", ""), r.get("prev_period_end", ""), today])
    return rows


def write_company_list(rows: list[list[str]], path: Path) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "社一覧"
    ws.append(LIST_COLS)
    for r in rows:
        ws.append(r)
    wb.save(path)


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_") or "unknown"


def company_md(filer: dict, code: str) -> str:
    out = [f"# {filer.get('label', '')}（{code}）事業等のリスク 前期→当期", "",
           f"- EDINETコード: {filer.get('edinetCode') or ''}",
           f"- 当期書類: {filer.get('cur_doc') or ''} / 前期書類: {filer.get('prev_doc') or ''}"]
    if filer.get("skip"):
        out += ["", f"> 比較なし: {filer['skip']}", ""]
        return "\n".join(out) + "\n"
    for side, title in (("cur", "当期"), ("prev", "前期")):
        s = filer.get(side) or {}
        out += ["", f"## {title}（期末 {s.get('period_end') or ''}、{s.get('chars', 0)}字）", ""]
        for it in s.get("items", []):
            out += [f"### {it.get('heading_raw', '').strip()}", "", (it.get("body") or "").strip(), ""]
    return "\n".join(out) + "\n"


def write_company_mds(compare_json: Path, codes: dict[str, str], dest: Path) -> list[Path]:
    data = json.loads(compare_json.read_text(encoding="utf-8"))
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for f in data.get("filers", []):
        code = codes.get(f.get("edinetCode") or "", f.get("edinetCode") or "")
        p = dest / f"{_safe(code)}_{_safe(f.get('label', ''))}.md"
        p.write_text(company_md(f, code), encoding="utf-8")
        written.append(p)
    return written


def weekly_summary(rows: list[list[str]], monitor_md: Path | None, reconcile_md: Path | None, today: str) -> str:
    out = [f"# 週次要約（{today}）", "", "## 対象社", "", "| " + " | ".join(LIST_COLS[:7]) + " |",
           "|" + "---|" * 7]
    out += ["| " + " | ".join(r[:7]) + " |" for r in rows]
    for title, p in (("監視（monitor）", monitor_md), ("照合（reconcile）", reconcile_md)):
        out += ["", f"## {title}", ""]
        out.append(p.read_text(encoding="utf-8").strip() if p else "（ファイルなし）")
    return "\n".join(out) + "\n"


def build(out: Path, work: Path, targets: list[Path], *, dry_run: bool = False,
          today: str | None = None, template: Path = TEMPLATE) -> dict:
    today = today or date.today().isoformat()
    src = find_sources(out)
    missing = [k for k in ("risk_diff", "compare", "monitor", "resolve") if not src[k]]
    if not Path(template).exists():
        missing.append("template")
    plan = {"sources": {k: str(v) if v else None for k, v in src.items()}, "work": str(work),
            "missing": missing, "dry_run": dry_run, "ok": not missing}
    if dry_run or missing:
        return plan
    stage = work.parent / (work.name + ".tmp")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy2(src["risk_diff"], stage / "risk_diff.xlsx")
    shutil.copy2(src["monitor"], stage / "monitor.xlsx")
    codes = read_targets(targets) if targets else None
    rows = company_rows(src["resolve"], codes, today)
    write_company_list(rows, stage / "社一覧.xlsx")
    write_company_mds(src["compare"], {r[2]: r[0] for r in rows}, stage / "社別")
    (stage / "週次要約.md").write_text(weekly_summary(rows, src["monitor_md"], src["reconcile"], today),
                                       encoding="utf-8")
    (stage / "templates").mkdir()
    shutil.copy2(template, stage / "templates" / "yoY_report_template.xlsx")  # Copilot 出力の固定書式
    absent = [n for n in CANONICAL if not (stage / n).exists()]
    if absent:
        plan |= {"ok": False, "missing": absent}
        return plan
    work.mkdir(parents=True, exist_ok=True)
    for n in CANONICAL:                      # overwrite in place (keeps the SharePoint links)
        s, d = stage / n, work / n
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    shutil.rmtree(stage)
    plan["companies"] = len(rows)
    return plan


def sync(work: Path, dest: Path, *, dry_run: bool = False) -> list[str]:
    """Copy publish/ into the SharePoint sync folder, overwriting files (never delete-and-recreate)."""
    done = []
    for p in sorted(work.rglob("*")):
        if p.is_file():
            d = dest / p.relative_to(work)
            done.append(str(d))
            if not dry_run:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, d)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="edinet_risk.publish")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--work", type=Path, default=Path("publish"))
    b.add_argument("--targets", type=Path, nargs="*", default=[])
    b.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("sync")
    s.add_argument("--work", type=Path, default=Path("publish"))
    s.add_argument("--dest", type=Path, required=True)
    s.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        res = build(a.out, a.work, a.targets, dry_run=a.dry_run)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res["ok"] else 2
    files = sync(a.work, a.dest, dry_run=a.dry_run)
    print(f"{'would copy' if a.dry_run else 'copied'} {len(files)} files -> {a.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
