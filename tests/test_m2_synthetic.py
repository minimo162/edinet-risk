"""M2 acceptance 4 (plan §4 M2-4, §6-5): synthetic doclists through the whole offline resolve path.

Each case goes doclist day files -> ``doclist_raw`` (append-only snapshots) -> ``documents`` merge ->
``runner.resolve_targets`` (T-state, version groups, previous period, reasons) -> ``filing_selection``,
and is checked on the saved rows. Unit-level checks of the same rules: test_resolve.py (versions /
period), test_doclist_sync.py (re-listing order, state_as_of), test_resolve_population.py (reasons).

| M2-4 item                                         | test                                         |
|---------------------------------------------------|----------------------------------------------|
| withdrawalStatus=1/2 excluded                     | test_withdrawn_versions_excluded, test_only_* |
| not disclosed excluded                            | test_not_disclosed_excluded                  |
| docInfoEditStatus=1 re-listing: newer record wins | test_edit_relisting_adopted                  |
| correction submitted after T excluded             | test_correction_after_T_excluded             |
| withdrawal after T does not affect the T-state    | test_withdrawal_after_T_not_applied          |
| xbrlFlag=0 corrections only -> previous version   | test_unparsed_correction_uses_previous_...   |
| 702001 is a version                               | test_xbrl_fix_702001_is_a_version            |
| TextBlock missing / fragment -> correction_partial | test_partial_correction_*                   |
| previous year missing -> prev_missing_year        | test_prev_missing_year_not_compared          |
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from edinet_risk.config import Resolve
from edinet_risk.ingest import doclist
from edinet_risk.resolve import runner
from edinet_risk.resolve.reasons import Target
from edinet_risk.resolve.versions import FilingFilter
from edinet_risk.store import db

T = date(2026, 9, 30)
WIN = runner.fy_window("2026-03")
CODE = "E00001"
BODY = "当社グループの事業等のリスクは次のとおりです。" * 40
FILTER_TOML = Path(__file__).resolve().parents[1] / "config" / "filing_filter.toml"
FINAL = "2026-10-01 00:01"      # processDateTime of a snapshot taken after T


def rec(doc_id, *, ps="2025-04-01", pe="2026-03-31", submit="2026-06-20 10:00", form="030000", dtype="120",
        xbrl="1", parent=None, w="0", disc="0", legal="1", edit="0", ope=None, desc=None):
    return {"docID": doc_id, "seqNumber": 1, "edinetCode": CODE, "secCode": "10010", "JCN": None,
            "filerName": "テスト株式会社", "ordinanceCode": "010", "formCode": form, "docTypeCode": dtype,
            "periodStart": ps, "periodEnd": pe, "submitDateTime": submit, "docDescription": desc,
            "parentDocID": parent, "opeDateTime": ope, "withdrawalStatus": w, "docInfoEditStatus": edit,
            "disclosureStatus": disc, "xbrlFlag": xbrl, "csvFlag": xbrl, "legalStatus": legal}


def prev_year(doc_id, **kw):
    kw.setdefault("ps", "2024-04-01")
    kw.setdefault("pe", "2025-03-31")
    kw.setdefault("submit", "2025-06-20 10:00")
    return rec(doc_id, **kw)


def correction(doc_id, parent, submit, form="030001", **kw):
    return rec(doc_id, ps=None, pe=None, form=form, dtype="130", parent=parent, submit=submit, **kw)


def masked(doc_id, submit):
    """A withdrawn filing as EDINET re-lists it: withdrawalStatus=2, metadata blanked."""
    r = rec(doc_id, w="2", legal="0", submit=submit)
    for k in ("edinetCode", "secCode", "formCode", "docTypeCode", "ordinanceCode", "periodStart", "periodEnd",
              "submitDateTime", "parentDocID", "xbrlFlag"):
        r[k] = None
    return r


def letter(doc_id, parent, submit):
    """取下書 (withdrawalStatus=1). It carries the withdrawn filing's form, so it would form a version."""
    return rec(doc_id, submit=submit, parent=parent, w="1", legal="0")


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    with db.transaction(c):
        db.upsert(c, "edinet_codes", {"edinet_code": CODE, "sec_code5": "10010", "name": "テスト株式会社",
                                      "listed": 1}, key=["edinet_code"])
    yield c
    c.close()


