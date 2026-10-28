"""compare / report (plan §3 report/, §4 M1-4/5/7, §8-1 report, §8-2 snapshot) on synthetic and M0 inputs.

Gold from a manual Excel is out of scope for M1 (not needed): gold-dependent columns are checked for the explicit
``対象外`` state, and tools/gold_import.py only for its interface / error. Idempotency is checked on
cell-value + style snapshots and HTML text, never on the xlsx binary.
"""
import importlib.util
import json
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from edinet_risk import m1_compare, m1_report  # noqa: E402
from edinet_risk.__main__ import main  # noqa: E402
from edinet_risk.report import excel, html, reconcile  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TH = ROOT / "config" / "thresholds.toml"

PREV = """(1) 為替の変動
当社グループは海外売上の比率が高く、為替の変動が業績に影響を与える可能性があります。
対応として為替予約を行っています。
(2) 原材料の価格
鋼材や樹脂などの原材料の価格が上昇した場合、2025年3月期の利益が減少する可能性があります。
(3) 品質問題
大規模なリコールが発生した場合、多額の費用が発生する可能性があります。
"""
CUR = """(1) 為替の変動
当社グループは海外売上の比率が高く、為替の変動が業績に大きな影響を与える可能性があります。
対応として為替予約を行っています。
(2) 原材料の価格
鋼材や樹脂などの原材料の価格が上昇した場合、2026年3月期の利益が減少する可能性があります。
(3) 品質問題
大規模なリコールが発生した場合、多額の費用が発生する可能性があります。
(4) AIの活用
生成AIの利用に伴い、<機密情報>の漏えいや権利侵害が生じる可能性があります。
"""


def _inputs(tmp: Path, prev: str = PREV, cur: str = CUR) -> tuple[Path, Path]:
    m0 = tmp / "m0"
    m0.mkdir()
    (m0 / "P0000001_business_risks.txt").write_text(prev, encoding="utf-8")
    (m0 / "C0000001_business_risks.txt").write_text(cur, encoding="utf-8")
    resolve = tmp / "resolve.json"
    resolve.write_text(json.dumps({"as_of": "2026-09-30", "rows": [
        {"label": "テスト自動車", "edinetCode": "E99999", "cur_doc_id": "C0000001", "prev_doc_id": "P0000001",
         "cur_kind": "original", "prev_kind": "original", "cur_period_end": "2026-03-31",
         "prev_period_end": "2025-03-31", "cur_flags": [], "prev_flags": [], "no_prev_reason": None},
        {"label": "前期なし工業", "edinetCode": "E99998", "cur_doc_id": "C0000002", "prev_doc_id": None,
         "cur_kind": "original", "prev_kind": None, "no_prev_reason": "new_listing"},
    ]}, ensure_ascii=False), encoding="utf-8")
    return resolve, m0


def _run(tmp: Path, run_id: str, **kw) -> dict:
    resolve, m0 = (tmp / "resolve.json", tmp / "m0") if (tmp / "resolve.json").exists() else _inputs(tmp)
    res = m1_compare.run(resolve, m0, tmp / "cache", TH, tmp / "out", run_id)
    return m1_report.run(res["run_dir"], **kw) | {"data": res["data"]}


def _sheet(snap, name):
    return {c: runs for s, c, runs in snap if s == name}


# ---------------------------------------------------------------- end to end (synthetic)
def test_compare_report_end_to_end(tmp_path):
    r = _run(tmp_path, "t1")
    f = r["data"]["filers"][0]
    assert [p["label"] for p in f["pairs"]] == ["modified", "modified", "same", "added"]
    assert [p["change"] for p in f["pairs"]] == ["changed", "minor", "none", "changed"]
    assert f["pairs"][1]["minor_kind"] == "number"
    assert r["data"]["filers"][1]["skip"].startswith("no pair")
    assert r["reconcile"] == {"rows": 4, "fulltext_ok": 2, "fulltext_rows": 2, "gold_ok": 0, "gold_missing": 2}
    out = r["out_dir"]
    for name in ("risk_diff_transport7.xlsx", "reconcile.csv", "reconcile.md", "html/index.html"):
        assert (out / name).exists()
    snap = excel.snapshot(r["xlsx"])
    assert {s for s, _, _ in snap} == {"概要", "テスト自動車", "照合", "出典"}


