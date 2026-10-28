"""resolve.versions / resolve.period / ingest.doclist on synthetic doclist records (plan §6-5)."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from edinet_risk.config import Resolve, Window
from edinet_risk.ingest import doclist
from edinet_risk.resolve.period import period_days, resolve_entity, save_resolution
from edinet_risk.resolve.versions import (FilingFilter, build_groups, merge_state, select_version, t_bound)
from edinet_risk.store import db

T = date(2026, 9, 30)
CUR_WIN = Window(date(2025, 4, 1), date(2026, 3, 31), date(2025, 4, 1), date(2026, 9, 30))
FLT = FilingFilter()


def rec(doc_id, *, code="E00001", form="030000", dtype="120", ps="2025-04-01", pe="2026-03-31",
        submit="2026-06-20 10:00", xbrl="1", parent=None, seq=1, ope=None, w="0", disc="0", legal="1",
        edit="0", desc=None, fetched="2026-10-01T00:00:00+09:00"):
    return {"docID": doc_id, "seqNumber": seq, "edinetCode": code, "ordinanceCode": "010", "formCode": form,
            "docTypeCode": dtype, "periodStart": ps, "periodEnd": pe, "submitDateTime": submit,
            "docDescription": desc, "parentDocID": parent, "opeDateTime": ope, "withdrawalStatus": w,
            "docInfoEditStatus": edit, "disclosureStatus": disc, "xbrlFlag": xbrl, "csvFlag": xbrl,
            "legalStatus": legal, "_fetched_at": fetched}


def correction(doc_id, parent, submit, **kw):
    kw.setdefault("ps", None)
    kw.setdefault("pe", None)
    return rec(doc_id, form="030001", dtype="130", parent=parent, submit=submit, **kw)


def masked(doc_id, **kw):
    """Withdrawn filing as re-listed by EDINET: status 2, metadata blanked."""
    r = rec(doc_id, w="2", legal="0", **kw)
    for k in ("edinetCode", "formCode", "docTypeCode", "ordinanceCode", "periodStart", "periodEnd",
              "submitDateTime", "parentDocID", "xbrlFlag"):
        r[k] = None
    return r


def letter(doc_id, parent, submit):
    """取下書 (withdrawalStatus=1) pointing at the withdrawn filing."""
    return rec(doc_id, form=None, dtype=None, ps=None, pe=None, submit=submit, parent=parent, w="1", legal="0")


def group(records, as_of=T, key=("E00001", "2026-03-31")):
    return build_groups(merge_state(records, as_of=as_of), FLT)[key]


def texts(mapping):
    return lambda d: mapping.get(d)


BODY = "当社グループの事業等のリスクは次のとおりです。" * 20


# ---------------------------------------------------------------- versions
def test_single_original_adopted():
    s = select_version(group([rec("S1")]), T)
    assert (s.doc_id, s.original_doc_id, s.version_kind, s.flags) == ("S1", "S1", "original", [])
    assert s.basis["texts_checked"] is False


def test_correction_period_inherited_from_parent_and_adopted():
    st = merge_state([rec("S1"), correction("S2", "S1", "2026-08-01 09:00")], as_of=T)
    assert (st["S2"]["periodEnd"], st["S2"]["_period_basis"]) == ("2026-03-31", "parent:S1")
    s = select_version(build_groups(st, FLT)[("E00001", "2026-03-31")], T,
                       text_of=texts({"S1": BODY, "S2": BODY}))
    assert (s.doc_id, s.original_doc_id, s.version_kind) == ("S2", "S1", "correction")
    assert s.basis["risk_changed_by_correction"] is False and s.flags == []


def test_correction_period_from_description_when_parent_unknown():
    st = merge_state([correction("S2", "S0", "2026-08-01 09:00",
                                 desc="訂正有価証券報告書－第10期(2025/04/01－2026/03/31)")])
    assert (st["S2"]["periodStart"], st["S2"]["periodEnd"], st["S2"]["_period_basis"]) == \
        ("2025-04-01", "2026-03-31", "description")


def test_risk_changed_by_correction_flag():
    g = group([rec("S1"), correction("S2", "S1", "2026-08-01 09:00")])
    s = select_version(g, T, text_of=texts({"S1": BODY, "S2": BODY + "新たなリスクを追加しました。"}))
    assert s.doc_id == "S2" and "risk_changed_by_correction" in s.flags


def test_whitespace_only_change_is_not_a_risk_change():
    g = group([rec("S1"), correction("S2", "S1", "2026-08-01 09:00")])
    s = select_version(g, T, text_of=texts({"S1": BODY, "S2": BODY.replace("。", "。\n ")}))
    assert "risk_changed_by_correction" not in s.flags


def test_correction_partial_short_text_falls_back_to_original():
    g = group([rec("S1"), correction("S2", "S1", "2026-08-01 09:00")])
    s = select_version(g, T, text_of=texts({"S1": BODY, "S2": "（訂正前）…（訂正後）…"}))
    assert (s.doc_id, s.version_kind) == ("S1", "original")
    assert "correction_partial" in s.flags
    assert s.basis["partial_corrections"][0]["doc_id"] == "S2"


def test_correction_partial_without_textblock_uses_latest_full_version():
    g = group([rec("S1"), correction("S2", "S1", "2026-07-01 09:00"), correction("S3", "S1", "2026-08-01 09:00")])
    s = select_version(g, T, text_of=texts({"S1": BODY, "S2": BODY + "追記", "S3": None}))
    assert s.doc_id == "S2" and "correction_partial" in s.flags
    assert "risk_changed_by_correction" in s.flags


def test_correction_without_xbrl_is_unparsed():
    g = group([rec("S1"), correction("S2", "S1", "2026-08-01 09:00", xbrl="0")])
    s = select_version(g, T)
    assert s.doc_id == "S1" and "correction_unparsed" in s.flags
    assert s.basis["unparsed_corrections"] == ["S2"]


def test_correction_after_T_is_ignored():
    g = group([rec("S1"), correction("S2", "S1", "2026-10-15 09:00")])
    s = select_version(g, T)
    assert s.doc_id == "S1" and s.basis["excluded"] == {"S2": "submitted_after_T"}


def test_withdrawn_version_excluded():
    old = rec("S1", fetched="2026-07-01T00:00:00+09:00")
    recs = [old, rec("S2", submit="2026-06-25 10:00"),
            masked("S1", fetched="2026-10-01T00:00:00+09:00"), letter("S9", "S1", "2026-06-24 09:00")]
    st = merge_state(recs, as_of=T)
    assert st["S1"]["withdrawalStatus"] == "2" and st["S1"]["periodEnd"] == "2026-03-31"  # metadata kept
    s = select_version(build_groups(st, FLT)[("E00001", "2026-03-31")], T)
    assert s.doc_id == "S2" and s.original_doc_id == "S2"
    assert s.basis["excluded"] == {"S1": "withdrawn"}


def test_withdrawal_after_T_not_applied():
    recs = [rec("S1", fetched="2026-07-01T00:00:00+09:00"), masked("S1", fetched="2026-11-01T00:00:00+09:00"),
            letter("S9", "S1", "2026-10-20 09:00")]
    s = select_version(group(recs), T)
    assert s.doc_id == "S1" and s.basis["state_notes"] == {"S1": ["withdrawal_after_T"]}
    later = date(2026, 10, 31)
    s2 = select_version(group(recs, as_of=later), later)
    assert s2.doc_id is None and "no_valid_version" in s2.flags


def test_withdrawal_without_letter_is_applied_conservatively():
    recs = [rec("S1", fetched="2026-07-01T00:00:00+09:00"), masked("S1", fetched="2026-11-01T00:00:00+09:00")]
    s = select_version(group(recs), T)
    assert s.doc_id is None and s.basis["state_notes"] == {"S1": ["withdrawal_time_unknown"]}


def test_not_disclosed_and_expired_excluded():
    g = group([rec("S1", disc="2"), rec("S2", submit="2026-06-21 10:00", legal="0"),
               rec("S3", submit="2026-06-22 10:00")])
    s = select_version(g, T)
    assert s.doc_id == "S3" and s.basis["excluded"] == {"S1": "not_disclosed", "S2": "legal_expired"}


def test_metadata_edit_after_T_ignored():
    recs = [rec("S1", edit="2"), rec("S1", seq=5, ope="2026-10-10 12:00", edit="1", pe="2026-02-28")]
    assert merge_state(recs)["S1"]["periodEnd"] == "2026-02-28"            # latest known state
    assert merge_state(recs, as_of=T)["S1"]["periodEnd"] == "2026-03-31"   # state at T


def test_filter_excludes_other_forms_and_codes():
    st = merge_state([rec("S1"), rec("S2", form="043000"), rec("S3", dtype="140")])
    assert [v.doc_id for vs in build_groups(st, FLT).values() for v in vs] == ["S1"]


def test_filing_filter_from_repo_toml():
    f = FilingFilter.from_toml(Path(__file__).resolve().parents[1] / "config" / "filing_filter.toml")
    assert f.is_original(rec("S1")) and f.is_version(correction("S2", "S1", "x"))
    assert not f.is_version(rec("S3", form="043000"))


def test_t_bound_forms():
    assert t_bound(date(2026, 9, 30)) == t_bound("2026-09-30") == "2026-09-30 23:59"
    assert t_bound("2026-09-30T10:00") == "2026-09-30 10:00"


# ---------------------------------------------------------------- period
def two_years(prev_pe="2025-03-31", prev_ps="2024-04-01", **cur_kw):
    return build_groups(merge_state([rec("C1", **cur_kw),
                                     rec("P1", ps=prev_ps, pe=prev_pe, submit="2025-06-20 10:00")], as_of=T), FLT)


def test_prev_normal():
    r = resolve_entity(two_years(), "E00001", CUR_WIN, T)
    assert (r.cur.doc_id, r.prev.doc_id, r.no_prev_reason, r.flags) == ("C1", "P1", None, [])
    assert r.basis["gap_days"] == 1 and r.cur.flags == [] and r.prev.flags == []


def test_prev_gap_flag():
    r = resolve_entity(two_years(prev_pe="2025-03-15", prev_ps="2024-03-16"), "E00001", CUR_WIN, T)
    assert r.prev.doc_id == "P1" and r.flags == ["prev_gap"] and r.basis["gap_days"] == 17


def test_prev_missing_year_not_compared():
    g = build_groups(merge_state([rec("C1"), rec("P0", ps="2023-04-01", pe="2024-03-31",
                                                 submit="2024-06-20 10:00")]), FLT)
    r = resolve_entity(g, "E00001", CUR_WIN, T)
    assert r.prev is None and r.no_prev_reason == "prev_missing_year"
    assert r.basis["skipped_prev_doc_id"] == "P0" and r.basis["gap_days"] == 366


def test_irregular_period_change():
    # FY change: previous period ended 2024-12-31, current runs 15 months to 2026-03-31
    g = two_years(prev_pe="2024-12-31", prev_ps="2024-01-01", ps="2025-01-01")
    r = resolve_entity(g, "E00001", CUR_WIN, T)
    assert r.prev.doc_id == "P1" and "irregular" in r.cur.flags and "irregular" not in r.prev.flags
    assert r.cur.basis["period_days"] == period_days("2025-01-01", "2026-03-31") == 455


def test_no_previous_group():
    g = build_groups(merge_state([rec("C1")]), FLT)
    r = resolve_entity(g, "E00001", CUR_WIN, T)
    assert r.cur.doc_id == "C1" and r.prev is None and r.no_prev_reason == "unknown"


def test_prev_all_withdrawn_is_prev_not_fetched():
    recs = [rec("C1"), rec("P1", ps="2024-04-01", pe="2025-03-31", submit="2025-06-20 10:00",
                            fetched="2025-07-01T00:00:00+09:00"),
            masked("P1"), letter("S9", "P1", "2025-07-10 09:00")]
    r = resolve_entity(build_groups(merge_state(recs, as_of=T), FLT), "E00001", CUR_WIN, T)
    assert r.prev.doc_id is None and r.no_prev_reason == "prev_not_fetched"


def test_no_current_filing():
    r = resolve_entity(two_years(), "E99999", CUR_WIN, T)
    assert r.cur is None and r.basis["cur_missing"]


# ---------------------------------------------------------------- ingest + store
def day_file(records, processed="2026-10-01 00:01"):
    return {"metadata": {"status": "200", "processDateTime": processed},
            "results": [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]}


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    yield c
    c.close()


def test_import_cache_idempotent_and_merge(conn, tmp_path):
    d = tmp_path / "cache" / "doclist"
    d.mkdir(parents=True)
    (d / "2026-06-20.json").write_text(json.dumps(day_file([rec("S1", seq=1), rec("X1", code="E00002", seq=2)])))
    (d / "2026-08-01.json").write_text(json.dumps(day_file([correction("S2", "S1", "2026-08-01 09:00", seq=1)])))
    st1 = doclist.import_cache(conn, tmp_path / "cache")
    st2 = doclist.import_cache(conn, tmp_path / "cache")
    assert st1["files"] == 2 and st1["documents"] == 3
    assert conn.execute("SELECT count(*) FROM doclist_raw").fetchone()[0] == 3 == st2["records"]
    row = conn.execute("SELECT period_end, fetched_at FROM documents d JOIN doclist_raw r USING (doc_id) "
                       "WHERE doc_id = 'S2'").fetchone()
    assert tuple(row) == ("2026-03-31", "2026-10-01T00:01:00+09:00")   # period inherited from parent
    assert {r["docID"] for r in doclist.load_records(conn, ["E00001"])} == {"S1", "S2"}


def test_load_records_keeps_masked_records_of_selected_codes(conn):
    with db.transaction(conn):
        doclist.import_day(conn, day_file([rec("S1")], "2026-07-01 00:01"), "2026-06-20")
        doclist.import_day(conn, day_file([masked("S1")], "2026-10-01 00:01"), "2026-06-20")
    recs = doclist.load_records(conn, ["E00001"])
    assert [r["withdrawalStatus"] for r in recs] == ["0", "2"]
    assert merge_state(recs)["S1"]["edinetCode"] == "E00001"


def test_save_resolution_rows(conn, tmp_path):
    d = tmp_path / "cache" / "doclist"
    d.mkdir(parents=True)
    recs = [rec("C1", seq=1), rec("P1", seq=2, ps="2024-04-01", pe="2025-03-31", submit="2025-06-20 10:00"),
            correction("P2", "P1", "2026-06-22 10:00", seq=3)]
    (d / "2026-06-20.json").write_text(json.dumps(day_file(recs)))
    doclist.import_cache(conn, tmp_path / "cache")
    groups = build_groups(merge_state(doclist.load_records(conn, ["E00001"]), as_of=T), FLT)
    res = resolve_entity(groups, "E00001", CUR_WIN, T, Resolve(), text_of=texts({"P1": BODY, "P2": BODY}))
    db.start_run(conn, "r1", "test", code_version="t")
    with db.transaction(conn):
        save_resolution(conn, "r1", res)
    rows = {r["role"]: dict(r) for r in conn.execute("SELECT * FROM filing_selection WHERE run_id = 'r1'")}
    assert rows["cur"]["doc_id"] == "C1" and rows["cur"]["version_kind"] == "original"
    assert (rows["prev"]["doc_id"], rows["prev"]["original_doc_id"], rows["prev"]["version_kind"]) == \
        ("P2", "P1", "correction")
    assert json.loads(rows["prev"]["flags_json"]) == []
    assert json.loads(rows["prev"]["reason_basis_json"])["risk_changed_by_correction"] is False


# ---------------------------------------------------------------- offline regression on the saved M0 doclists
REPO = Path(__file__).resolve().parents[1]
EXPECTED7 = {  # (cur, prev) as selected from the saved doclists at T=2026-09-30
    "E02144": ("S100Y8NY", "S100VWVY"), "E02166": ("S100YDD9", "S100VYOD"), "E02142": ("S100YH2B", "S100W2UQ"),
    "E02152": ("S100YFKZ", "S100YF9D"), "E02163": ("S100YDZ1", "S100VZ6C"), "E02213": ("S100YCMC", "S100VZMJ"),
    "E02167": ("S100YFG2", "S100W4MT")}


def _window_dates():
    out = []
    for start in (date(2025, 6, 10), date(2026, 6, 10)):
        out += [(start + timedelta(days=i)).isoformat() for i in range(21)]
    return out


@pytest.mark.skipif(not (REPO / "cache" / "doclist" / "2026-06-22.json").exists(), reason="M0 doclist cache absent")
def test_transport7_selection_from_saved_doclists(conn):
    stats = doclist.import_cache(conn, REPO / "cache", _window_dates())
    assert not stats["missing"]
    groups = build_groups(merge_state(doclist.load_records(conn, EXPECTED7), as_of=T), FLT)
    got = {}
    for code in EXPECTED7:
        r = resolve_entity(groups, code, CUR_WIN, T)
        got[code] = (r.cur.doc_id, r.prev.doc_id)
        assert r.no_prev_reason is None and r.flags == [] and r.basis["gap_days"] == 1
    assert got == EXPECTED7
    sub = resolve_entity(groups, "E02152", CUR_WIN, T)
    assert (sub.prev.original_doc_id, sub.prev.version_kind) == ("S100W1AE", "correction")
