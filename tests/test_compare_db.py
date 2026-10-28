"""compare -> SQLite (plan §5, §6-3; M1 step H): extracts / items / matches / sentence_changes.

Synthetic inputs from test_report (no gold needed); the 7 filers run offline on out/m0 + cache when present.
"""
import hashlib
import json
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from edinet_risk import m1_compare
from edinet_risk.__main__ import main
from edinet_risk.diff.sentences import DIFFER_VER, resolve_evidence
from edinet_risk.extract import EXTRACTOR_VER
from edinet_risk.store import db

from test_extract import xbrl, zipped
from test_report import CUR, PREV, TH, _inputs

ROOT = Path(__file__).resolve().parents[1]
DERIVED = ("extracts", "items", "matches", "sentence_changes")


def _db(path: Path, docs) -> Path:
    conn = db.open_db(path)
    for d in docs:
        db.upsert(conn, "documents", {"doc_id": d}, key=["doc_id"])
    conn.close()
    return path


def _rows(path: Path, table: str, order: str) -> list[dict]:
    conn = db.connect(path)
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        conn.close()


def _content_hash(path: Path) -> str:
    """Hash of the derived tables (they carry no run_id / timestamp; M1 acceptance 5a)."""
    conn = db.connect(path)
    try:
        h = hashlib.sha256()
        for t in DERIVED:
            for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3, 4"):
                h.update(json.dumps(list(r), ensure_ascii=False).encode())
        return h.hexdigest()
    finally:
        conn.close()


def _type1(cache: Path, doc_id: str, text: str) -> None:
    html = "".join(f"<p>{escape(ln)}</p>" for ln in text.splitlines())
    (cache / "docs").mkdir(parents=True, exist_ok=True)
    (cache / "docs" / f"{doc_id}_type1.zip").write_bytes(
        zipped({"XBRL/PublicDoc/a.xbrl": xbrl(("BusinessRisksTextBlock", html))}))


def _compare(tmp: Path, dbp: Path | None, run_id: str = "r1") -> dict:
    resolve, m0 = (tmp / "resolve.json", tmp / "m0") if (tmp / "resolve.json").exists() else _inputs(tmp)
    return m1_compare.run(resolve, m0, tmp / "cache", TH, tmp / "out", run_id, db_path=dbp)


def test_compare_writes_items_matches_and_sentence_changes(tmp_path):
    dbp = _db(tmp_path / "t.sqlite", ["C0000001", "P0000001"])
    res = _compare(tmp_path, dbp)
    f = res["data"]["filers"][0]
    assert res["db"] == {"filers": 1, "extracts": 0, "items": 7, "matches": len(f["pairs"]),
                         "sentence_changes": sum(p["n_changes"] for p in f["pairs"]), "skipped": []}

    items = _rows(dbp, "items", "doc_id, item_idx")
    for side, text in (("cur", CUR), ("prev", PREV)):
        rows = [r for r in items if r["doc_id"] == f[side]["doc_id"]]
        # leaves = items whose next item is not deeper; they are the compare.json items (same idx / offsets)
        leaves = [r for k, r in enumerate(rows) if k + 1 == len(rows) or rows[k + 1]["level"] <= r["level"]]
        assert [(r["item_idx"], r["start_off"], r["end_off"], r["heading_norm"]) for r in leaves] == \
            [(it["idx"], it["start_off"], it["end_off"], it["heading_norm"]) for it in f[side]["items"]]
        assert {r["input_sha256"] for r in rows} == {hashlib.sha256(text.encode()).hexdigest()}
        assert {r["segmenter_ver"] for r in rows} == {res["data"]["segmenter_ver"]}

    matches = _rows(dbp, "matches", "pair_idx")
    assert [(json.loads(m["cur_items"]), json.loads(m["prev_items"]), m["label"], m["stage"]) for m in matches] == \
        [(p["cur_items"], p["prev_items"], p["label"], p["stage"]) for p in f["pairs"]]
    assert {m["input_sha256"] for m in matches} == {f["input_sha256"]}
    assert "added" in {m["label"] for m in matches}

    chs = _rows(dbp, "sentence_changes", "pair_idx, change_id")
    assert {c["differ_ver"] for c in chs} == {DIFFER_VER}
    assert {c["op"] for c in chs} == {"add", "mod"}
    for c in chs:  # every evidence ID resolves against the compared text
        if c["cur_evidence_id"]:
            assert resolve_evidence(c["cur_evidence_id"], CUR) == c["text_cur"]
        if c["prev_evidence_id"]:
            assert resolve_evidence(c["prev_evidence_id"], PREV) == c["text_prev"]
    assert any(c["is_minor"] for c in chs) and any(not c["is_minor"] for c in chs)   # 2025年→2026年 is minor

    runs = _rows(dbp, "runs", "run_id")
    assert [(r["run_id"], r["cmd"], r["status"], r["code_version"]) for r in runs] == \
        [("r1", "compare", "ok", m1_compare.COMPARE_VER)]
    assert json.loads(runs[0]["args_json"])["matcher_ver"] == res["data"]["matcher_ver"]


def test_rerun_gives_the_same_content(tmp_path):
    dbp = _db(tmp_path / "t.sqlite", ["C0000001", "P0000001"])
    first = _compare(tmp_path, dbp, "r1")["db"]
    h1 = _content_hash(dbp)
    assert _compare(tmp_path, dbp, "r2")["db"] == first
    assert _content_hash(dbp) == h1
    assert _compare(tmp_path, dbp, "r2")["db"] == first      # same run_id again: the runs row is replaced
    assert [r["run_id"] for r in _rows(dbp, "runs", "run_id")] == ["r1", "r2"]
    conn = db.connect(dbp)
    assert [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in DERIVED[1:]] == \
        [first["items"], first["matches"], first["sentence_changes"]]
    conn.close()