@pytest.fixture
def scanned(tmp_path):
    """Every day 2024-04-01..2026-10-31 counts as scanned (empty cached day files)."""
    d = tmp_path / "cache" / "doclist"
    d.mkdir(parents=True)
    day = date(2024, 4, 1)
    while day <= date(2026, 10, 31):
        (d / f"{day.isoformat()}.json").touch()
        day += timedelta(days=1)
    return tmp_path / "cache"


def snap(conn, records, processed=FINAL, file_date=None):
    """One doclist snapshot. Records go to the day of ``file_date`` or their own submit / ope date."""
    by_day: dict[str, list] = {}
    for r in records:
        fd = file_date or (r["opeDateTime"] or r["submitDateTime"] or "2026-06-20")[:10]
        by_day.setdefault(fd, []).append(dict(r))
    with db.transaction(conn):
        for fd, rs in by_day.items():
            for i, r in enumerate(rs, start=1):
                r["seqNumber"] = i
            doclist.import_day(conn, {"metadata": {"status": "200", "processDateTime": processed},
                                      "results": rs}, fd)


def resolve(conn, scanned, as_of=T, **kw):
    doclist.merge_documents(conn)
    run_id = f"r{len(conn.execute('SELECT run_id FROM runs').fetchall())}"
    runner.start(conn, run_id, "resolve", fy="2026-03", as_of=as_of, args={})
    runner.resolve_targets(conn, [Target("1001", CODE, "テスト株式会社", "test")], run_id=run_id, as_of=as_of,
                           window=WIN, cfg=Resolve(), cache_dir=scanned, **kw)
    rows = {}
    for r in conn.execute("SELECT * FROM filing_selection WHERE run_id = ? AND entity_id = ?", (run_id, CODE)):
        r = dict(r)
        r["flags"], r["basis"] = json.loads(r.pop("flags_json")), json.loads(r.pop("reason_basis_json"))
        rows[r["role"]] = r
    return rows["cur"], rows["prev"]


def texts(mapping):
    return {"text_of": mapping.get, "available": lambda d: True}


