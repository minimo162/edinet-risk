"""publish helpers for scripts/publish_peer.ps1 (docs/publish_peer.spec.md): targets merge, 社一覧.xlsx,
社別 md, 週次要約.md, the success check, the ``publish`` CLI and static checks of the PowerShell script.

Synthetic: compare/report run on the two-filer texts of test_report; resolve / monitor folders are written
by hand in the shape the CLI writes them.
"""
import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from edinet_risk import m1_compare, m1_report, peer_publish as pp  # noqa: E402
from edinet_risk.__main__ import main  # noqa: E402
from edinet_risk.config import ConfigError, load_settings  # noqa: E402
from edinet_risk.store.db import JST  # noqa: E402

from test_report import CUR, PREV, TH  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "publish_peer.ps1"
RUN = "resolve-20261029T070000-pub"
TODAY = date(2026, 10, 29)


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE edinet_codes (edinet_code TEXT PRIMARY KEY, sec_code5 TEXT, name TEXT, listed INTEGER, "
                 "fetched_at TEXT)")
    conn.executemany("INSERT INTO edinet_codes VALUES (?, ?, ?, 1, NULL)",
                     [("E99999", "72610", "テスト自動車株式会社"), ("E99998", "130A0", "前期なし工業株式会社"),
                      ("E02142", "72010", "日産自動車株式会社")])
    conn.commit()
    conn.close()
    return path


def _targets(tmp: Path) -> Path:
    t = tmp / "targets.toml"
    pp.write_targets(t, [{"label": "テスト自動車", "edinetCode": "E99999", "secCode": "7261", "group": "ピア"},
                         {"label": "前期なし工業", "edinetCode": "E99998", "group": "依頼"}], [])
    return t


def _outputs(tmp: Path) -> Path:
    """<out>/resolve_<RUN>/, <out>/m1/run_<RUN>/ (compare + report), <out>/monitor_*/ like the weekly run."""
    out = tmp / "out"
    m0 = tmp / "m0"
    m0.mkdir()
    (m0 / "P0000001_business_risks.txt").write_text(PREV, encoding="utf-8")
    (m0 / "C0000001_business_risks.txt").write_text(CUR, encoding="utf-8")
    rows = [{"key": "テスト自動車", "name": "テスト自動車", "edinet_code": "E99999", "entity_id": "E99999",
             "cur_doc_id": "C0000001", "cur_period_end": "2026-03-31", "cur_kind": "original", "cur_flags": [],
             "cur_reason": None, "prev_doc_id": "P0000001", "prev_period_end": "2025-03-31", "prev_kind": "original",
             "prev_flags": [], "no_prev_reason": None, "gap_days": 1, "unknown_cause": None},
            {"key": "前期なし工業", "name": "前期なし工業", "edinet_code": "E99998", "entity_id": "E99998",
             "cur_doc_id": "C0000002", "cur_period_end": "2026-03-31", "cur_kind": "original",
             "cur_flags": ["irregular"], "cur_reason": None, "prev_doc_id": None, "prev_period_end": None,
             "prev_kind": None, "prev_flags": [], "no_prev_reason": "new_listing", "gap_days": None,
             "unknown_cause": None}]
    rd = out / f"resolve_{RUN}"
    rd.mkdir(parents=True)
    (rd / "resolve.json").write_text(json.dumps({"run_id": RUN, "scope": "targets:targets.toml", "fy": "2026-03",
                                                 "as_of": "2026-09-30", "rows": rows}, ensure_ascii=False),
                                     encoding="utf-8")
    m1 = [{"label": r["key"], "edinetCode": r["edinet_code"], "cur_doc_id": r["cur_doc_id"],
           "prev_doc_id": r["prev_doc_id"], "cur_kind": r["cur_kind"], "prev_kind": r["prev_kind"],
           "cur_period_end": r["cur_period_end"], "prev_period_end": r["prev_period_end"],
           "cur_flags": r["cur_flags"], "prev_flags": r["prev_flags"], "no_prev_reason": r["no_prev_reason"]}
          for r in rows]
    (rd / "resolve_transport7.json").write_text(json.dumps({"as_of": "2026-09-30", "rows": m1}, ensure_ascii=False),
                                                encoding="utf-8")
    res = m1_compare.run(rd / "resolve_transport7.json", m0, tmp / "cache", TH, out / "m1", RUN)
    m1_report.run(res["run_dir"])
    for name, resolve_run in (("monitor_monitor-old", "resolve-other"), ("monitor_monitor-new", RUN)):
        md = out / name
        md.mkdir()
        (md / "monitor.json").write_text(json.dumps({"run_id": name[8:], "resolve_run": resolve_run}), encoding="utf-8")
        (md / "monitor.md").write_text(f"# 監視レポート（run_id={name[8:]}）\n\n閾値超過なし\n", encoding="utf-8")
        wb = openpyxl.Workbook()
        wb.active.append([name])
        wb.save(md / "monitor.xlsx")
    return out


