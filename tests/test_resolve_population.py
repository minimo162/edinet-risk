"""resolve for the population (M2 Step F): entity_id, reasons and their basis on a synthetic database (plan §6-5)."""
import json
from datetime import date, timedelta

import pytest

from edinet_risk.__main__ import main
from edinet_risk.config import Resolve
from edinet_risk.ingest import doclist
from edinet_risk.resolve import runner
from edinet_risk.resolve.entity import EntityError, EntityMap, load_successor_map, read_successor_csv
from edinet_risk.resolve.reasons import Target, add_months
from edinet_risk.store import db

T = date(2026, 9, 30)
WIN = runner.fy_window("2026-03")


def rec(doc_id, code, *, ps="2025-04-01", pe="2026-03-31", submit="2026-06-20 10:00", form="030000", dtype="120",
        ordinance="010", xbrl="1", parent=None, sec=None, name=None, jcn=None, seq=1, w="0", desc=None):
    return {"docID": doc_id, "seqNumber": seq, "edinetCode": code, "secCode": sec, "JCN": jcn,
            "filerName": name or f"会社{code}", "ordinanceCode": ordinance, "formCode": form, "docTypeCode": dtype,
            "periodStart": ps, "periodEnd": pe, "submitDateTime": submit, "docDescription": desc,
            "parentDocID": parent, "opeDateTime": None, "withdrawalStatus": w, "docInfoEditStatus": "0",
            "disclosureStatus": "0", "xbrlFlag": xbrl, "csvFlag": xbrl, "legalStatus": "1"}


def prev_year(doc_id, code, **kw):
    kw.setdefault("ps", "2024-04-01")
    kw.setdefault("pe", "2025-03-31")
    kw.setdefault("submit", "2025-06-20 10:00")
    return rec(doc_id, code, **kw)


def registration(doc_id, code, form, submit):
    return rec(doc_id, code, ps=None, pe=None, submit=submit, form=form, dtype="030")


def load(conn, records):
    """One doclist day file per submit date; fetched after T so the days are final."""
    by_day = {}
    for r in records:
        by_day.setdefault((r["submitDateTime"] or "2026-01-01")[:10], []).append(r)
    with db.transaction(conn):
        for day, rs in by_day.items():
            for i, r in enumerate(rs, start=1):
                r["seqNumber"] = i
            doclist.import_day(conn, {"metadata": {"status": "200", "processDateTime": "2026-10-01 00:01"},
                                      "results": rs}, day)
    doclist.merge_documents(conn)


def codes(conn, *cs):
    with db.transaction(conn):
        for c in cs:
            db.upsert(conn, "edinet_codes", {"edinet_code": c, "sec_code5": None, "name": f"会社{c}", "listed": 1},
                      key=["edinet_code"])


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    yield c
    c.close()


@pytest.fixture
def scanned(tmp_path):
    """Every day 2024-04-01..2026-09-30 counts as scanned (empty cached day files)."""
    d = tmp_path / "cache" / "doclist"
    d.mkdir(parents=True)
    day = date(2024, 4, 1)
    while day <= T:
        (d / f"{day.isoformat()}.json").touch()
        day += timedelta(days=1)
    return tmp_path / "cache"


def resolve(conn, targets, cache_dir=None, as_of=T, **kw):
    run_id = f"r{len(conn.execute('SELECT run_id FROM runs').fetchall())}"
    runner.start(conn, run_id, "resolve", fy="2026-03", as_of=as_of, args={})
    ts = [t if isinstance(t, Target) else Target(t, t, None, "test") for t in targets]
    out = runner.resolve_targets(conn, ts, run_id=run_id, as_of=as_of, window=WIN, cfg=Resolve(),
                                 cache_dir=cache_dir, **kw)
    return out, {t.key: r for t, r in out.results}


def basis_row(conn, run_id, entity_id, role):
    r = conn.execute("SELECT * FROM filing_selection WHERE run_id = ? AND entity_id = ? AND role = ?",
                     (run_id, entity_id, role)).fetchone()
    return dict(r), json.loads(r["reason_basis_json"])