# ---------------------------------------------------------------- 1. withdrawal (withdrawalStatus=1/2)
def test_withdrawn_versions_excluded(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1")], processed="2026-07-01 00:01")
    snap(conn, [masked("O1", "2026-06-20 10:00"), letter("L1", "O1", "2026-06-24 09:00"),
                rec("O2", submit="2026-06-25 10:00"), prev_year("P1")])
    cur, prev = resolve(conn, scanned)
    assert (cur["doc_id"], cur["original_doc_id"], cur["version_kind"]) == ("O2", "O2", "original")
    assert cur["basis"]["excluded"] == {"O1": "withdrawn", "L1": "withdrawn"}
    assert cur["basis"]["reason"] is None and prev["doc_id"] == "P1" and prev["no_prev_reason"] is None
    # documents keeps the withdrawn filing's metadata (from the older snapshot) with status 2
    o1 = conn.execute("SELECT withdrawal_status, period_end FROM documents WHERE doc_id = 'O1'").fetchone()
    assert tuple(o1) == ("2", "2026-03-31")


def test_only_version_withdrawn_has_no_adopted_version(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1")], processed="2026-07-01 00:01")
    snap(conn, [masked("O1", "2026-06-20 10:00"), letter("L1", "O1", "2026-06-24 09:00"), prev_year("P1")])
    cur, _ = resolve(conn, scanned)
    assert cur["doc_id"] is None and "no_valid_version" in cur["flags"]
    assert cur["basis"]["reason"] in ("not_filed", "late")
    assert cur["basis"]["reason_basis"]["no_filing"]["excluded_versions"] == {"O1": "withdrawn", "L1": "withdrawn"}


# ---------------------------------------------------------------- 2. not disclosed
def test_not_disclosed_excluded(conn, scanned):
    snap(conn, [rec("O1", disc="2"), rec("O2", submit="2026-06-26 10:00"), prev_year("P1", disc="2"),
                prev_year("P2", submit="2025-06-27 10:00")])
    cur, prev = resolve(conn, scanned)
    assert cur["doc_id"] == "O2" and cur["basis"]["excluded"] == {"O1": "not_disclosed"}
    assert prev["doc_id"] == "P2" and prev["basis"]["excluded"] == {"P1": "not_disclosed"}


# ---------------------------------------------------------------- 3. docInfoEditStatus=1 re-listing
def test_edit_relisting_adopted(conn, scanned):
    # filed with a wrong periodEnd (outside the window); EDINET staff fix it on 07-01 (edit=1 re-listing)
    # and the original slot is re-fetched later as "edited" (edit=2) -- fetch order must not matter.
    snap(conn, [rec("O1", ope="2026-07-01 10:00", edit="1", xbrl="1")], file_date="2026-07-01",
         processed="2026-07-02 00:01")
    snap(conn, [rec("O1", pe="2025-03-31", ps="2024-04-01", edit="2"), prev_year("P1")])
    cur, prev = resolve(conn, scanned)
    assert (cur["doc_id"], cur["period_end"]) == ("O1", "2026-03-31")
    assert prev["doc_id"] == "P1" and prev["basis"]["pair"]["gap_days"] == 1
    d = conn.execute("SELECT period_end, doc_info_edit_status, last_ope_dt FROM documents WHERE doc_id = 'O1'")
    assert tuple(d.fetchone()) == ("2026-03-31", "1", "2026-07-01 10:00")


def test_edit_relisting_after_T_not_used_at_T(conn, scanned):
    snap(conn, [rec("O1", edit="2"), prev_year("P1")])
    snap(conn, [rec("O1", pe="2026-02-28", ps="2025-03-01", ope="2026-10-10 12:00", edit="1")],
         file_date="2026-10-10", processed="2026-10-11 00:01")
    cur, _ = resolve(conn, scanned)
    assert (cur["doc_id"], cur["period_end"]) == ("O1", "2026-03-31")          # state at T
    later = date(2026, 10, 31)
    cur2, _ = resolve(conn, scanned, as_of=later)
    assert (cur2["doc_id"], cur2["period_end"]) == ("O1", "2026-02-28")         # the edit, once visible


# ---------------------------------------------------------------- 4. correction after T
def test_correction_after_T_excluded(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1"), correction("K1", "O1", "2026-10-15 09:00")],
         processed="2026-10-16 00:01")
    cur, _ = resolve(conn, scanned)
    assert (cur["doc_id"], cur["version_kind"]) == ("O1", "original")
    assert cur["basis"]["excluded"] == {"K1": "submitted_after_T"} and cur["flags"] == []
    cur2, _ = resolve(conn, scanned, as_of=date(2026, 10, 31))
    assert (cur2["doc_id"], cur2["original_doc_id"], cur2["version_kind"]) == ("K1", "O1", "correction")


# ---------------------------------------------------------------- 5. withdrawal after T
def test_withdrawal_after_T_not_applied(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1")], processed="2026-07-01 00:01")
    snap(conn, [masked("O1", "2026-06-20 10:00"), letter("L1", "O1", "2026-10-20 09:00"), prev_year("P1")],
         processed="2026-10-21 00:01")
    cur, prev = resolve(conn, scanned)
    assert cur["doc_id"] == "O1" and cur["basis"]["reason"] is None
    assert cur["basis"]["state_notes"] == {"O1": ["withdrawal_after_T"]}
    assert cur["basis"]["excluded"] == {"L1": "withdrawn"}            # the letter itself is after T anyway
    assert prev["doc_id"] == "P1"
    cur2, _ = resolve(conn, scanned, as_of=date(2026, 10, 31))
    assert cur2["doc_id"] is None and cur2["basis"]["excluded"]["O1"] == "withdrawn"


# ---------------------------------------------------------------- 6. xbrlFlag=0 corrections only
def test_unparsed_correction_uses_previous_version(conn, scanned):
    snap(conn, [rec("O1"), correction("K1", "O1", "2026-07-10 09:00", xbrl="0"),
                correction("K2", "O1", "2026-08-10 09:00", xbrl="0"),
                prev_year("P1"), correction("Q1", "P1", "2025-08-01 09:00", xbrl="0")])
    cur, prev = resolve(conn, scanned)
    assert (cur["doc_id"], cur["version_kind"]) == ("O1", "original") and "correction_unparsed" in cur["flags"]
    assert cur["basis"]["unparsed_corrections"] == ["K1", "K2"] and cur["basis"]["reason"] is None
    assert prev["doc_id"] == "P1" and "correction_unparsed" in prev["flags"] and prev["no_prev_reason"] is None


def test_unparsed_correction_after_parsed_one(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1"), correction("K1", "O1", "2026-07-10 09:00"),
                correction("K2", "O1", "2026-08-10 09:00", xbrl="0")])
    cur, _ = resolve(conn, scanned, **texts({"O1": BODY, "K1": BODY}))
    assert (cur["doc_id"], cur["version_kind"]) == ("K1", "correction")      # latest parseable version
    assert "correction_unparsed" in cur["flags"] and cur["basis"]["unparsed_corrections"] == ["K2"]


# ---------------------------------------------------------------- 7. 702001
def test_xbrl_fix_702001_is_a_version(conn, scanned):
    assert "702001" in FilingFilter.from_toml(FILTER_TOML).extra_forms
    snap(conn, [rec("O1"), prev_year("P1"), correction("X1", "O1", "2026-07-15 09:00", form="702001")])
    cur, _ = resolve(conn, scanned, **texts({"O1": BODY, "X1": BODY}))
    assert (cur["doc_id"], cur["original_doc_id"], cur["version_kind"]) == ("X1", "O1", "xbrl_fix")
    assert cur["basis"]["versions"] == ["O1", "X1"] and cur["flags"] == []
    assert cur["basis"]["risk_changed_by_correction"] is False


def test_xbrl_fix_702001_without_text_is_pending_not_partial(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1"), correction("X1", "O1", "2026-07-15 09:00", form="702001")])
    cur, _ = resolve(conn, scanned, text_of=lambda d: None, available=lambda d: d == "O1")
    assert cur["doc_id"] == "X1" and cur["basis"]["texts_pending"] == ["X1"]
    assert "correction_partial" not in cur["flags"]


# ---------------------------------------------------------------- 8. correction_partial
@pytest.mark.parametrize("k1_text", [None, "２【事業等のリスク】（訂正前）…（訂正後）…"],
                         ids=["textblock_missing", "fragment"])
def test_partial_correction_falls_back_to_full_version(conn, scanned, k1_text):
    snap(conn, [rec("O1"), prev_year("P1"), correction("K1", "O1", "2026-08-01 09:00")])
    cur, _ = resolve(conn, scanned, **texts({"O1": BODY, "K1": k1_text}))
    assert (cur["doc_id"], cur["version_kind"]) == ("O1", "original") and "correction_partial" in cur["flags"]
    part = cur["basis"]["partial_corrections"]
    assert [p["doc_id"] for p in part] == ["K1"]
    assert part[0]["chars"] is None if k1_text is None else 0 < part[0]["chars"] < 0.5 * cur["basis"]["original_chars"]


def test_partial_correction_keeps_latest_full_correction(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P1"), correction("K1", "O1", "2026-07-01 09:00"),
                correction("K2", "O1", "2026-08-01 09:00", form="702001")])
    cur, _ = resolve(conn, scanned, **texts({"O1": BODY, "K1": BODY + "新たなリスク。", "K2": "（訂正箇所のみ）"}))
    assert (cur["doc_id"], cur["original_doc_id"]) == ("K1", "O1")
    assert {"correction_partial", "risk_changed_by_correction"} <= set(cur["flags"])


# ---------------------------------------------------------------- 9. previous year missing
def test_prev_missing_year_not_compared(conn, scanned):
    snap(conn, [rec("O1"), prev_year("P0", ps="2023-04-01", pe="2024-03-31", submit="2024-06-20 10:00")])
    cur, prev = resolve(conn, scanned)
    assert cur["doc_id"] == "O1" and cur["basis"]["reason"] is None
    assert prev["doc_id"] is None and prev["no_prev_reason"] == "prev_missing_year"
    assert prev["period_end"] == "2024-03-31"
    pair = prev["basis"]["pair"]
    assert pair["gap_days"] == 366 and pair["skipped_prev_doc_id"] == "P0"
    assert prev["basis"]["reason"] == "prev_missing_year"