# ---------------------------------------------------------------- targets
def test_merge_targets_dedupes_and_looks_up_sec_codes(tmp_path):
    a = tmp_path / "a.toml"
    a.write_text('[[filer]]\nlabel = "テスト自動車"\nedinetCode = "E99999"\n\n'
                 '[[period]]\nperiodEnd = "2026-03-31"\nscan_from = "2026-06-10"\nscan_to = "2026-06-30"\n',
                 encoding="utf-8")
    b = tmp_path / "requests.toml"
    b.write_text('[[filer]]\nsecCode = "7201"\n\n[[filer]]\nedinetCode = "E99999"\nlabel = "重複"\n', encoding="utf-8")
    conn = sqlite3.connect(_db(tmp_path / "db.sqlite"))
    filers, periods = pp.merge_targets([a, b], conn)
    assert filers == [{"label": "テスト自動車", "edinetCode": "E99999", "secCode": "7261", "group": "ピア"},
                      {"label": "日産自動車株式会社", "edinetCode": "E02142", "secCode": "7201", "group": "依頼"}]
    out = pp.write_targets(tmp_path / "merged.toml", filers, periods)
    from edinet_risk.m0_multi import load_targets       # resolve / fetch read it this way
    f2, p2 = load_targets(out)
    assert [f["edinetCode"] for f in f2] == ["E99999", "E02142"] and p2 == periods
    b.write_text('[[filer]]\nsecCode = "9999"\n', encoding="utf-8")
    with pytest.raises(pp.PublishError, match="secCode 9999"):
        pp.merge_targets([b], conn)
    b.write_text('[[filer]]\nlabel = "コードなし"\n', encoding="utf-8")
    with pytest.raises(pp.PublishError, match="need edinetCode"):
        pp.merge_targets([b], None)
    with pytest.raises(pp.PublishError, match="not found"):
        pp.merge_targets([tmp_path / "missing.toml"], None)
    b.write_text('[[filer]]\nedinetCode = "E02142"\ngroup = "ピア"\n', encoding="utf-8")   # explicit 区分 wins
    assert pp.merge_targets([a, b], None)[0][1]["group"] == "ピア"
    b.write_text('[[filer]]\nedinetCode = "E02142"\ngroup = "その他"\n', encoding="utf-8")
    with pytest.raises(pp.PublishError, match="group"):
        pp.merge_targets([a, b], None)


def test_merged_targets_without_periods_load(tmp_path):
    from edinet_risk.m0_multi import load_targets
    f, p = load_targets(_targets(tmp_path))
    assert [x["edinetCode"] for x in f] == ["E99999", "E99998"] and p == []


def test_transport7_merges_with_sec_codes_from_db(tmp_path):
    filers, periods = pp.merge_targets([ROOT / "config" / "transport7.toml"], None)
    assert len(filers) == 7 and len(periods) == 2 and all("secCode" not in f for f in filers)
    assert {f["group"] for f in filers} == {"ピア"}


# ---------------------------------------------------------------- build
def test_build_writes_the_three_generated_files(tmp_path):
    out = _outputs(tmp_path)
    pub = tmp_path / "publish"
    (pub / "社別").mkdir(parents=True)
    (pub / "社別" / "古い社.md").write_text("old", encoding="utf-8")
    (pub / "メモ.txt").write_text("keep", encoding="utf-8")            # not ours: never removed
    res = pp.build(pub, out, RUN, _targets(tmp_path), db_path=_db(tmp_path / "db.sqlite"), today=TODAY)
    assert res["companies"] == 2
    assert res["monitor_dir"].name == "monitor_monitor-new"            # the monitor run of this resolve run
    assert res["copies"] == {"risk_diff.xlsx": out / "m1" / f"run_{RUN}" / "risk_diff_transport7.xlsx",
                             "monitor.xlsx": out / "monitor_monitor-new" / "monitor.xlsx"}
    assert (pub / "メモ.txt").exists() and not (pub / "社別" / "古い社.md").exists()

    ws = openpyxl.load_workbook(pub / "社一覧.xlsx").active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    assert rows[0] == list(pp.COMPANY_HEADER)
    assert rows[0][:4] == ["証券コード", "社名", "区分", "EDINETコード"] and rows[0][-2:] == ["基準日T", "更新日"]
    assert rows[1] == ["7261", "テスト自動車", "ピア", "E99999", "C0000001", "2026-03-31", "P0000001", "2025-03-31",
                       "2026-09-30", "2026-10-29"]
    assert rows[2] == ["130A", "前期なし工業", "依頼", "E99998", "C0000002", "2026-03-31", None, None,
                       "2026-09-30", "2026-10-29"]

    assert sorted(p.name for p in (pub / "社別").glob("*.md")) == ["130A_前期なし工業.md", "7261_テスト自動車.md"]
    md = (pub / "社別" / "7261_テスト自動車.md").read_text(encoding="utf-8")
    assert md.startswith("# 7261 テスト自動車「事業等のリスク」前期→当期")
    assert "> 生成AIの利用に伴い、<機密情報>の漏えいや権利侵害が生じる可能性があります。" in md   # 新設 item, verbatim
    assert "[新設・変更あり] (4) AIの活用" in md and "#### 前期\n\n（なし）" in md
    assert "業績に大きな影響を与える" in md and "業績に影響を与える" in md                           # both sides
    assert "| 1 | 2 | 0 | 0 | 0 | 0 | 1 | 0 | 2 | 1 | 1 | 0 |" in md                             # counts, not lists
    assert "出典" in md and "PDL1.0" in md
    skip = (pub / "社別" / "130A_前期なし工業.md").read_text(encoding="utf-8")
    assert "比較なし" in skip and "フラグ: irregular" in skip

    wk = (pub / "週次要約.md").read_text(encoding="utf-8")
    assert wk.startswith("# 週次要約（更新日 2026-10-29、基準日T 2026-09-30）")
    assert "| 7261 | テスト自動車 | ピア | E99999 |" in wk
    cur, prev = len(CUR), len(PREV)
    assert f"| 7261 | テスト自動車 | 1 | 0 | 2 | 1 | 1 | 0 | {prev} | {cur} | {(cur - prev) / prev * 100:+.1f}% | " \
           "社別/7261_テスト自動車.md |" in wk
    assert "- 新設: (4) AIの活用" in wk
    assert "| 130A | 前期なし工業 | 前期なし: new_listing; フラグ: irregular; 比較なし:" in wk
    assert "### 監視レポート（run_id=monitor-new）" in wk                # monitor.md, headings demoted
    assert "### 照合（run_id=" + RUN in wk                                  # reconcile.md
    assert pp.check(pub, TODAY) == ["missing: risk_diff.xlsx", "missing: monitor.xlsx"]


