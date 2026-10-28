"""ingest.doclist sync / merge / state_as_of and `doclist sync` CLI (plan §3-2, §6-1, §6-4, §6-5). Offline."""
import json
from datetime import date, datetime

import pytest

from edinet_risk.__main__ import main
from edinet_risk.config import Window
from edinet_risk.ingest import doclist
from edinet_risk.net.edinet_api import JST, DoclistQuietHours, EdinetClient, in_quiet_hours
from edinet_risk.net.http import Response
from edinet_risk.store import db

KEY = "sk-test-SECRET-123"
TODAY = date(2026, 10, 27)


def rec(doc_id, seq, *, ope=None, edit="0", w="0", pe="2026-03-31", code="E00001", dtype="120"):
    return {"docID": doc_id, "seqNumber": seq, "edinetCode": code, "ordinanceCode": "010", "formCode": "030000",
            "docTypeCode": dtype, "periodStart": "2025-04-01", "periodEnd": pe,
            "submitDateTime": "2026-06-20 10:00", "parentDocID": None, "opeDateTime": ope,
            "withdrawalStatus": w, "docInfoEditStatus": edit, "disclosureStatus": "0", "xbrlFlag": "1",
            "csvFlag": "1", "legalStatus": "1"}


def day(records, processed, file_date="2026-06-20", status="200"):
    return {"metadata": {"status": status, "processDateTime": processed, "parameter": {"date": file_date}},
            "results": records}


def write_cache(root, file_date, obj):
    d = root / "doclist"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{file_date}.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def js(obj, status=200):
    return Response(status, {"content-type": "application/json; charset=utf-8"}, json.dumps(obj).encode())


class Fake:
    def __init__(self, responses):
        self.responses, self.urls = list(responses), []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        r = self.responses.pop(0)
        return r(url) if callable(r) else r


def client(tmp_path, fake, conn=None, **kw):
    return EdinetClient(api_key=KEY, cache_dir=tmp_path / "cache", transport=fake, sleep=lambda s: None,
                        audit_hook=db.api_call_recorder(conn, None) if conn is not None else None, **kw)


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    yield c
    c.close()


def raw_rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT file_date, seq_number, fetched_at, record_json FROM doclist_raw ORDER BY 1, 2, 3")]


# ---------------------------------------------------------------- doclist_raw is append-only
def test_raw_append_never_overwrites(conn, tmp_path):
    cache = tmp_path / "cache"
    write_cache(cache, "2026-06-20", day([rec("S1", 1)], "2026-06-21 00:05"))
    plan = doclist.plan_sync(cache, ["2026-06-20"], TODAY)
    r1 = doclist.sync(conn, cache, plan)
    r2 = doclist.sync(conn, cache, plan)                      # same snapshot again: no-op
    assert (r1.imported_days, r2.imported_days) == (1, 0)
    first = raw_rows(conn)
    assert len(first) == 1 and json.loads(first[0][3]) == rec("S1", 1)   # record_json kept as is
    # re-fetched snapshot of the same day (EDINET masked S1): appended, old row untouched
    write_cache(cache, "2026-06-20", day([rec("S1", 1, w="2")], "2026-09-01 10:00"))
    doclist.sync(conn, cache, plan)
    rows = raw_rows(conn)
    assert len(rows) == 2 and rows[0] == first[0]
    assert rows[1][2] == "2026-09-01T10:00:00+09:00"
    assert conn.execute("SELECT withdrawal_status FROM documents WHERE doc_id='S1'").fetchone()[0] == "2"


# ---------------------------------------------------------------- merge: max opeDateTime, then seqNumber
def test_edit_relisting_wins_regardless_of_fetch_order(conn, tmp_path):
    """docInfoEditStatus=1 re-listing on a later day vs the original slot re-fetched later (status 2)."""
    cache = tmp_path / "cache"
    write_cache(cache, "2026-06-25", day([rec("S1", 7, ope="2026-06-25 15:18", edit="1", pe="2026-02-28")],
                                         "2026-06-26 00:05"))
    write_cache(cache, "2026-06-20", day([rec("S1", 3, edit="2")], "2026-10-01 00:05"))   # fetched last
    doclist.sync(conn, cache, doclist.plan_sync(cache, ["2026-06-20", "2026-06-25"], TODAY))
    row = conn.execute("SELECT period_end, doc_info_edit_status, last_ope_dt, last_seq_number, last_seen_file_date "
                       "FROM documents WHERE doc_id='S1'").fetchone()
    assert tuple(row) == ("2026-02-28", "1", "2026-06-25 15:18", 7, "2026-06-25")