def test_changed_text_replaces_the_rows(tmp_path):
    dbp = _db(tmp_path / "t.sqlite", ["C0000001", "P0000001"])
    _compare(tmp_path, dbp)
    (tmp_path / "m0" / "C0000001_business_risks.txt").write_text(PREV, encoding="utf-8")
    res = _compare(tmp_path, dbp, "r2")
    assert res["db"]["sentence_changes"] == 0
    assert _rows(dbp, "sentence_changes", "change_id") == []
    assert {m["label"] for m in _rows(dbp, "matches", "pair_idx")} == {"same"}
    assert len(_rows(dbp, "items", "doc_id, item_idx")) == 6


def test_extracts_need_the_cached_zip(tmp_path):
    dbp = _db(tmp_path / "t.sqlite", ["C0000001", "P0000001"])
    _type1(tmp_path / "cache", "P0000001", PREV)
    _type1(tmp_path / "cache", "C0000001", "別の本文\n")
    assert _compare(tmp_path, dbp)["db"]["extracts"] == 2
    ex = {r["doc_id"]: r for r in _rows(dbp, "extracts", "doc_id")}
    p = ex["P0000001"]
    assert (p["extractor_ver"], p["source"], p["text"], p["char_len"], p["hit_count"]) == \
        (EXTRACTOR_VER, "xbrl", PREV, len(PREV), 1)
    assert p["input_sha256"] == hashlib.sha256((tmp_path / "cache/docs/P0000001_type1.zip").read_bytes()).hexdigest()
    assert json.loads(p["warnings_json"]) == ["text_from=m0_txt"]
    assert ex["C0000001"]["text"] == CUR       # the compared text is stored; the mismatch is flagged
    assert json.loads(ex["C0000001"]["warnings_json"]) == ["text_from=m0_txt", "text_differs_from_zip_extract"]


def test_missing_documents_rows_skip_the_filer(tmp_path):
    dbp = _db(tmp_path / "t.sqlite", ["C0000001"])
    res = _compare(tmp_path, dbp)
    assert res["db"] == {"filers": 0, "extracts": 0, "items": 0, "matches": 0, "sentence_changes": 0,
                         "skipped": ["テスト自動車"]}
    assert all(_rows(dbp, t, "1") == [] for t in DERIVED)
    assert _rows(dbp, "runs", "run_id")[0]["status"] == "ok"


def test_no_db_file_means_no_write(tmp_path):
    res = _compare(tmp_path, tmp_path / "none.sqlite")
    assert res["db"] is None and not (tmp_path / "none.sqlite").exists()
    assert _compare(tmp_path, None, "r2")["db"] is None


def test_cli_compare_db(tmp_path, capsys):
    resolve, m0 = _inputs(tmp_path)
    out = tmp_path / "out"
    base = ["--log-level", "WARNING", "compare", "--resolve", str(resolve), "--m0", str(m0), "--cache",
            str(tmp_path / "cache"), "--thresholds", str(TH), "--out", str(out)]
    assert main(base + ["--run-id", "a"]) == 2               # one filer has no previous period
    assert "db=" not in capsys.readouterr().out               # no <out>/m1.sqlite -> nothing written
    _db(out / "m1.sqlite", ["C0000001", "P0000001"])
    assert main(base + ["--run-id", "b"]) == 2
    assert f"db={out / 'm1.sqlite'}: filers=1 extracts=0 items=7" in capsys.readouterr().out
    other = _db(tmp_path / "x.sqlite", [])
    assert main(base + ["--run-id", "c", "--db", str(other)]) == 2
    assert "skipped=['テスト自動車']" in capsys.readouterr().out


# ---------------------------------------------------------------- the 7 filers (M0 outputs, offline)
M1 = ROOT / "out" / "m1" / "resolve_transport7.json"


@pytest.mark.skipif(not M1.exists(), reason="out/m1/resolve_transport7.json not present")
def test_transport7_into_sqlite(tmp_path):
    rows = json.loads(M1.read_text(encoding="utf-8"))["rows"]
    docs = sorted({r[k] for r in rows for k in ("cur_doc_id", "prev_doc_id") if r.get(k)})
    if not all((ROOT / "cache" / "docs" / f"{d}_type1.zip").exists() for d in docs):
        pytest.skip("type=1 ZIPs of the 7 filers are not cached")
    dbp = _db(tmp_path / "t.sqlite", docs)
    res = m1_compare.run(M1, ROOT / "out" / "m0", ROOT / "cache", TH, tmp_path, "seven", db_path=dbp)
    fs = [f for f in res["data"]["filers"] if f["skip"] is None]
    assert len(fs) == 7
    c = res["db"]
    assert (c["filers"], c["extracts"], c["skipped"]) == (7, 14, [])
    assert c["matches"] == sum(len(f["pairs"]) for f in fs)
    assert c["sentence_changes"] == sum(p["n_changes"] for f in fs for p in f["pairs"])
    ex = {r["doc_id"]: r for r in _rows(dbp, "extracts", "doc_id")}
    assert all("text_differs_from_zip_extract" not in r["warnings_json"] for r in ex.values())
    for ch in _rows(dbp, "sentence_changes", "change_id"):
        for side in ("cur", "prev"):
            eid = ch[f"{side}_evidence_id"]
            if eid:
                assert resolve_evidence(eid, ex[ch[f"{side}_doc"]]["text"]) == ch[f"text_{side}"]
    h = _content_hash(dbp)
    shutil.rmtree(tmp_path / "run_seven")
    m1_compare.run(M1, ROOT / "out" / "m0", ROOT / "cache", TH, tmp_path, "seven2", db_path=dbp)
    assert _content_hash(dbp) == h