def test_rich_text_colors(tmp_path):
    r = _run(tmp_path, "t1")
    sh = _sheet(excel.snapshot(r["xlsx"]), "テスト自動車")
    cols = {k: i for i, k in enumerate(excel.PAIR_HEADER)}
    L, R = (openpyxl.utils.get_column_letter(cols[k] + 1) for k in
            ("前期の本文（青字＝前期のみ）", "当期の本文（赤字下線＝当期のみ）"))
    # pair 1: "大きな" is current-only -> red + underline on the right; the left has no coloured run
    right = sh[f"{R}2"]
    assert ("大きな", excel.RED, "single", False) in right
    assert all(col is None for _, col, _, _ in sh[f"{L}2"])
    # pair 2: 2025 -> 2026: previous-only digit blue on the left, current-only digit red underlined
    assert any(col == excel.BLUE and "5" in t for t, col, _, _ in sh[f"{L}3"])
    assert any(col == excel.RED and u == "single" and "6" in t for t, col, u, _ in sh[f"{R}3"])
    # added item: whole text red underlined, left empty; text kept as-is (no escaping in Excel)
    assert f"{L}5" not in sh
    assert "".join(t for t, *_ in sh[f"{R}5"]).startswith("(4) AIの活用")
    assert any("<機密情報>" in t and col == excel.RED for t, col, _, _ in sh[f"{R}5"])
    # label / stage / scores in their own columns (M1-7)
    row2 = {excel.PAIR_HEADER[openpyxl.utils.column_index_from_string(c[:-1]) - 1]: v[0][0]
            for c, v in sh.items() if c.endswith("2") and c[:-1].isalpha() and len(c) <= 3}
    assert row2["ラベル"] == "修正" and row2["段階"] == "anchor" and row2["変更"] == "あり"
    assert isinstance(row2["S"], float) and 0 < row2["S"] <= 1