# ---------------------------------------------------------------- fiscal years
def test_march_december_and_irregular_years(conn, scanned):
    codes(conn, "E00001", "E00002", "E00003")
    load(conn, [rec("C1", "E00001"), prev_year("P1", "E00001"),
                rec("C2", "E00002", ps="2025-01-01", pe="2025-12-31", submit="2026-03-25 10:00"),
                prev_year("P2", "E00002", ps="2024-01-01", pe="2024-12-31", submit="2025-03-26 10:00"),
                # 9-month transition year (March -> December)
                rec("C3", "E00003", ps="2025-04-01", pe="2025-12-31", submit="2026-03-27 10:00"),
                prev_year("P3", "E00003")])
    out, res = resolve(conn, ["E00001", "E00002", "E00003"], scanned)
    for code, (c, p) in {"E00001": ("C1", "P1"), "E00002": ("C2", "P2"), "E00003": ("C3", "P3")}.items():
        r = res[code]
        assert (r.cur.doc_id, r.prev.doc_id, r.cur_reason, r.no_prev_reason) == (c, p, None, None)
        assert r.basis["gap_days"] == 1
    assert res["E00003"].cur.flags == ["irregular"] and res["E00003"].cur.basis["period_days"] == 275
    s = runner.summarize(out)
    assert s["matched"] == 3 and s["with_prev"] == 3 and s["flags"]["irregular"] == 1
    assert s["flag_lists"]["irregular"] == ["E00003"] and s["unknown"] == 0


def test_add_months_keeps_month_ends():
    assert add_months(date(2025, 3, 31), 3) == date(2025, 6, 30)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2025, 2, 28), 12) == date(2026, 2, 28)
    assert add_months(date(2025, 1, 20), 3) == date(2025, 4, 20)
    assert runner.fy_window("2026-03").period_end_from == date(2025, 4, 1)
    assert runner.fy_window("2025-12").period_end_to == date(2025, 12, 31)


# ---------------------------------------------------------------- entity_id
def test_successor_map_links_previous_code(conn, scanned, tmp_path):
    codes(conn, "E00010", "E00011")
    load(conn, [prev_year("P10", "E00010", sec="11110", name="旧社株式会社"),
                rec("C11", "E00011", sec="11110", name="新社ホールディングス株式会社")])
    _, res = resolve(conn, ["E00011"], scanned)
    r = res["E00011"]
    assert r.prev is None and r.no_prev_reason == "successor_suspected"
    cand = r.basis["reason_basis"]["successor_candidates"]
    assert [(c["edinet_code"], c["matched_on"]) for c in cand] == [("E00010", ["secCode"])]

    csv = tmp_path / "successor_map.csv"
    csv.write_text("prev_code,next_code,reason,registered_by,registered_at\n"
                   "# comment\nE00010,E00011,持株会社化,dev,2026-10-28\n", encoding="utf-8")
    assert load_successor_map(conn, csv)["rows"] == 1
    out, res = resolve(conn, ["E00011"], scanned)
    r = res["E00011"]
    assert (r.cur.doc_id, r.prev.doc_id, r.no_prev_reason) == ("C11", "P10", None)
    assert "entity_chain" not in r.basis["reason_basis"]           # E00011 itself is not remapped
    assert conn.execute("SELECT entity_id FROM documents WHERE doc_id = 'P10'").fetchone()[0] == "E00011"
    assert out.documents_remapped == 1