def test_build_without_db_and_missing_sources(tmp_path):
    out = _outputs(tmp_path)
    t = tmp_path / "t.toml"
    pp.write_targets(t, [{"label": "テスト自動車", "edinetCode": "E99999"}], [])
    pp.build(tmp_path / "pub", out, RUN, t, today=TODAY)                     # 証券コード unknown -> NA
    assert (tmp_path / "pub" / "社別" / "NA_テスト自動車.md").exists()
    with pytest.raises(pp.PublishError, match="resolve_json"):
        pp.build(tmp_path / "pub", out, "resolve-none", t, today=TODAY)
    import shutil
    shutil.rmtree(out / "monitor_monitor-new")                                 # only another run's monitor left
    with pytest.raises(pp.PublishError, match="monitor"):
        pp.build(tmp_path / "pub", out, RUN, t, today=TODAY)


def test_build_copies_templates_if_present(tmp_path):
    out = _outputs(tmp_path)
    tpl = tmp_path / "templates"
    tpl.mkdir()
    (tpl / "見本.xlsx").write_bytes(b"x")
    (tpl / "memo.txt").write_bytes(b"x")
    res = pp.build(tmp_path / "pub", out, RUN, _targets(tmp_path), today=TODAY, templates=tpl)
    assert res["templates"] == ["見本.xlsx"] and (tmp_path / "pub" / "templates" / "見本.xlsx").exists()
    assert pp.build(tmp_path / "pub2", out, RUN, _targets(tmp_path), today=TODAY,
                    templates=tmp_path / "none")["templates"] == []


def test_md_name_is_safe_on_windows():
    assert pp.md_name("7203", 'A/B:C*"D"?') == "7203_A_B_C_D_.md"
    assert pp.md_name(None, "社名. ") == "NA_社名.md"


# ---------------------------------------------------------------- check (spec §2-5)
def test_check_requires_five_and_todays_files(tmp_path):
    pub = tmp_path / "publish"
    assert len(pp.check(pub, TODAY)) == 5
    (pub / "社別").mkdir(parents=True)
    for n in ("risk_diff.xlsx", "monitor.xlsx", "社一覧.xlsx", "週次要約.md", "社別/7261_マツダ.md"):
        (pub / n).write_bytes(b"x")
    assert pp.check(pub) == []
    old = (pp.today_jst().toordinal() - 7 - date(1970, 1, 1).toordinal()) * 86400 + 12 * 3600
    os.utime(pub / "monitor.xlsx", (old, old))
    assert [b.split(" (")[0] for b in pp.check(pub)] == ["not updated today: monitor.xlsx"]


def test_check_uses_jst_dates_like_build(tmp_path):
    """A file written at 00:30 JST counts as that JST day (build's today_jst), whatever the local time zone."""
    pub = tmp_path / "publish"
    (pub / "社別").mkdir(parents=True)
    for n in ("risk_diff.xlsx", "monitor.xlsx", "社一覧.xlsx", "週次要約.md", "社別/7261_マツダ.md"):
        (pub / n).write_bytes(b"x")
    t = datetime(2026, 10, 29, 0, 30, tzinfo=JST).timestamp()   # = 2026-10-28 15:30 UTC
    for n in ("monitor.xlsx", "社一覧.xlsx"):
        os.utime(pub / n, (t, t))
    assert pp.check(pub, date(2026, 10, 29)) == []
    assert len(pp.check(pub, date(2026, 10, 28))) == 2