def test_reappearance_twice_and_seq_tiebreak(conn):
    with db.transaction(conn):
        doclist.import_day(conn, day([rec("S1", 3, edit="2")], "2026-10-01 00:05"), "2026-06-20")
        doclist.import_day(conn, day([rec("S1", 5, ope="2026-07-01 10:00", edit="1", pe="2026-01-31")],
                                     "2026-07-02 00:05"), "2026-07-01")
        doclist.import_day(conn, day([rec("S1", 9, ope="2026-08-01 10:00", edit="1", pe="2026-02-28")],
                                     "2026-08-02 00:05"), "2026-08-01")
        # same opeDateTime on the same day: higher seqNumber wins
        doclist.import_day(conn, day([rec("S2", 4, ope="2026-08-01 11:00", edit="1", pe="2026-01-31"),
                                      rec("S2", 2, ope="2026-08-01 11:00", edit="1", pe="2026-02-28")],
                                     "2026-08-02 00:05"), "2026-08-01")
    doclist.merge_documents(conn)
    pe = dict(conn.execute("SELECT doc_id, period_end FROM documents"))
    assert pe == {"S1": "2026-02-28", "S2": "2026-01-31"}


def test_masked_snapshot_of_edited_slot_keeps_its_place():
    """A withdrawn edit re-listing is blanked (no opeDateTime) on re-fetch; it must still beat the original."""
    from edinet_risk.resolve.versions import merge_state
    orig = {**rec("S1", 3, edit="2"), "_file_date": "2026-06-20", "_fetched_at": "2026-10-01T00:05:00+09:00"}
    edit = {**rec("S1", 5, ope="2026-07-01 10:00", edit="1"), "_file_date": "2026-07-01",
            "_fetched_at": "2026-07-02T00:05:00+09:00"}
    gone = {**rec("S1", 5, w="2"), "_file_date": "2026-07-01", "_fetched_at": "2026-09-01T00:05:00+09:00"}
    assert merge_state([gone, edit, orig])["S1"]["withdrawalStatus"] == "2"


# ---------------------------------------------------------------- state at base date T
def test_state_as_of(conn):
    with db.transaction(conn):
        doclist.import_day(conn, day([rec("S1", 3, edit="2")], "2026-10-20 00:05"), "2026-06-20")
        doclist.import_day(conn, day([rec("S1", 5, ope="2026-10-10 12:00", edit="1", pe="2026-02-28")],
                                     "2026-10-11 00:05"), "2026-10-10")
    doclist.merge_documents(conn)
    assert doclist.state_as_of(conn, date(2026, 9, 30))["S1"]["periodEnd"] == "2026-03-31"
    assert doclist.state_as_of(conn, "2026-10-10 11:59")["S1"]["periodEnd"] == "2026-03-31"
    assert doclist.state_as_of(conn, "2026-10-10 12:00")["S1"]["periodEnd"] == "2026-02-28"   # ope <= T
    assert doclist.state_as_of(conn, date(2026, 10, 31), ["E00001"])["S1"]["periodEnd"] == "2026-02-28"
    assert conn.execute("SELECT period_end FROM documents").fetchone()[0] == "2026-02-28"


# ---------------------------------------------------------------- planning / dry-run counts
def test_plan_skips_final_cached_days(tmp_path):
    cache = tmp_path / "cache"
    write_cache(cache, "2026-06-20", day([], "2026-06-21 00:05"))     # final
    write_cache(cache, "2026-06-21", day([], "2026-06-21 18:00"))     # fetched the same day: not final
    dates = doclist.date_range(date(2026, 6, 19), date(2026, 6, 22))
    p = doclist.plan_sync(cache, dates, TODAY)
    assert p.to_fetch == ["2026-06-19", "2026-06-21", "2026-06-22"] and p.calls == 3
    assert p.cached_final == ["2026-06-20"] and p.cached_stale == ["2026-06-21"]
    assert doclist.plan_sync(cache, dates, TODAY, refresh=True).calls == 4
    assert doclist.plan_sync(cache, ["2026-10-27", "2026-10-28"], TODAY).future == ["2026-10-28"]


