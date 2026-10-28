"""M2 Step N: `extract` (fetched type=1 bodies -> extracts) and `fetch --include-pending`."""
import io
import json
import zipfile

from edinet_risk.__main__ import main
from edinet_risk.extract import EXTRACTOR_VER
from edinet_risk.ingest import extract_bulk, fetch
from edinet_risk.store import db

NS = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor"
RUN = "resolve-T1"


def xbrl(*bodies: str) -> bytes:
    facts = "".join(f'<jpcrp_cor:BusinessRisksTextBlock contextRef="FilingDateInstant">{b}'
                    f"</jpcrp_cor:BusinessRisksTextBlock>" for b in bodies)
    return f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:jpcrp_cor="{NS}">{facts}' \
           f"</xbrli:xbrl>".encode()


def make_zip(path, member="XBRL/PublicDoc/x.xbrl", data=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, data if data is not None else xbrl("&lt;p&gt;(1) 為替リスク&lt;/p&gt;"))
    path.write_bytes(buf.getvalue())
    return path


def setup(tmp_path, files: dict):
    conn = db.open_db(tmp_path / "t.sqlite")
    with db.transaction(conn):
        for doc, p in files.items():
            db.upsert(conn, "documents", {"doc_id": doc, "edinet_code": "E00001", "entity_id": "E00001",
                                          "xbrl_flag": "1"}, key=["doc_id"])
            if p is not None:
                db.upsert(conn, "fetches", {"doc_id": doc, "type": 1, "status": "ok", "path": str(p),
                                            "sha256": fetch.sha256_file(p) if p.exists() else "x"},
                          key=["doc_id", "type"])
    return conn


def ext(conn):
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM extracts")}


def test_extract_one_hit_and_rerun_keeps(tmp_path):
    conn = setup(tmp_path, {"D1": make_zip(tmp_path / "D1.zip")})
    r = extract_bulk.run(conn)
    assert (r.extracted, r.kept, r.empty, r.multi) == (1, 0, [], [])
    e = ext(conn)["D1"]
    assert e["extractor_ver"] == EXTRACTOR_VER and e["hit_count"] == 1 and "為替リスク" in e["text"]
    assert e["char_len"] == len(e["text"]) and e["input_sha256"] == fetch.sha256_file(tmp_path / "D1.zip")
    r2 = extract_bulk.run(conn)
    assert (r2.extracted, r2.kept) == (0, 1)
    assert extract_bulk.run(conn, force=True).extracted == 1


def test_extract_changed_zip_is_reextracted(tmp_path):
    conn = setup(tmp_path, {"D1": make_zip(tmp_path / "D1.zip")})
    extract_bulk.run(conn)
    make_zip(tmp_path / "D1.zip", data=xbrl("&lt;p&gt;(1) 金利&lt;/p&gt;"))
    conn.execute("UPDATE fetches SET sha256 = ? WHERE doc_id = 'D1'", (fetch.sha256_file(tmp_path / "D1.zip"),))
    assert extract_bulk.run(conn).extracted == 1
    assert "金利" in ext(conn)["D1"]["text"]


def test_extract_empty_multi_and_missing_are_rows(tmp_path):
    conn = setup(tmp_path, {
        "NOTB": make_zip(tmp_path / "a.zip", data=xbrl()),
        "NOPUB": make_zip(tmp_path / "b.zip", member="XBRL/AuditDoc/x.xbrl"),
        "MULTI": make_zip(tmp_path / "c.zip", data=xbrl("&lt;p&gt;一&lt;/p&gt;", "&lt;p&gt;二&lt;/p&gt;")),
        "BAD": (tmp_path / "d.zip"),
        "GONE": (tmp_path / "missing.zip"),
    })
    (tmp_path / "d.zip").write_bytes(b"not a zip")
    r = extract_bulk.run(conn)
    e = ext(conn)
    assert sorted(r.empty) == ["BAD", "NOPUB", "NOTB"] and r.multi == ["MULTI"] and r.missing_file == ["GONE"]
    assert e["NOTB"]["char_len"] == 0 and "no BusinessRisksTextBlock" in json.loads(e["NOTB"]["warnings_json"])
    assert "no XBRL/PublicDoc/*.xbrl" in json.loads(e["NOPUB"]["warnings_json"])
    assert json.loads(e["BAD"]["warnings_json"])[0].startswith("unreadable: BadZipFile")
    assert e["MULTI"]["hit_count"] == 2 and e["MULTI"]["text"].strip() == "一"
    assert "GONE" not in e


def test_extract_cli(tmp_path, capsys):
    conn = setup(tmp_path, {"D1": make_zip(tmp_path / "D1.zip")})
    conn.close()
    assert main(["extract", "--db", str(tmp_path / "t.sqlite")]) == 0
    assert "extracted=1 kept=0 empty=0" in capsys.readouterr().out
    assert main(["extract", "--db", str(tmp_path / "t.sqlite"), "--entity", "E99999"]) == 0
    assert "extracted=0 kept=0" in capsys.readouterr().out


def test_selection_targets_include_pending(tmp_path):
    conn = db.open_db(tmp_path / "t.sqlite")
    db.start_run(conn, RUN, "resolve", code_version="t")
    with db.transaction(conn):
        for d in ("ORIG", "CORR", "PREV"):
            db.upsert(conn, "documents", {"doc_id": d, "edinet_code": "E1", "entity_id": "E1", "xbrl_flag": "1"},
                      key=["doc_id"])
        conn.execute("INSERT INTO filing_selection (run_id, entity_id, role, doc_id, reason_basis_json) "
                     "VALUES (?, 'E1', 'cur', 'CORR', ?)", (RUN, json.dumps({"texts_pending": ["ORIG", "CORR"]})))
        conn.execute("INSERT INTO filing_selection (run_id, entity_id, role, doc_id) VALUES (?, 'E1', 'prev', 'PREV')",
                     (RUN,))
    assert [t.doc_id for t in fetch.selection_targets(conn, RUN)] == ["CORR", "PREV"]
    ts = {t.doc_id: t for t in fetch.selection_targets(conn, RUN, include_pending=True)}
    assert sorted(ts) == ["CORR", "ORIG", "PREV"]
    assert ts["ORIG"].roles == ("cur_pending",) and ts["ORIG"].xbrl_flag == "1" and ts["CORR"].roles == ("cur",)
    assert fetch.selection_targets(conn, RUN, {"E2"}, include_pending=True) == []
