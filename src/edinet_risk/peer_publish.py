"""Weekly peer publication helpers for ``scripts/publish_peer.ps1`` (docs/publish_peer.spec.md, offline).

Used through ``python -m edinet_risk publish {config,targets,build,check}``. The CLI keeps its run-specific
names; this module writes the files the CLI does not produce, under the canonical names of spec §1, into the
local ``publish/`` folder:

- ``社一覧.xlsx``: the target filers of the resolve run (証券コード, 社名, EDINETコード, 当期/前期の書類と期末, 更新日)
- ``社別/<証券コード>_<社名>.md``: per filer the previous / current text of every matched item (compare.json)
- ``週次要約.md``: 対象社一覧 + 変化表 + 要確認 + ``monitor.md`` + ``reconcile.md`` (fallback when xlsx can't be read)

``risk_diff.xlsx`` / ``monitor.xlsx`` are plain renamed copies, made by the PowerShell script from the source
paths that ``build`` returns. ``check`` is the success definition of spec §2-5: all five present and
``社一覧.xlsx`` / ``monitor.xlsx`` written today; otherwise the sync folder keeps the previous week's files.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tomllib
from datetime import date, datetime
from pathlib import Path

from .report.excel import CHANGE_JA, LABEL_JA, SOURCE_NOTICE, _header
from .store.db import JST

RISK_DIFF = "risk_diff.xlsx"
MONITOR = "monitor.xlsx"
COMPANY_LIST = "社一覧.xlsx"
PER_COMPANY = "社別"
WEEKLY = "週次要約.md"
TEMPLATES = "templates"       # optional (Copilot 見本ブック)
CANONICAL = (RISK_DIFF, MONITOR, COMPANY_LIST, PER_COMPANY, WEEKLY)
COMPANY_HEADER = ("証券コード", "社名", "EDINETコード", "当期書類", "当期期末", "前期書類", "前期期末", "更新日")
REPORT_XLSX = "risk_diff_transport7.xlsx"   # m1_report.XLSX_NAME (CLI output name)


class PublishError(Exception):
    pass


def today_jst() -> date:
    return datetime.now(JST).date()


# ---------------------------------------------------------------- targets (transport7 + 依頼社リスト)
def sec_codes(conn: sqlite3.Connection | None) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """({edinet_code: (証券コード4桁, name)}, {証券コード4桁: (edinet_code, name)}) from edinet_codes."""
    by_code: dict[str, tuple[str, str]] = {}
    by_sec: dict[str, tuple[str, str]] = {}
    if conn is None:
        return by_code, by_sec
    try:
        rows = conn.execute("SELECT edinet_code, sec_code5, name FROM edinet_codes "
                            "WHERE sec_code5 IS NOT NULL AND sec_code5 != '' ORDER BY listed DESC, edinet_code")
    except sqlite3.OperationalError:     # schema not created yet
        return by_code, by_sec
    for code, sec5, name in rows:
        by_code.setdefault(code, (sec5[:4], name or ""))
        by_sec.setdefault(sec5[:4], (code, name or ""))
    return by_code, by_sec


def merge_targets(paths: list[Path], conn: sqlite3.Connection | None = None) -> tuple[list[dict], list[dict]]:
    """[[filer]] of every file (first occurrence of an edinetCode wins) + their [[period]] (deduplicated).

    A request entry may give ``secCode`` instead of ``edinetCode`` (looked up in edinet_codes); ``label``
    defaults to the EDINET name. ``secCode`` is filled in from edinet_codes when known.
    """
    by_code, by_sec = sec_codes(conn)
    filers: list[dict] = []
    periods: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        try:
            with open(p, "rb") as f:
                cfg = tomllib.load(f)
        except FileNotFoundError:
            raise PublishError(f"targets file not found: {p}") from None
        except tomllib.TOMLDecodeError as e:
            raise PublishError(f"{p}: invalid TOML: {e}") from None
        for i, f in enumerate(cfg.get("filer", []), 1):
            code = str(f.get("edinetCode", "")).strip()
            sec = str(f.get("secCode", "")).strip()
            if not code and sec:
                code = by_sec.get(sec, ("", ""))[0]
                if not code:
                    raise PublishError(f"{p} filer #{i}: secCode {sec} is not in edinet_codes (run `codes sync`, "
                                       "or give edinetCode)")
            if not re.fullmatch(r"E\d{5}", code):
                raise PublishError(f"{p} filer #{i}: need edinetCode (E + 5 digits) or secCode, got {code or '-'}")
            if code in seen:
                continue
            seen.add(code)
            sec = sec or by_code.get(code, ("", ""))[0]
            label = str(f.get("label", "")).strip() or by_code.get(code, ("", ""))[1] or code
            filers.append({"label": label, "edinetCode": code} | ({"secCode": sec} if sec else {}))
        for per in cfg.get("period", []):
            if per not in periods:
                periods.append(dict(per))
    if not filers:
        raise PublishError("no [[filer]] in the targets files")
    return filers, periods


def _toml_str(v) -> str:
    return json.dumps(str(v), ensure_ascii=False)     # a JSON string is a valid TOML basic string


def write_targets(path: Path, filers: list[dict], periods: list[dict]) -> Path:
    """One targets TOML (same shape as config/transport7.toml) for resolve / fetch."""
    lines = ["# generated by `python -m edinet_risk publish targets` (do not edit; edit publish.targets instead)", ""]
    if not periods:      # m0_multi.load_targets expects the key; must precede the [[filer]] tables
        lines += ["period = []", ""]
    for f in filers:
        lines += ["[[filer]]"] + [f"{k} = {_toml_str(v)}" for k, v in f.items()] + [""]
    for per in periods:
        lines += ["[[period]]"] + [f"{k} = {_toml_str(v)}" for k, v in per.items()] + [""]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def load_filers(path: Path) -> list[dict]:
    with open(path, "rb") as f:
        return tomllib.load(f).get("filer", [])


# ---------------------------------------------------------------- locating the CLI outputs
def find_monitor_dir(out_root: Path, resolve_run: str) -> Path:
    """Newest ``monitor_*`` under ``out_root`` whose monitor.json belongs to ``resolve_run``."""
    hits = []
    for d in Path(out_root).glob("monitor_*"):
        mj = d / "monitor.json"
        if not (mj.exists() and (d / "monitor.xlsx").exists()):
            continue
        try:
            if json.loads(mj.read_text(encoding="utf-8")).get("resolve_run") == resolve_run:
                hits.append(d)
        except json.JSONDecodeError:
            continue
    if not hits:
        raise PublishError(f"no {out_root}/monitor_*/monitor.json for resolve run {resolve_run}; run `monitor` first")
    return max(hits, key=lambda d: (d.stat().st_mtime, d.name))


def locate(out_root: Path, resolve_run: str, compare_run: str | None = None) -> dict[str, Path]:
    """Sources of spec §1 for one weekly run (``compare_run`` defaults to ``resolve_run``)."""
    out_root = Path(out_root)
    src = {"resolve_dir": out_root / f"resolve_{resolve_run}",
           "compare_dir": out_root / "m1" / f"run_{compare_run or resolve_run}"}
    src |= {"resolve_json": src["resolve_dir"] / "resolve.json",
            "compare_json": src["compare_dir"] / "compare.json",
            "risk_diff": src["compare_dir"] / REPORT_XLSX,
            "reconcile_md": src["compare_dir"] / "reconcile.md"}
    for k in ("resolve_json", "compare_json", "risk_diff", "reconcile_md"):
        if not src[k].exists():
            raise PublishError(f"{k}: {src[k]} not found")
    src["monitor_dir"] = find_monitor_dir(out_root, resolve_run)
    src |= {"monitor_xlsx": src["monitor_dir"] / "monitor.xlsx", "monitor_md": src["monitor_dir"] / "monitor.md"}
    return src


# ---------------------------------------------------------------- 社一覧.xlsx
def company_rows(resolve: dict, filers: list[dict], by_code: dict[str, tuple[str, str]], updated: date) -> list[list]:
    """One row per resolve row, in the order of the targets; 社名 = targets label."""
    lab = {f["edinetCode"]: f for f in filers}
    rows = []
    for r in resolve["rows"]:
        code = r.get("edinet_code") or r.get("entity_id")
        f = lab.get(code, {})
        sec = f.get("secCode") or by_code.get(code, ("", ""))[0]
        rows.append([sec or None, f.get("label") or r.get("name") or r.get("key"), code, r.get("cur_doc_id"),
                     r.get("cur_period_end"), r.get("prev_doc_id"), r.get("prev_period_end"), updated.isoformat()])
    order = {f["edinetCode"]: i for i, f in enumerate(filers)}
    return sorted(rows, key=lambda x: (order.get(x[2], len(order)), x[2] or ""))


def write_company_list(path: Path, rows: list[list]) -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "社一覧"
    _header(ws, COMPANY_HEADER, {"A": 10, "B": 22, "C": 12, "D": 12, "E": 12, "F": 12, "G": 12, "H": 12})
    for r in rows:
        ws.append(r)
    for c in ws["A"][1:]:
        c.number_format = "@"          # 証券コード as text (e.g. 130A)
    wb.save(path)
    return Path(path)


# ---------------------------------------------------------------- 社別 md
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def md_name(sec: str | None, name: str) -> str:
    """``<証券コード>_<社名>.md``, safe as a Windows / SharePoint file name."""
    return _BAD_NAME.sub("_", f"{sec or 'NA'}_{name}").strip(" .") + ".md"


def _rate(prev: int | None, cur: int | None) -> float | None:
    return None if not prev or cur is None else (cur - prev) / prev


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def _quote(text: str) -> list[str]:
    lines = text.strip("\n").splitlines()
    return [("> " + ln) if ln.strip() else ">" for ln in lines] or ["> （本文なし）"]


def _heads(side: dict | None, idxs: list[int]) -> str:
    if side is None:
        return ""
    by = {it["idx"]: it["heading_raw"] for it in side["items"]}
    return " ＋ ".join(by[i] for i in idxs if i in by)


def filer_stats(f: dict) -> dict:
    pairs = f["pairs"]
    st: dict = {k: sum(p["label"] == k for p in pairs) for k in LABEL_JA}
    st |= {f"change_{k}": sum(p["change"] == k for p in pairs) for k in CHANGE_JA}
    st["review"] = sum(bool(p["review_flag"]) for p in pairs)
    st["prev_chars"] = f["prev"]["chars"] if f["prev"] else None
    st["cur_chars"] = f["cur"]["chars"] if f["cur"] else None
    st["rate"] = _rate(st["prev_chars"], st["cur_chars"])
    st["added_heads"] = [_heads(f["cur"], p["cur_items"]) for p in pairs if p["label"] == "added"]
    st["deleted_heads"] = [_heads(f["prev"], p["prev_items"]) for p in pairs if p["label"] == "deleted"]
    return st


def _source_lines() -> list[str]:
    return ["---", "", *[f"- {k}: {v}" for k, v in SOURCE_NOTICE]]


def company_md(f: dict, sec: str | None, name: str, updated: date, resolve_row: dict | None = None) -> str:
    rr = resolve_row or {}
    out = [f"# {sec or '-'} {name}「事業等のリスク」前期→当期", "",
           f"- 更新日: {updated.isoformat()}",
           f"- EDINETコード: {f.get('edinetCode') or '-'}",
           f"- 当期: {f['cur_doc'] or '-'}（期末 {rr.get('cur_period_end') or '-'}、{f['cur_kind'] or '-'}）",
           f"- 前期: {f['prev_doc'] or '-'}（期末 {rr.get('prev_period_end') or '-'}、{f['prev_kind'] or '-'}）",
           f"- フラグ: {', '.join(f.get('flags') or []) or 'なし'}"]
    if f["skip"]:
        out += [f"- 比較なし: {f['skip']}", "", "前期と当期の組がそろわないため、項目の比較はありません。", ""]
        return "\n".join(out + _source_lines()) + "\n"
    st = filer_stats(f)
    out += [f"- 文字数: 前期 {st['prev_chars']} → 当期 {st['cur_chars']}（{_pct(st['rate'])}）", "",
            "## 変化の概要", "",
            "| " + " | ".join(LABEL_JA.values()) + " | 変更あり | 軽微 | 変更なし | 要確認 |",
            "|" + "---|" * (len(LABEL_JA) + 4),
            "| " + " | ".join(str(st[k]) for k in LABEL_JA)
            + f" | {st['change_changed']} | {st['change_minor']} | {st['change_none']} | {st['review']} |", "",
            "## 項目ごとの前期／当期", "",
            "新設＝当期のみ、削除＝前期のみ。本文は原文のまま（表記の正規化は比較時のみ）。", ""]
    for p in f["pairs"]:
        head = _heads(f["cur"], p["cur_items"]) or _heads(f["prev"], p["prev_items"])
        tag = LABEL_JA[p["label"]] + f"・変更{CHANGE_JA[p['change']]}" + ("・要確認" if p["review_flag"] else "")
        out += [f"### {p['pair_idx'] + 1}. [{tag}] {head}", "",
                f"前期 {len(p['prev_src'])} 字 → 当期 {len(p['cur_src'])} 字", "",
                "#### 前期", "", *(_quote(p["prev_src"]) if p["prev_items"] else ["（なし）"]), "",
                "#### 当期", "", *(_quote(p["cur_src"]) if p["cur_items"] else ["（なし）"]), ""]
    return "\n".join(out + _source_lines()) + "\n"


# ---------------------------------------------------------------- 週次要約.md
def _cell(v) -> str:
    return "-" if v is None or v == "" else str(v).replace("|", "\\|").replace("\n", " ")


def _demote(md: str, by: int = 2) -> list[str]:
    return [("#" * by + ln) if ln.startswith("#") else ln for ln in md.rstrip("\n").splitlines()]


def review_reasons(r: dict, f: dict | None) -> list[str]:
    out = []
    if r.get("cur_reason"):
        out.append(f"突合: {r['cur_reason']}" + (f"（{r['unknown_cause']}）" if r.get("unknown_cause") else ""))
    if r.get("no_prev_reason"):
        out.append(f"前期なし: {r['no_prev_reason']}")
    fl = sorted(set((r.get("cur_flags") or []) + (r.get("prev_flags") or [])))
    if fl:
        out.append("フラグ: " + ", ".join(fl))
    if f is not None and f["skip"]:
        out.append(f"比較なし: {f['skip']}")
    elif f is not None and any(p["review_flag"] for p in f["pairs"]):
        out.append(f"対応付けの要確認 {sum(bool(p['review_flag']) for p in f['pairs'])} 件")
    return out


def weekly_md(updated: date, company: list[list], resolve: dict, compare: dict, md_names: dict[str, str],
              monitor_md: str, reconcile_md: str, monitor_run: str | None) -> str:
    by_code = {f.get("edinetCode"): f for f in compare["filers"]}
    rrow = {r.get("edinet_code") or r.get("entity_id"): r for r in resolve["rows"]}
    out = [f"# 週次要約（更新日 {updated.isoformat()}）", "",
           f"`{RISK_DIFF}`・`{MONITOR}`・`{COMPANY_LIST}` をリンクで読めないときの代替。項目の本文は `{PER_COMPANY}/` の社別 md。",
           f"生成元: resolve run {resolve.get('run_id')}（{resolve.get('scope')}、FY {resolve.get('fy')}、"
           f"基準日 T={resolve.get('as_of')}）、compare run {compare.get('run_id')}、monitor run {monitor_run}。", "",
           "## 1. 対象社一覧", "", "| " + " | ".join(COMPANY_HEADER[:7]) + " |", "|" + "---|" * 7]
    out += ["| " + " | ".join(_cell(v) for v in row[:7]) + " |" for row in company]
    out += ["", "## 2. 変化表", "",
            "文字数増減率 = (当期文字数 − 前期文字数) ÷ 前期文字数（「事業等のリスク」全体）。", "",
            "| 証券コード | 社名 | 新設 | 削除 | 変更あり | 軽微 | 変更なし | 要確認 | 前期文字数 | 当期文字数 | 文字数増減率 | 社別 md |",
            "|" + "---|" * 12]
    details: list[str] = []
    for row in company:
        f = by_code.get(row[2])
        if f is None:
            continue
        md = md_names.get(row[2])
        link = _cell(md and f"{PER_COMPANY}/{md}")
        if f["skip"]:
            out.append(f"| {_cell(row[0])} | {_cell(row[1])} |" + " - |" * 9 + f" {link} |")
            continue
        st = filer_stats(f)
        out.append(f"| {_cell(row[0])} | {_cell(row[1])} | {st['added']} | {st['deleted']} | "
                   f"{st['change_changed']} | {st['change_minor']} | {st['change_none']} | {st['review']} | "
                   f"{st['prev_chars']} | {st['cur_chars']} | {_pct(st['rate'])} | {link} |")
        if st["added_heads"] or st["deleted_heads"]:
            details += [f"### {_cell(row[0])} {row[1]}", ""]
            details += [f"- 新設: {h}" for h in st["added_heads"]] + [f"- 削除: {h}" for h in st["deleted_heads"]] + [""]
    out += ["", "### 新設・削除された項目（見出し）", ""] + (details or ["該当なし", ""])
    out += ["## 3. 要確認リスト", "", "| 証券コード | 社名 | 理由 |", "|---|---|---|"]
    n = len(out)
    for row in company:
        reasons = review_reasons(rrow.get(row[2], {}), by_code.get(row[2]))
        if reasons:
            out.append(f"| {_cell(row[0])} | {_cell(row[1])} | {_cell('; '.join(reasons))} |")
    if len(out) == n:
        out.append("| - | - | 該当なし |")
    out += ["", "## 4. 監視（monitor.md）", "", *_demote(monitor_md), "",
            "## 5. 照合（reconcile.md）", "", *_demote(reconcile_md), "", *_source_lines()]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- build / check
def clean(publish_dir: Path) -> None:
    """Remove last run's canonical files from the local publish/ only (never other files, never the sync folder)."""
    d = Path(publish_dir)
    for n in (RISK_DIFF, MONITOR, COMPANY_LIST, WEEKLY):
        (d / n).unlink(missing_ok=True)
    for p in (d / PER_COMPANY).glob("*.md"):
        p.unlink()