# ---------------------------------------------------------------- CLI
def test_cli_build_check_and_targets(tmp_path, capsys):
    out = _outputs(tmp_path)
    db = _db(tmp_path / "db.sqlite")
    req = tmp_path / "req.toml"
    req.write_text('[[filer]]\nsecCode = "7261"\n\n[[filer]]\nedinetCode = "E99998"\nlabel = "前期なし工業"\n',
                   encoding="utf-8")
    t = tmp_path / "merged.toml"
    assert main(["publish", "targets", "--targets", str(req), "--out", str(t), "--db", str(db)]) == 0
    assert "2 filer(s)" in capsys.readouterr().out
    pub = tmp_path / "publish"
    assert main(["publish", "build", "--out-root", str(out), "--resolve-run", RUN, "--targets", str(t),
                 "--publish-dir", str(pub), "--db", str(db)]) == 0
    res = json.loads(capsys.readouterr().out)                    # ASCII JSON for PowerShell 5.1
    assert res["companies"] == 2 and set(res["copies"]) == {"risk_diff.xlsx", "monitor.xlsx"}
    assert main(["publish", "check", "--publish-dir", str(pub)]) == 2
    assert "NOT OK" in capsys.readouterr().out
    import shutil
    for k, v in res["copies"].items():                            # what publish_peer.ps1 does
        shutil.copyfile(v, pub / k)
    assert main(["publish", "check", "--publish-dir", str(pub)]) == 0
    assert main(["publish", "build", "--out-root", str(out), "--resolve-run", "x", "--targets", str(t),
                 "--publish-dir", str(pub)]) == 2


def test_cli_config_never_prints_the_key(tmp_path, capsys, monkeypatch):
    s = tmp_path / "settings.toml"
    s.write_text('[publish]\ndest_dir = "D:\\\\SharePoint\\\\公開"\ntargets = ["config/transport7.toml", '
                 '"config/requests.toml"]\n', encoding="utf-8")
    monkeypatch.setenv("EDINET_API_KEY", "SECRET-KEY-123")
    assert main(["publish", "config", "--config", str(s)]) == 0
    raw = capsys.readouterr().out
    cfg = json.loads(raw)
    assert "SECRET" not in raw and raw.isascii()
    assert cfg["api_key_source"] == "env" and cfg["dest_dir"] == "D:\\SharePoint\\公開"
    assert cfg["targets"] == ["config/transport7.toml", "config/requests.toml"] and cfg["work_dir"] == "publish"
    monkeypatch.delenv("EDINET_API_KEY")
    kf = tmp_path / "key.txt"
    kf.write_text("FILE-SECRET\n", encoding="utf-8")
    s.write_text(f'[paths]\napi_key_file = "{kf.as_posix()}"\n', encoding="utf-8")
    assert main(["publish", "config", "--config", str(s)]) == 0
    raw = capsys.readouterr().out
    assert "FILE-SECRET" not in raw and json.loads(raw)["api_key_source"] == "file"
    s.write_text("", encoding="utf-8")
    assert main(["publish", "config", "--config", str(s)]) == 0
    assert json.loads(capsys.readouterr().out)["api_key_source"] == "none"