def test_daily_and_window_dates():
    assert doclist.daily_dates(TODAY) == ["2026-10-26", "2026-10-27"]
    cur = Window(date(2025, 4, 1), date(2026, 3, 31), date(2025, 4, 1), date(2026, 9, 30))
    prev = Window(date(2024, 4, 1), date(2025, 3, 31), date(2024, 4, 1), date(2025, 9, 30))
    assert len(doclist.window_dates([cur], TODAY)) == 548
    both = doclist.window_dates([cur, prev], TODAY)
    assert both[0] == "2024-04-01" and both[-1] == "2026-09-30" and len(both) == 913
    assert doclist.window_dates([cur], date(2025, 4, 10))[-1] == "2025-04-10"   # capped at today


def test_cli_dry_run_counts_without_network_or_db(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    cache = tmp_path / "cache"
    write_cache(cache, "2026-06-20", day([], "2026-06-21 00:05"))
    dbp = tmp_path / "x.sqlite"
    args = ["--offline", "doclist", "sync", "--from", "2026-06-18", "--to", "2026-06-21", "--dry-run",
            "--cache", str(cache), "--db", str(dbp), "--today", "2026-10-27"]
    assert main(args[1:]) == 0                     # without --offline: still no network, no key needed
    out = capsys.readouterr().out
    assert "days=4" in out and "cached_final=1" in out and "dry-run: documents.json calls=3 " in out
    assert main(args[1:3] + ["--rescan-month", "--dry-run", "--cache", str(cache), "--today", "2026-10-27"]) == 0
    assert "calls=548 " in capsys.readouterr().out
    assert not dbp.exists()


# ---------------------------------------------------------------- offline / cache only
def test_cli_offline_imports_cache_and_reports_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    cache = tmp_path / "cache"
    write_cache(cache, "2026-06-20", day([rec("S1", 1), rec("S2", 2)], "2026-06-21 00:05"))
    write_cache(cache, "2026-06-22", day([rec("S3", 1)], "2026-06-23 00:05"))
    dbp = tmp_path / "x.sqlite"
    rc = main(["--offline", "doclist", "sync", "--from", "2026-06-20", "--to", "2026-06-22", "--cache", str(cache),
               "--db", str(dbp), "--today", "2026-10-27"])
    out = capsys.readouterr().out
    assert rc == 0 and "fetched=0" in out and "not fetched=1 (cache only)" in out
    assert "doclist_raw rows=3 days=2 documents=3" in out and "network calls=0" in out
    rc = main(["doclist", "sync", "--from-cache", "--cache", str(cache), "--db", str(dbp)])
    assert rc == 0 and "imported day files=0" in capsys.readouterr().out          # idempotent
    c = db.connect(dbp)
    assert c.execute("SELECT count(*) FROM api_calls").fetchone()[0] == 0
    assert c.execute("SELECT status, meta_synced_at FROM runs ORDER BY started_at LIMIT 1").fetchone()[1] \
        == "2026-06-23T00:05:00+09:00"
    c.close()


# ---------------------------------------------------------------- network behaviour (fake transport)
def test_sync_fetches_imports_and_audits_without_key(conn, tmp_path, caplog):
    cache = tmp_path / "cache"
    write_cache(cache, "2026-10-26", day([rec("S1", 1)], "2026-10-26 18:00"))    # not final: re-fetched
    fake = Fake([js(day([rec("S1", 1), rec("S2", 2)], "2026-10-27 00:05", "2026-10-26")),
                 js(day([rec("S3", 1)], "2026-10-27 12:00", "2026-10-27"))])
    plan = doclist.plan_sync(cache, doclist.daily_dates(TODAY), TODAY)
    res = doclist.sync(conn, cache, plan, client(tmp_path, fake, conn))
    assert res.fetched == ["2026-10-26", "2026-10-27"] and not res.failed and res.documents == 3
    assert len(fake.urls) == 2
    assert conn.execute("SELECT count(*) FROM doclist_raw").fetchone()[0] == 4    # 1 old snapshot + 2 + 1
    rows = conn.execute("SELECT endpoint, params_masked, http_status, api_status FROM api_calls").fetchall()
    assert [tuple(r) for r in rows] == [("documents.json", '{"date": "2026-10-26", "type": 2}', 200, "200"),
                                        ("documents.json", '{"date": "2026-10-27", "type": 2}', 200, "200")]
    dump = "\n".join(conn.iterdump())
    assert KEY not in dump and KEY not in caplog.text
    assert KEY not in (cache / "api_calls.jsonl").read_text()


@pytest.mark.parametrize("resp", [
    js({"StatusCode": 401, "message": "Access denied due to invalid subscription key."}),   # HTTP 200 + JSON 401
    js({"metadata": {"status": "403", "message": "Forbidden"}}),
    js({"statusCode": 401, "message": "denied"}, status=401),
])
def test_auth_error_stops_at_once_without_retry(conn, tmp_path, resp):
    cache = tmp_path / "cache"
    fake = Fake([resp])
    plan = doclist.plan_sync(cache, doclist.date_range(date(2026, 6, 1), date(2026, 6, 5)), TODAY)
    c = client(tmp_path, fake, conn)
    res = doclist.sync(conn, cache, plan, c)
    assert res.stopped == "auth" and len(fake.urls) == 1 and c.calls == 1
    assert list(res.failed) == ["2026-06-01"] and res.missing == plan.to_fetch[1:]
    assert not (cache / "doclist").exists()
    assert KEY not in "".join(res.failed.values())


def test_metadata_status_error_fails_day_and_continues(conn, tmp_path):
    cache = tmp_path / "cache"
    fake = Fake([js({"metadata": {"status": "500", "message": "err"}}),
                 js(day([rec("S1", 1)], "2026-06-03 00:05", "2026-06-02"))])
    plan = doclist.plan_sync(cache, ["2026-06-01", "2026-06-02"], TODAY)
    res = doclist.sync(conn, cache, plan, client(tmp_path, fake, conn))
    assert list(res.failed) == ["2026-06-01"] and res.fetched == ["2026-06-02"] and res.stopped is None
    assert not (cache / "doclist" / "2026-06-01.json").exists()


def test_backoff_on_429_then_success(conn, tmp_path):
    slept = []
    fake = Fake([Response(429, {}, b""), Response(503, {}, b""), js(day([], "2026-06-02 00:05", "2026-06-01"))])
    c = EdinetClient(api_key=KEY, cache_dir=tmp_path / "cache", transport=fake, sleep=slept.append)
    res = doclist.sync(conn, tmp_path / "cache", doclist.plan_sync(tmp_path / "cache", ["2026-06-01"], TODAY), c)
    assert res.fetched == ["2026-06-01"] and len(fake.urls) == 3
    assert [s for s in slept if s >= 60] == [60.0, 120.0]


def test_quiet_hours_stop_before_any_call(conn, tmp_path):
    fake = Fake([])
    c = client(tmp_path, fake, wallclock=lambda: datetime(2026, 10, 28, 0, 30, tzinfo=JST))
    with pytest.raises(DoclistQuietHours):
        c.list_documents("2026-10-27")
    res = doclist.sync(conn, tmp_path / "cache",
                       doclist.plan_sync(tmp_path / "cache", ["2026-10-26", "2026-10-27"], TODAY), c)
    assert res.stopped == "quiet_hours" and fake.urls == [] and res.missing == ["2026-10-27"]
    assert in_quiet_hours(datetime(2026, 10, 28, 0, 0)) and not in_quiet_hours(datetime(2026, 10, 28, 1, 0))
    assert in_quiet_hours(datetime(2026, 10, 27, 15, 30, tzinfo=JST)) is False
    from datetime import timezone
    assert in_quiet_hours(datetime(2026, 10, 27, 15, 30, tzinfo=timezone.utc))   # = 00:30 JST


def test_interval_at_least_one_second(conn, tmp_path):
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s
    fake = Fake([js(day([], f"2026-06-0{i + 1} 00:05", f"2026-06-0{i}")) for i in (1, 2, 3)])
    c = EdinetClient(api_key=KEY, cache_dir=tmp_path / "cache", transport=fake, sleep=sleep, clock=lambda: t[0],
                     min_interval=0.2)
    doclist.sync(conn, tmp_path / "cache",
                 doclist.plan_sync(tmp_path / "cache", ["2026-06-01", "2026-06-02", "2026-06-03"], TODAY), c)
    assert len(fake.urls) == 3 and slept == [1.0, 1.0]