def test_successor_csv_validation(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("prev_code,next_code,reason,registered_by,registered_at\nE1,E00011,,,\n", encoding="utf-8")
    with pytest.raises(EntityError, match="row 2"):
        read_successor_csv(p)
    p.write_text("a,b\n", encoding="utf-8")
    with pytest.raises(EntityError, match="header"):
        read_successor_csv(p)


def test_code_aggregation_merges_codes(conn, scanned):
    codes(conn, "E00020", "E00021")
    with db.transaction(conn):
        db.upsert(conn, "code_aggregation", {"old_code": "E00020", "new_code": "E00021", "source_date": "2025-09-01"},
                  key=["old_code"])
        db.upsert(conn, "code_aggregation", {"old_code": "E00030", "new_code": "E00039", "source_date": "2023-10-12"},
                  key=["old_code"])
    load(conn, [prev_year("P20", "E00020"), rec("C21", "E00021")])
    out, res = resolve(conn, [Target("2000", "E00020", "旧コードの母集団行", "population:2026-09")], scanned)
    r = res["2000"]
    assert r.entity_id == "E00021" and (r.cur.doc_id, r.prev.doc_id) == ("C21", "P20")
    assert r.basis["reason_basis"]["entity_chain"] == [{"from": "E00020", "to": "E00021", "via": "code_aggregation"}]
    # aggregation target missing from edinet_codes (E38680 -> E38709 in the real lists): followed, noted
    assert {"note": "aggregation_target_not_in_codes", "old_code": "E00030", "new_code": "E00039",
            "decision": "followed (entity_id = new_code)"} in out.entity_notes
    assert EntityMap({"E00030": "E00039"})("E00030") == "E00039"


def test_entity_map_cycle_and_split():
    m = EntityMap({"E1": "E2"}, {"E2": ["E1"], "E5": ["E6", "E7"]})
    assert m("E1") == "E2" and m.basis("E1")[-1]["stopped"] == "cycle"
    assert m("E5") == "E5" and any(n["note"] == "successor_ambiguous" for n in m.notes)


# ---------------------------------------------------------------- 前期なし reasons
def test_new_listing_from_ipo_registration(conn, scanned):
    codes(conn, "E00040", "E00041")
    load(conn, [rec("C40", "E00040"), registration("R40", "E00040", "024000", "2025-08-21 15:30"),
                # 参照方式 (listed company's stock plan) is no listing evidence
                rec("C41", "E00041"), registration("R41", "E00041", "023000", "2025-08-21 15:30")])
    _, res = resolve(conn, ["E00040", "E00041"], scanned)
    assert res["E00040"].no_prev_reason == "new_listing"
    nl = res["E00040"].basis["reason_basis"]["new_listing"]
    assert [x["doc_id"] for x in nl["registration_statements"]] == ["R40"] and nl["prev_filing_days_scanned"]
    assert res["E00041"].no_prev_reason == "unknown"
    assert res["E00041"].basis["reason_basis"]["unknown_cause"] == "no_basis_matched"


def test_reorganization_registration_is_successor_suspected(conn, scanned):
    codes(conn, "E00045")
    load(conn, [rec("C45", "E00045"), registration("R45", "E00045", "027000", "2025-06-06 10:00")])
    _, res = resolve(conn, ["E00045"], scanned)
    rb = res["E00045"].basis["reason_basis"]
    assert res["E00045"].no_prev_reason == "successor_suspected"
    assert [x["doc_id"] for x in rb["reorganization_statements"]] == ["R45"]


def test_prev_gap_and_prev_missing_year(conn, scanned):
    codes(conn, "E00050", "E00051")
    load(conn, [rec("C50", "E00050"), prev_year("P50", "E00050", ps="2024-03-16", pe="2025-03-15"),
                rec("C51", "E00051"), prev_year("P51", "E00051", ps="2023-04-01", pe="2024-03-31",
                                                 submit="2025-07-01 10:00")])
    out, res = resolve(conn, ["E00050", "E00051"], scanned)
    assert res["E00050"].prev.doc_id == "P50" and res["E00050"].flags == ["prev_gap"]
    r = res["E00051"]
    assert r.prev is None and r.no_prev_reason == "prev_missing_year"
    assert r.basis["reason_basis"]["prev_missing_year"] == {"gap_days": 366, "max_days": 31,
                                                           "prev_period_end": "2024-03-31",
                                                           "skipped_prev_doc_id": "P51"}
    _, rb = basis_row(conn, out.run_id, "E00051", "prev")
    assert rb["reason"] == "prev_missing_year" and rb["reason_basis"]["prev_missing_year"]["gap_days"] == 366
    assert runner.summarize(out)["flag_lists"]["prev_gap"] == ["E00050"]


def test_unscanned_days_give_unknown_not_a_guess(conn, tmp_path):
    codes(conn, "E00055", "E00056")
    load(conn, [rec("C55", "E00055"),
                rec("C56", "E00056"), prev_year("P56", "E00056", ps="2023-04-01", pe="2024-03-31",
                                                 submit="2025-07-01 10:00")])
    _, res = resolve(conn, ["E00055", "E00056"], tmp_path / "nocache")
    for code in ("E00055", "E00056"):
        rb = res[code].basis["reason_basis"]
        assert res[code].no_prev_reason == "unknown" and rb["unknown_cause"] == "doclist_not_scanned"
        assert rb["unscanned_days"][0].startswith("2025-04-01")
    assert res["E00056"].basis["reason_basis"]["would_be"] == "prev_missing_year"


def test_prev_not_fetched_when_fetch_failed(conn, scanned):
    codes(conn, "E00060")
    load(conn, [rec("C60", "E00060"), prev_year("P60", "E00060")])
    with db.transaction(conn):
        db.upsert(conn, "fetches", {"doc_id": "P60", "type": 1, "status": "failed", "attempts": 3,
                                    "last_error": "HTTP 404"}, key=["doc_id", "type"])
    _, res = resolve(conn, ["E00060"], scanned)
    r = res["E00060"]
    assert r.prev.doc_id == "P60" and r.no_prev_reason == "prev_not_fetched"
    assert r.basis["reason_basis"]["prev_not_fetched"]["fetch_status"] == "failed"


# ---------------------------------------------------------------- 突合 reasons
def test_form_out_of_scope(conn, scanned):
    codes(conn, "E00070")
    load(conn, [rec("C70", "E00070", ordinance="015", form="080000"), prev_year("P70", "E00070")])
    out, res = resolve(conn, ["E00070"], scanned)
    r = res["E00070"]
    assert r.cur is None and r.cur_reason == "form_out_of_scope"
    assert [d["doc_id"] for d in r.basis["reason_basis"]["out_of_scope_docs"]] == ["C70"]
    row, rb = basis_row(conn, out.run_id, "E00070", "cur")
    assert row["doc_id"] is None and rb["reason"] == "form_out_of_scope"
    assert runner.summarize(out)["cur_reasons"] == {"form_out_of_scope": 1}


def test_not_filed_and_late(conn, scanned):
    codes(conn, "E00080")
    load(conn, [prev_year("P80", "E00080")])
    _, res = resolve(conn, ["E00080"], scanned, as_of=date(2026, 6, 15))
    nf = res["E00080"].basis["reason_basis"]["no_filing"]
    assert res["E00080"].cur_reason == "not_filed"
    assert (nf["expected_period_end"], nf["deadline"]) == ("2026-03-31", "2026-06-30")
    _, res = resolve(conn, ["E00080"], scanned)
    assert res["E00080"].cur_reason == "late"


def test_no_history_without_fiscal_year_end_is_unknown(conn, scanned):
    codes(conn, "E00085")
    load(conn, [registration("R85", "E00085", "027000", "2026-03-05 10:00")])
    _, res = resolve(conn, ["E00085"], scanned)
    rb = res["E00085"].basis["reason_basis"]
    assert res["E00085"].cur_reason == "unknown" and rb["unknown_cause"] == "no_annual_report_history"
    assert [x["doc_id"] for x in rb["no_filing"]["reorganization_statements"]] == ["R85"]


def test_code_mismatch_xbrl_missing_and_fetch_failed(conn, scanned):
    codes(conn, "E00090", "E00091")
    load(conn, [rec("C90", "E00090", xbrl="0"), prev_year("P90", "E00090"),
                rec("C91", "E00091"), prev_year("P91", "E00091")])
    with db.transaction(conn):
        db.upsert(conn, "fetches", {"doc_id": "C91", "type": 1, "status": "failed", "attempts": 5,
                                    "last_error": "ZIP CRC"}, key=["doc_id", "type"])
    out, res = resolve(conn, [Target("9000", None, "コードなし"), Target("9001", "E09999", "一覧にない"),
                              "E00090", "E00091"], scanned)
    assert res["9000"].cur_reason == "code_mismatch" and res["9000"].entity_id == "nocode:9000"
    assert res["9001"].cur_reason == "code_mismatch"
    assert res["E00090"].cur_reason == "xbrl_missing" and "no_xbrl" in res["E00090"].cur.flags
    assert res["E00091"].cur_reason == "fetch_failed" and res["E00091"].cur.doc_id == "C91"
    s = runner.summarize(out)
    assert s["cur_reasons"] == {"code_mismatch": 2, "fetch_failed": 1, "xbrl_missing": 1} and s["matched"] == 0


def test_correction_flags_and_pending_texts(conn, scanned):
    codes(conn, "E00095", "E00096")
    load(conn, [rec("C95", "E00095"), prev_year("P95", "E00095"),
                rec("K95", "E00095", form="030001", dtype="130", ps=None, pe=None, parent="C95",
                    submit="2026-08-01 10:00", xbrl="0"),
                rec("C96", "E00096"), prev_year("P96", "E00096"),
                rec("K96", "E00096", form="030001", dtype="130", ps=None, pe=None, parent="C96",
                    submit="2026-08-01 10:00")])
    out, res = resolve(conn, ["E00095", "E00096"], scanned, text_of=lambda d: None, available=lambda d: False)
    assert res["E00095"].cur.doc_id == "C95" and "correction_unparsed" in res["E00095"].cur.flags
    c96 = res["E00096"].cur
    assert c96.doc_id == "K96" and c96.basis["texts_pending"] == ["C96", "K96"]
    assert "correction_partial" not in c96.flags            # a missing body is no evidence of a partial correction
    assert runner.summarize(out)["texts_pending"] == 1


def test_records_without_edinet_code_are_skipped_and_counted(conn, scanned):
    codes(conn, "E00001")
    r = rec("N1", None, dtype="350", form="010000", ps=None, pe=None)
    load(conn, [rec("C1", "E00001"), prev_year("P1", "E00001"), r])
    out, res = resolve(conn, ["E00001"], scanned)
    assert out.skipped_no_edinet_code == 1 and res["E00001"].cur.doc_id == "C1"


# ---------------------------------------------------------------- population + CLI
def test_cli_resolve_population_and_entity(tmp_path, scanned, capsys):
    path = tmp_path / "t.sqlite"
    c = db.open_db(path)
    codes(c, "E00001", "E00040")
    load(c, [rec("C1", "E00001"), prev_year("P1", "E00001"), rec("C40", "E00040"),
             registration("R40", "E00040", "024000", "2025-08-21 15:30")])
    with db.transaction(c):
        c.executemany("INSERT INTO population (as_of_month, sec_code4, name, market, edinet_code, match_method) "
                      "VALUES ('2026-09', ?, ?, 'プライム（内国株式）', ?, ?)",
                      [("1001", "A社", "E00001", "sec_code"), ("1002", "B社", "E00040", "sec_code"),
                       ("1003", "C社", None, "unmatched")])
    c.close()
    sm = tmp_path / "sm.csv"
    sm.write_text("prev_code,next_code,reason,registered_by,registered_at\n", encoding="utf-8")
    argv = ["--offline", "resolve", "--fy", "2026-03", "--as-of", "2026-09-30", "--db", str(path), "--cache",
            str(scanned), "--out", str(tmp_path / "out"), "--successor-map", str(sm)]
    assert main(argv + ["--run-id", "rp"]) == 0
    o = capsys.readouterr().out
    assert "targets=3 entities=3" in o and "matched(当期採用版あり)=2" in o
    assert "突合の理由: code_mismatch=1" in o and "前期なしの理由: new_listing=1" in o
    data = json.loads((tmp_path / "out" / "resolve_rp" / "resolve.json").read_text(encoding="utf-8"))
    assert data["scope"] == "population:2026-09" and data["unknown"] == 0
    assert (tmp_path / "out" / "resolve_rp" / "resolve.csv").exists()
    assert main(argv + ["--run-id", "re", "--entity", "E00001"]) == 0
    assert "targets=1 entities=1 duplicates=0 matched(当期採用版あり)=1 with_prev=1" in capsys.readouterr().out
    c = db.connect(path)
    assert c.execute("SELECT count(*) FROM filing_selection WHERE run_id = 'rp'").fetchone()[0] == 6
    assert c.execute("SELECT cmd, fy, as_of, status FROM runs WHERE run_id = 're'").fetchone()[:] == \
        ("resolve", "2026-03", "2026-09-30", "ok")
    c.close()