def test_publish_settings(tmp_path):
    s = load_settings(None, env={})
    assert s.publish.targets == ["config/transport7.toml"] and s.publish.dest_dir == ""
    s = load_settings(None, env={"EDINET_RISK__PUBLISH__TARGETS": "a.toml; b.toml", "EDINET_RISK__PUBLISH__DEST_DIR": "X"})
    assert s.publish.targets == ["a.toml", "b.toml"] and s.publish.dest_dir == "X"
    with pytest.raises(ConfigError):
        load_settings(None, env={"EDINET_RISK__PUBLISH__TARGETS": ""})
    p = tmp_path / "s.toml"
    p.write_text("[publish]\ntargets = [1]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="publish.targets"):
        load_settings(p, env={})
    assert load_settings(ROOT / "config" / "settings.example.toml", env={}).publish.out_dir == "out/publish"


# ---------------------------------------------------------------- base date T / doclist range
def _day(cache: Path, d: str, fetched: str) -> None:
    (cache / "doclist").mkdir(parents=True, exist_ok=True)
    (cache / "doclist" / f"{d}.json").write_text(json.dumps({"metadata": {"processDateTime": f"{fetched} 09:00"},
                                                             "results": []}), encoding="utf-8")


def test_doclist_range_catches_up_a_skipped_week(tmp_path):
    cache = tmp_path / "cache"
    for i in range(1, 30):                                       # 09-30..10-28 final (fetched the next day)
        d = date.fromordinal(TODAY.toordinal() - i)
        _day(cache, d.isoformat(), date.fromordinal(d.toordinal() + 1).isoformat())
    assert pp.doclist_range(cache, TODAY, 8, 62) == (date(2026, 10, 22), TODAY, False)   # nothing missing: 8 days
    for i in range(10, 22):                                      # last week's run skipped (10-10..10-21 missing)
        (cache / "doclist" / f"2026-10-{i}.json").unlink()
    _day(cache, "2026-10-10", "2026-10-10")                      # ... and 10-10 fetched on the day (not final)
    assert pp.doclist_range(cache, TODAY, 8, 62) == (date(2026, 10, 10), TODAY, False)   # from the day after 10-09
    assert pp.doclist_range(cache, TODAY, 8, 15) == (date(2026, 10, 15), TODAY, True)     # capped at max_days
    assert pp.doclist_warnings(date(2026, 10, 15), True, 15) and not pp.doclist_warnings(TODAY, False, 15)
    assert pp.doclist_range(tmp_path / "empty", TODAY, 8, 8) == (date(2026, 10, 22), TODAY, False)


def test_as_of_is_today_unless_fixed():
    assert pp.as_of_date("today", TODAY) == TODAY
    assert pp.as_of_date("2026-09-30", TODAY) == date(2026, 9, 30)
    assert pp.as_of_warnings(TODAY, date(2026, 9, 30), date(2026, 9, 30))           # past the submit window
    assert not pp.as_of_warnings(date(2026, 9, 30), date(2026, 9, 30), date(2026, 9, 30))
    w = pp.as_of_warnings(date(2027, 3, 1), date(2026, 9, 30), date(2027, 6, 30))
    assert len(w) == 1 and "run.as_of" in w[0]


def test_submit_to_is_extended_to_t_only_for_today():
    end = date(2026, 9, 30)
    assert pp.effective_submit_to("today", TODAY, end) == (TODAY, True)
    assert pp.effective_submit_to("today", date(2026, 9, 1), end) == (end, False)
    assert pp.effective_submit_to("2026-10-29", TODAY, end) == (end, False)       # fixed T: keep (and warn)
    assert not pp.as_of_warnings(TODAY, date(2026, 9, 30), pp.effective_submit_to("today", TODAY, end)[0])


def test_cli_config_gives_as_of_and_doclist_range(tmp_path, capsys):
    s = tmp_path / "settings.toml"
    cache = tmp_path / "cache"
    s.write_text(f'[paths]\ncache_dir = "{cache.as_posix()}"\n', encoding="utf-8")
    assert main(["publish", "config", "--config", str(s), "--today", "2026-10-29"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert (cfg["as_of"], cfg["run_as_of"], cfg["today"]) == ("2026-10-29", "2026-09-30", "2026-10-29")
    assert (cfg["doclist_from"], cfg["doclist_to"]) == ("2026-08-29", "2026-10-29")      # empty cache: max 62 days
    assert (cfg["submit_to"], cfg["submit_to_extended"]) == ("2026-10-29", True)        # as_of=today: extended
    assert len(cfg["warnings"]) == 1 and "doclist_max_days" in cfg["warnings"][0]       # no submit_to warning
    s.write_text(f'[paths]\ncache_dir = "{cache.as_posix()}"\n[publish]\nas_of = "2026-09-30"\n'
                 'doclist_max_days = 8\n', encoding="utf-8")
    assert main(["publish", "config", "--config", str(s), "--today", "2026-10-29"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert (cfg["as_of"], cfg["doclist_from"], cfg["warnings"]) == ("2026-09-30", "2026-10-22", [])
    assert (cfg["submit_to"], cfg["submit_to_extended"]) == ("2026-09-30", False)
    s.write_text(f'[paths]\ncache_dir = "{cache.as_posix()}"\n[publish]\nas_of = "2026-10-29"\n'
                 'doclist_max_days = 8\n', encoding="utf-8")
    assert main(["publish", "config", "--config", str(s), "--today", "2026-10-29"]) == 0
    cfg = json.loads(capsys.readouterr().out)                    # fixed T past submit_to: not extended, warned
    assert cfg["submit_to_extended"] is False and len(cfg["warnings"]) == 1 and "submit_to" in cfg["warnings"][0]
    for bad in ('as_of = "yesterday"', "doclist_max_days = 3"):
        s.write_text(f"[publish]\n{bad}\n", encoding="utf-8")
        assert main(["publish", "config", "--config", str(s)]) == 2


# ---------------------------------------------------------------- scripts/publish_peer.ps1 (static)
def test_ps1_is_powershell51_safe():
    raw = PS1.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 reads BOM-less UTF-8 as the ANSI code page"
    text = raw.decode("utf-8-sig")
    code = "\n".join(ln.split("#")[0] for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for bad in ("??", "?.", " && ", " || ", "-Parallel", "ConvertFrom-Json -AsHashtable"):   # PowerShell 7 only
        assert bad not in code, bad
    assert re.search(r"param\(\s*\[switch\]\$DryRun", text)
    assert "EDINET_API_KEY" in text and not re.search(r"EDINET_API_KEY\s*=", code)      # never set a key
    # overwrite only; the one exception removes stale 社別\*.md in the sync folder (Remove-StaleMd)
    fn = code[code.index("function Remove-StaleMd"):]
    fn = fn[:fn.index("\n}\n")]
    assert code.count("Remove-Item") == fn.count("Remove-Item") == 1
    assert '-Filter *.md' in fn and 'Join-Path $DestDir "社別"' in fn and "$keep -notcontains" in fn


def _cli_options(cmd: list[str], capsys) -> str:
    with pytest.raises(SystemExit):
        main([*cmd, "--help"])
    return capsys.readouterr().out


def test_ps1_uses_existing_cli_options(capsys):
    """Every option the script passes to a subcommand exists in that subcommand's argparse parser."""
    text = PS1.read_text(encoding="utf-8-sig")
    lines, buf = [], ""
    for ln in text.splitlines():                 # join array continuation lines ("...,\n  ...")
        buf += ln.strip() + " "
        if not ln.rstrip().endswith(","):
            lines.append(buf)
            buf = ""
    nested = {"publish", "doclist", "codes", "population"}
    seen = set()
    for ln in lines:
        m = re.search(r'@\("([a-z][a-z-]*)"(?:,\s*"([a-z][a-z-]*)")?', ln)
        if not m:
            continue
        cmd = [m.group(1)] + ([m.group(2)] if m.group(1) in nested and m.group(2) else [])
        helptext = _cli_options(cmd, capsys)
        opts = re.findall(r'"(--[a-z][a-z-]*)"', ln) + (["--config"] if "$CfgArgs" in ln else [])
        for o in opts:
            assert re.search(rf"(?<![\w-]){re.escape(o)}(?![\w-])", helptext), f"{' '.join(cmd)} has no {o}"
        seen.add(" ".join(cmd))
    assert {"publish config", "publish targets", "doclist sync", "resolve", "fetch", "extract", "compare", "report",
            "monitor", "publish build", "publish check"} <= seen, seen
    assert re.search(r'@\("fetch",[^)]*"--targets"', text)     # fetch refuses to run unscoped without --all


def test_ps1_reads_existing_json_keys(tmp_path, capsys):
    """Every ``$cfg.<key>`` / ``$built.<key>`` the script reads is in the `publish config` / `publish build` JSON."""
    text = PS1.read_text(encoding="utf-8-sig")
    s = tmp_path / "settings.toml"
    s.write_text("", encoding="utf-8")
    assert main(["publish", "config", "--config", str(s)]) == 0
    cfg = json.loads(capsys.readouterr().out)
    out = _outputs(tmp_path)
    assert main(["publish", "build", "--out-root", str(out), "--resolve-run", RUN, "--targets", str(_targets(tmp_path)),
                 "--publish-dir", str(tmp_path / "pub")]) == 0
    built = json.loads(capsys.readouterr().out)
    for var, obj in (("cfg", cfg), ("built", built)):
        keys = set(re.findall(rf"\${var}\.([A-Za-z_]+)", text))
        assert keys, var
        assert keys <= set(obj), f"${var}: {keys - set(obj)} not in the JSON"
    for k in re.findall(r'\$built\.copies\."([^"]+)"', text):
        assert k in built["copies"], k


def _ps1_code() -> str:
    text = PS1.read_text(encoding="utf-8-sig")
    return "\n".join(ln.split("#")[0] for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _joined(code: str) -> list[str]:
    lines, buf = [], ""
    for ln in code.splitlines():
        buf += ln.strip() + " "
        if not ln.rstrip().endswith(","):
            lines.append(buf.strip())
            buf = ""
    return lines


def test_ps1_weekly_resolve_uses_as_of_and_doclist_range():
    """Both resolves get --as-of from publish config (today by default); doclist uses the computed range."""
    code = _ps1_code()
    assert re.search(r"\$AsOf\s*=\s*\$cfg\.as_of", code)
    resolves = [ln for ln in _joined(code) if re.match(r'Invoke-Cli "resolve', ln)]
    assert len(resolves) == 2 and all('"--as-of", $AsOf' in ln for ln in resolves), resolves
    doclist = next(ln for ln in _joined(code) if ln.startswith('Invoke-Cli "doclist"'))
    assert '"--from", $cfg.doclist_from' in doclist and '"--to", $cfg.doclist_to' in doclist
    assert "Get-Date).AddDays" not in code
    assert re.search(r"foreach \(\$w in @\(\$cfg\.warnings\)\).*?\$Script:Warnings\.Add", code, re.S)


def test_ps1_gate_missing_settings_or_dest_fails():
    """Not -DryRun / -NoSync: no settings file or empty dest_dir throws (exit 1) before any CLI step runs."""
    code = _ps1_code()
    m = re.search(r"if \(-not \$DryRun -and -not \$NoSync\) \{(.*?)\n    \}\n", code, re.S)
    assert m, "settings / dest gate not found"
    gate = m.group(1)
    assert re.search(r"if \(-not \$cfg\.settings\) \{\s*throw", gate)
    assert re.search(r"if \(-not \$Dest\) \{\s*throw", gate)
    assert code.index(m.group(0)) < code.index('Invoke-Cli "targets"')
    assert code.index("$Dest = $cfg.dest_dir") < code.index(m.group(0))


def test_ps1_gate_doclist_failure_blocks_publish():
    """doclist sync: any non-zero exit (incl. 2) throws -> exit 1, no sync (spec §2-3)."""
    code = _ps1_code()
    line = next(ln for ln in code.splitlines() if re.search(r'Invoke-Cli\s+"doclist"', ln))
    assert "-WarnCodes" not in line, line
    assert re.search(r'catch \{.*?\$exit = 1', code, re.S)


def test_ps1_gate_fetch_zero_new_with_errors_blocks_publish():
    """fetch exit 2 is a warning only when something new came in; the gate runs right after fetch."""
    code = _ps1_code()
    lines = [ln.strip() for ln in code.splitlines() if ln.strip()]
    i = next(i for i, ln in enumerate(lines) if ln.startswith('Invoke-Cli "fetch"'))
    assert lines[i + 1] == "Assert-FetchGate"
    gate = code[code.index("function Assert-FetchGate"):]
    gate = gate[:gate.index("\n}\n")]
    assert "$Script:LastRc -ne 0 -and $new -eq 0" in gate and "throw" in gate
    assert "[int]$m[1] + [int]$m[2]" in gate                    # fetched + adopted_from_cache
    # no summary line + non-zero exit (config / DB error before fetching) also throws
    assert re.search(r"\$null -eq \$m\).*?LastRc -ne 0.*?throw", gate, re.S)


def test_ps1_fetch_summary_regex_matches_the_cli(tmp_path, capsys, monkeypatch):
    """$FetchSummary in the script parses the real `fetch` summary line."""
    from test_fetch import _no_network, make_zip, setup_db
    m = re.search(r"\$FetchSummary\s*=\s*'([^']+)'", PS1.read_text(encoding="utf-8-sig"))
    assert m, "$FetchSummary not found"
    rx = re.compile(m.group(1))
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    setup_db(tmp_path).close()
    (tmp_path / "cache" / "docs").mkdir(parents=True)
    (tmp_path / "cache" / "docs" / "D0000001_type1.zip").write_bytes(make_zip())
    _no_network(monkeypatch)
    assert main(["fetch", "--db", str(tmp_path / "t.sqlite"), "--cache", str(tmp_path / "cache"),
                 "--offline", "--entity", "E00001"]) == 0
    hits = [rx.search(ln) for ln in capsys.readouterr().out.splitlines()]
    hits = [h for h in hits if h]
    assert len(hits) == 1 and tuple(map(int, hits[0].groups())) == (0, 1, 0)


def _block(code: str, start: str) -> str:
    s = code[code.index(start):]
    return s[:s.index("\n}\n")]


def test_ps1_sync_copies_company_list_last_with_retry():
    """F: 社一覧.xlsx is copied last (the 'done' mark), locked files are retried, the copied list is logged."""
    code = _ps1_code()
    assert re.search(r'\$CompanyList = "社一覧\.xlsx"', code)
    sync = code[code.index("$rest = @($files | Where-Object { $_ -ne $CompanyList })"):]
    sync = sync[:sync.index("} finally {")]
    i_rest = sync.index("foreach ($rel in $rest)")
    i_stale = sync.index("Remove-StaleMd $Work $Dest")
    i_list = sync.index("Copy-Over (Join-Path $Work $CompanyList) (Join-Path $Dest $CompanyList)")
    assert i_rest < i_stale < i_list
    assert re.search(r"\} finally \{\s*Write-Log \(\"コピー済み", code)
    copy = _block(code, "function Copy-Over")
    assert "for ($i = 1; ; $i++)" in copy and "Start-Sleep -Seconds $CopyRetrySec" in copy
    assert "$i -ge $CopyRetries" in copy and "throw" in copy


def test_ps1_json_helper_logs_stderr():
    """G: publish config / build failures keep their reason (stderr) in the log."""
    fn = _block(_ps1_code(), "function Invoke-Json")
    assert "2>&1" in fn
    assert "-is [System.Management.Automation.ErrorRecord]" in fn and 'Write-Log ("    stderr: "' in fn
    assert "-isnot [System.Management.Automation.ErrorRecord]" in fn          # only stdout goes to ConvertFrom-Json
    code = _ps1_code()                      # lines before the log file exists (publish config) are kept and flushed
    assert "$Script:Pending.Add($Text)" in _block(code, "function Write-Log")
    assert "Add-Content -LiteralPath $Script:Log -Value $Script:Pending" in _block(code, "function Open-Log")
    assert re.search(r"catch \{.*?-not \$Script:Log\).*?Open-Log", code, re.S)


def test_ps1_dest_inside_work_is_rejected():
    """M: dest_dir equal to / inside work_dir (or the reverse) throws before any CLI step."""
    code = _ps1_code()
    fn = _block(code, "function Test-SameOrUnder")
    assert "OrdinalIgnoreCase" in fn and "Get-FullDir $Child" in fn
    m = re.search(r"if \(\$Dest -and \(\(Test-SameOrUnder \$Dest \$Work\) -or \(Test-SameOrUnder \$Work \$Dest\)\)\) "
                  r"\{\s*throw", code)
    assert m and code.index(m.group(0)) < code.index('Invoke-Cli "targets"')
    assert "TrimEnd('\\') + '\\'" in code     # a\b is not "inside" a\bc


def test_ps1_compare_report_exit2_partial_vs_no_input(tmp_path, capsys):
    """J: exit 2 with the summary line = some filers only (warn); exit 2 without it = no input (fail)."""
    code = _ps1_code()
    lines = [ln.strip() for ln in _joined(code) if ln.strip()]
    for step, var in (("compare", "$CompareSummary"), ("report", "$ReportSummary")):
        i = next(i for i, ln in enumerate(lines) if ln.startswith(f'Invoke-Cli "{step}"'))
        assert lines[i + 1] == f'Assert-Partial "{step}" {var}', lines[i + 1]
    fn = _block(code, "function Assert-Partial")
    assert "-not $hit" in fn and "throw" in fn
    text = PS1.read_text(encoding="utf-8-sig")
    rx = {k: re.compile(re.search(rf"\${k}\s*=\s*'([^']+)'", text).group(1)) for k in ("CompareSummary", "ReportSummary")}
    out = _outputs(tmp_path)
    rd = out / f"resolve_{RUN}"
    m0 = tmp_path / "m0"
    args = ["compare", "--resolve", str(rd / "resolve_transport7.json"), "--m0", str(m0), "--cache",
            str(tmp_path / "cache"), "--thresholds", str(TH), "--out", str(tmp_path / "m1b"), "--run-id", "x"]
    assert main(args) == 2                                         # 前期なし工業 cannot be compared
    assert any(rx["CompareSummary"].search(ln) for ln in capsys.readouterr().out.splitlines())
    assert main(["report", "--run", str(tmp_path / "m1b" / "run_x")]) in (0, 2)
    assert any(rx["ReportSummary"].search(ln) for ln in capsys.readouterr().out.splitlines())
    args[2] = str(tmp_path / "none.json")
    assert main(args) == 2                                         # no input
    assert not any(rx["CompareSummary"].search(ln) for ln in capsys.readouterr().out.splitlines())
    assert main(["report", "--run", str(tmp_path / "none")]) == 2
    assert not any(rx["ReportSummary"].search(ln) for ln in capsys.readouterr().out.splitlines())


def test_ps1_monitor_breach_and_fail_on_threshold(tmp_path, capsys):
    """I: monitor runs without --fail-on-threshold (its exit 2 also means input error); breaches are read from
    the summary line; -FailOnThreshold (default off) turns them into exit 3 after publishing."""
    from test_monitor import make_db, run_monitor
    code = _ps1_code()
    assert re.search(r"\[switch\]\$FailOnThreshold", code)
    lines = [ln.strip() for ln in _joined(code) if ln.strip()]
    i = next(i for i, ln in enumerate(lines) if ln.startswith('Invoke-Cli "monitor"'))
    assert "--fail-on-threshold" not in lines[i] and "-WarnCodes" not in lines[i] and lines[i + 1] == "Read-MonitorBreach"
    assert re.search(r"if \(\$FailOnThreshold -and \$Script:Breached -gt 0\) \{.*?\$exit = 3", code, re.S)
    rx = re.compile(re.search(r"\$MonitorSummary\s*=\s*'([^']+)'", PS1.read_text(encoding="utf-8-sig")).group(1))
    make_db(tmp_path).close()
    assert run_monitor(tmp_path) == 0
    hits = [rx.search(ln) for ln in capsys.readouterr().out.splitlines()]
    assert [int(h.group(1)) for h in hits if h] == [0]


# ---------------------------------------------------------------- Copilot 見本ブック / 統合プロンプト
SHEETS = ["対象確認", "変化表", "差分詳細", "分析", "マツダ比較", "要確認"]


def test_template_has_six_sheets_with_autofilter():
    wb = openpyxl.load_workbook(ROOT / "docs" / "templates" / "yoY_report_template.xlsx")
    assert wb.sheetnames == ["使い方", *SHEETS]
    for n in SHEETS:
        assert (wb[n].auto_filter.ref or "").startswith("A5:"), n


def test_runbook_prompt_maps_every_cli_label():
    from edinet_risk.report.excel import LABEL_JA
    text = (ROOT / "docs" / "copilot-runbook.md").read_text(encoding="utf-8")
    prompt = text[text.index("## 統合プロンプト"):text.index("## 回答がおかしいとき")]
    table = prompt[prompt.index("【変更種別の対応"):]
    for ja in LABEL_JA.values():
        assert ja in table, ja
    assert " → ".join(SHEETS).replace(" → ", "` → `") in prompt
    assert "再計算しない" in prompt and "4節" not in text and "4シート" not in text