def build(publish_dir: Path, out_root: Path, resolve_run: str, targets: Path, *, compare_run: str | None = None,
          db_path: Path | None = None, today: date | None = None, templates: Path | None = None) -> dict:
    """Writes 社一覧.xlsx, 社別/*.md, 週次要約.md; returns the sources (incl. the two files to rename-copy).

    ``templates``: the Copilot output templates (docs/templates/*.xlsx, copilot-runbook.md 見本ブック) are copied
    to ``publish/templates/`` when the folder exists; optional, not part of the success check.
    """
    today = today or today_jst()
    src = locate(out_root, resolve_run, compare_run)
    resolve = json.loads(src["resolve_json"].read_text(encoding="utf-8"))
    compare = json.loads(src["compare_json"].read_text(encoding="utf-8"))
    filers = load_filers(targets)
    by_code: dict[str, tuple[str, str]] = {}
    if db_path is not None and Path(db_path).exists():
        conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
        try:
            by_code = sec_codes(conn)[0]
        finally:
            conn.close()
    publish_dir = Path(publish_dir)
    (publish_dir / PER_COMPANY).mkdir(parents=True, exist_ok=True)
    clean(publish_dir)
    company = company_rows(resolve, filers, by_code, today)
    write_company_list(publish_dir / COMPANY_LIST, company)
    rrow = {r.get("edinet_code") or r.get("entity_id"): r for r in resolve["rows"]}
    sec = {row[2]: row[0] for row in company}
    name = {row[2]: row[1] for row in company}
    md_names: dict[str, str] = {}
    for f in compare["filers"]:
        code = f.get("edinetCode")
        nm = name.get(code) or f["label"]
        md_names[code] = md_name(sec.get(code), nm)
        (publish_dir / PER_COMPANY / md_names[code]).write_text(
            company_md(f, sec.get(code), nm, today, rrow.get(code)), encoding="utf-8")
    monitor_run = json.loads((src["monitor_dir"] / "monitor.json").read_text(encoding="utf-8")).get("run_id")
    monitor_md = src["monitor_md"].read_text(encoding="utf-8") if src["monitor_md"].exists() else "（monitor.md なし）"
    (publish_dir / WEEKLY).write_text(weekly_md(today, company, resolve, compare, md_names, monitor_md,
                                                src["reconcile_md"].read_text(encoding="utf-8"), monitor_run),
                                      encoding="utf-8")
    copied = []
    if templates is not None and Path(templates).is_dir():
        for t in sorted(Path(templates).glob("*.xlsx")):
            (publish_dir / TEMPLATES).mkdir(exist_ok=True)
            shutil.copyfile(t, publish_dir / TEMPLATES / t.name)
            copied.append(t.name)
    return {"publish_dir": publish_dir, "today": today.isoformat(), "companies": len(company),
            "md": sorted(md_names.values()), "templates": copied, **src,
            "copies": {RISK_DIFF: src["risk_diff"], MONITOR: src["monitor_xlsx"]}}


def _mdate(p: Path) -> date:
    return datetime.fromtimestamp(p.stat().st_mtime).date()


def check(publish_dir: Path, today: date | None = None) -> list[str]:
    """Problems that block the sync (spec §2-5); [] = all five present and today's 社一覧 / monitor."""
    today = today or date.today()           # file times are local; the admin PC runs on JST
    d = Path(publish_dir)
    bad = [f"missing: {n}" for n in (RISK_DIFF, MONITOR, COMPANY_LIST, WEEKLY) if not (d / n).is_file()]
    if not any((d / PER_COMPANY).glob("*.md")):
        bad.append(f"missing: {PER_COMPANY}/*.md")
    for n in (COMPANY_LIST, MONITOR):
        if (d / n).is_file() and _mdate(d / n) != today:
            bad.append(f"not updated today: {n} ({_mdate(d / n).isoformat()})")
    return bad