def test_source_sheet_and_html(tmp_path):
    r = _run(tmp_path, "t1")
    src = _sheet(excel.snapshot(r["xlsx"]), "出典")
    text = "".join(t for runs in src.values() for t, *_ in runs)
    assert "EDINET" in text and "PDL1.0" in text and "加工" in text and "run_id=t1" in text
    page = (r["out_dir"] / "html" / r["html"]["テスト自動車"]).read_text(encoding="utf-8")
    assert "<ins>大きな</ins>" in page and "<del>5</del>" in page and "<ins>6</ins>" in page
    assert "&lt;機密情報&gt;" in page and "<機密情報>" not in page
    assert "PDL1.0" in page
    assert "前期なし工業" in (r["out_dir"] / "html" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- cell limit (M1-4)
def test_long_pairs_are_split_under_the_cell_limit(tmp_path):
    long_prev = "(1) 長い項目\n" + "".join(f"第{i}文は前期の文章です。\n" for i in range(60))
    long_cur = "(1) 長い項目\n" + "".join(f"第{i}文は当期の文章です。\n" for i in range(60))
    resolve, m0 = _inputs(tmp_path, long_prev, long_cur)
    res = m1_compare.run(resolve, m0, tmp_path / "cache", TH, tmp_path / "out", "x")
    r = m1_report.run(res["run_dir"], cell_max=200)
    sh = _sheet(excel.snapshot(r["xlsx"]), "テスト自動車")
    bodies = {"L": [], "N": []}
    for c, runs in sorted(sh.items(), key=lambda kv: (kv[0][0], int(kv[0][1:]))):
        if not all(isinstance(t, str) for t, *_ in runs):
            continue
        text = "".join(t for t, *_ in runs)
        assert excel.cell_len(text) <= 200
        if c[0] in bodies and c != f"{c[0]}1":
            bodies[c[0]].append(text)
    assert len(bodies["L"]) > 3 and any(c.startswith("O") for c in sh)      # continuation rows "k/n"
    p = res["data"]["filers"][0]["pairs"][0]
    norm = reconcile.norm_notation
    assert norm("".join(bodies["L"])) == norm(p["prev_src"])
    assert norm("".join(bodies["N"])) == norm(p["cur_src"])


def test_chunk_splits_a_single_overlong_block_and_counts_utf16():
    blk = {"gap_prev": "", "gap_cur": "", "segments": [["equal", "𠮷" * 7], ["cur_only", "あ" * 5]]}
    rows = excel.chunk_pair([blk], limit=6)
    for pr, cr in rows:
        assert sum(excel.cell_len(t) for _, t in pr) <= 6
        assert sum(excel.cell_len(t) for _, t in cr) <= 6
    assert "".join(t for pr, _ in rows for _, t in pr) == "𠮷" * 7
    assert "".join(t for _, cr in rows for _, t in cr) == "𠮷" * 7 + "あ" * 5


def test_rich_and_clean():
    assert excel.rich([("equal", "a"), ("equal", "b")]) == "ab"
    assert excel.rich([("equal", "")]) is None
    rt = excel.rich([("equal", "a\x01"), ("cur_only", "b"), ("cur_only", "c")])
    assert str(rt) == "abc" and len(rt) == 2


# ---------------------------------------------------------------- idempotency (M1-5)
def test_two_runs_give_the_same_snapshot_and_html(tmp_path):
    a = _run(tmp_path, "RUNA")
    b = _run(tmp_path, "RUNB")
    assert excel.snapshot(a["xlsx"], {"RUNA": "<RUN>"}) == excel.snapshot(b["xlsx"], {"RUNB": "<RUN>"})
    for name in ["index.html", *a["html"].values()]:
        ha = (a["out_dir"] / "html" / name).read_text(encoding="utf-8").replace("RUNA", "<RUN>")
        hb = (b["out_dir"] / "html" / name).read_text(encoding="utf-8").replace("RUNB", "<RUN>")
        assert ha == hb
    ja, jb = (json.loads((r["out_dir"] / "compare.json").read_text(encoding="utf-8")) for r in (a, b))
    assert ja.pop("run_id") == "RUNA" and jb.pop("run_id") == "RUNB" and ja == jb
    # report again over the same run: identical snapshot
    again = m1_report.run(a["out_dir"])
    assert excel.snapshot(again["xlsx"]) == excel.snapshot(a["xlsx"])


# ---------------------------------------------------------------- reconcile / gold
def test_reconcile_with_gold_dict(tmp_path):
    r = _run(tmp_path, "g")
    data = r["data"]
    gold = {"P0000001": {"n_items": 3, "first": "(1) 為替の変動", "last": "(3) 品質問題"},
            "C0000001": {"n_items": 3, "first": "(1) 為替の変動", "last": "(3) 品質問題"}}
    rs = {row[2]: row for row in reconcile.rows(data, gold)}
    assert rs["P0000001"][13] == "一致"
    assert rs["C0000001"][13] == "不一致(項目数,末尾)"
    assert rs["C0000001"][6:9] == (4, "(1) 為替の変動", "(4) AIの活用")


def test_reconcile_flags_lost_text(tmp_path):
    r = _run(tmp_path, "z")
    f = r["data"]["filers"][0]
    f["pairs"][0]["blocks"][0]["segments"][0][1] = "書き換え"
    assert reconcile.side_check(f, "prev")[0] is False
    f["pairs"] = f["pairs"][1:]
    ok, notes = reconcile.side_check(f, "cur")
    assert not ok and any("対応表" in n for n in notes)


def _gold_mod():
    spec = importlib.util.spec_from_file_location("gold_import", ROOT / "tools" / "gold_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gold_import_not_received(tmp_path, capsys):
    g = _gold_mod()
    with pytest.raises(g.GoldNotReceived, match="未受領"):
        g.convert(None)
    with pytest.raises(g.GoldNotReceived):
        g.convert(tmp_path / "nope.xlsx")
    assert g.main(["--excel", str(tmp_path / "nope.xlsx"), "--out", str(tmp_path / "g.json")]) == 2
    assert "未受領" in capsys.readouterr().err
    assert not (tmp_path / "g.json").exists()
    x = tmp_path / "manual.xlsx"
    openpyxl.Workbook().save(x)
    with pytest.raises(g.GoldLayoutUnknown):
        g.convert(x)
    assert g.main(["--excel", str(x)]) == 3


# ---------------------------------------------------------------- CLI
def test_cli_compare_and_report(tmp_path, capsys):
    resolve, m0 = _inputs(tmp_path)
    out = tmp_path / "out"
    args = ["--log-level", "WARNING", "compare", "--resolve", str(resolve), "--m0", str(m0), "--cache",
            str(tmp_path / "cache"), "--thresholds", str(TH), "--out", str(out), "--run-id", "cli"]
    assert main(args) == 2                     # one filer has no previous period
    assert main(["--log-level", "WARNING", "report", "--run", str(out / "run_cli")]) == 0
    assert "全文一致 2/2" in capsys.readouterr().out
    assert main(["--log-level", "WARNING", "report", "--run", str(tmp_path / "missing")]) == 2


# ---------------------------------------------------------------- the 7 filers (M0 outputs, offline)
M1 = ROOT / "out" / "m1" / "resolve_transport7.json"


@pytest.mark.skipif(not M1.exists(), reason="out/m1/resolve_transport7.json not present")
def test_transport7_compare_report(tmp_path):
    res = m1_compare.run(M1, ROOT / "out" / "m0", ROOT / "cache", TH, tmp_path, "seven")
    done = [f for f in res["data"]["filers"] if f["skip"] is None]
    if len(done) < 7:
        pytest.skip("some texts are not available offline: "
                    + "; ".join(f["skip"] for f in res["data"]["filers"] if f["skip"]))
    r = m1_report.run(res["run_dir"])
    assert r["reconcile"]["fulltext_ok"] == r["reconcile"]["fulltext_rows"] == 14
    snap = excel.snapshot(r["xlsx"])
    assert len({s for s, _, _ in snap}) == 10              # 概要 + 7 filers + 照合 + 出典
    assert max(excel.cell_len(t) for _, _, runs in snap for t, *_ in runs if isinstance(t, str)) <= excel.CELL_MAX
    assert len(r["html"]) == 7
    again = m1_report.run(m1_compare.run(M1, ROOT / "out" / "m0", ROOT / "cache", TH, tmp_path, "seven2")["run_dir"])
    assert excel.snapshot(again["xlsx"], {"seven2": "<RUN>"}) == excel.snapshot(r["xlsx"], {"seven": "<RUN>"})
