"""M2 Step G: `fetch` (filing_selection cur/prev bodies) and the fetches retry queue (plan §3-2, §6-2〜6-4)."""
import http.client
import io
import json
import logging
import urllib.error
import zipfile

import pytest

from edinet_risk.__main__ import main
from edinet_risk.ingest import fetch
from edinet_risk.net import edinet_api
from edinet_risk.net.edinet_api import EdinetClient
from edinet_risk.net.http import Response
from edinet_risk.store import db, migrate

KEY = "FETCHKEY-0123456789abcdef"
RUN = "resolve-T1"


def make_zip(tag: str = "x") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL/PublicDoc/x.xbrl", f"<xbrl>{tag}</xbrl>".encode())
    return buf.getvalue()


ZIP = Response(200, {"content-type": "application/octet-stream"}, make_zip())


def jresp(status: int, obj) -> Response:
    return Response(status, {"content-type": "application/json; charset=utf-8"}, json.dumps(obj).encode())


class Fake:
    """Transport: pops a response per call (or raises it); records the URLs."""

    def __init__(self, responses):
        self.responses, self.urls = list(responses), []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    """Monotonic clock that moves only when slept (sleep is recorded)."""

    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"), ("E00001", "prev", "D0000002", "1"),
                             ("E00002", "cur", "D0000003", "1"))):
    conn = db.open_db(tmp_path / "t.sqlite")
    db.start_run(conn, RUN, "resolve", code_version="t")
    db.finish_run(conn, RUN, "ok")
    with db.transaction(conn):
        for ent, role, doc, xflag in docs:
            db.upsert(conn, "documents", {"doc_id": doc, "edinet_code": ent, "entity_id": ent, "xbrl_flag": xflag},
                      key=["doc_id"])
            conn.execute("INSERT INTO filing_selection (run_id, entity_id, role, doc_id) VALUES (?, ?, ?, ?)",
                         (RUN, ent, role, doc))
    return conn


def client(tmp_path, fake, clock=None, **kw):
    clock = clock or Clock()
    return EdinetClient(api_key=KEY, cache_dir=tmp_path / "cache", transport=fake, sleep=clock.sleep, clock=clock, **kw)


def run_fetch(conn, tmp_path, c, run_id="F1", retry_failed=False, max_attempts=3, entities=None):
    targets = fetch.selection_targets(conn, RUN, entities)
    plan = fetch.plan_fetch(conn, targets, tmp_path / "cache", retry_failed=retry_failed)
    return fetch.execute(conn, plan, tmp_path / "cache", run_id=run_id, client=c, key=KEY, max_attempts=max_attempts)


def rows(conn):
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM fetches")}


# ---------------------------------------------------------------- schema
def test_v3_fetches_statuses_and_rebuild_keeps_rows(tmp_path):
    c = db.connect(tmp_path / "m.sqlite")
    migrate.migrate(c, target=2)
    c.execute("INSERT INTO documents (doc_id) VALUES ('S1')")
    c.execute("INSERT INTO fetches (doc_id, type, status, attempts, last_error) VALUES ('S1', 1, 'failed', 2, 'x')")
    migrate.migrate(c)
    r = dict(c.execute("SELECT * FROM fetches").fetchone())
    assert (r["status"], r["attempts"], r["last_error"], r["error_kind"]) == ("failed", 2, "x", None)
    for st in ("pending", "ok", "retry", "failed", "no_xbrl"):
        c.execute("UPDATE fetches SET status = ?", (st,))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE fetches SET status = 'weird'")
    c.close()


# ---------------------------------------------------------------- 401 / 403: stop at once
@pytest.mark.parametrize("resp", [
    Response(401, {"content-type": "text/plain"}, b"unauthorized"),
    Response(403, {"content-type": "text/html"}, b"<html>forbidden</html>"),
    jresp(401, {"message": "Access denied due to invalid subscription key"}),   # HTTP 401 + JSON w/o status
    jresp(200, {"StatusCode": 401, "message": "Access denied"}),                 # HTTP 200 + JSON 401
    jresp(200, {"metadata": {"status": "403", "message": "Forbidden"}}),         # HTTP 200 + metadata 403
])
def test_auth_error_stops_without_retry_or_queueing(tmp_path, resp):
    conn = setup_db(tmp_path)
    fake, clock = Fake([resp]), Clock()
    res = run_fetch(conn, tmp_path, client(tmp_path, fake, clock))
    assert res.stopped == "auth" and len(fake.urls) == 1          # no retry, no further document
    assert 60 not in clock.slept and not any(s >= 60 for s in clock.slept)
    r = rows(conn)
    assert r["D0000001"]["status"] == "pending" and r["D0000001"]["error_kind"] == "auth"
    assert r["D0000001"]["attempts"] == 0                         # not counted against the document
    assert {r[d]["status"] for d in ("D0000002", "D0000003")} == {"pending"}
    assert res.not_fetched == ["D0000001", "D0000002", "D0000003"] and not res.failed and not res.retry
    assert KEY not in json.dumps(r) and KEY not in (res.stop_error or "")


# ---------------------------------------------------------------- 429 / 5xx: exponential backoff
def test_backoff_sequence_429_5xx_then_success(tmp_path):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"),))
    fake = Fake([Response(429, {}, b""), Response(503, {}, b""), Response(500, {}, b""), Response(502, {}, b""), ZIP])
    clock = Clock()
    res = run_fetch(conn, tmp_path, client(tmp_path, fake, clock))
    assert res.fetched == ["D0000001"] and len(fake.urls) == 5
    assert [s for s in clock.slept if s >= 60] == [60, 120, 240, 480]
    r = rows(conn)["D0000001"]
    assert (r["status"], r["attempts"], r["error_kind"], r["last_http_status"]) == ("ok", 1, None, 200)


def test_backoff_cap_and_retry_count_from_settings(tmp_path):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"),))
    fake = Fake([Response(503, {}, b"")] * 4)
    clock = Clock()
    res = run_fetch(conn, tmp_path, client(tmp_path, fake, clock, max_retries=3, backoff_base=60, backoff_max=100))
    assert len(fake.urls) == 4 and [s for s in clock.slept if s >= 60] == [60, 100, 100]
    assert list(res.retry) == ["D0000001"] and "HTTP 503" in res.retry["D0000001"]


def test_transient_failures_go_to_retry_queue_then_failed(tmp_path):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"),))
    for i, want in enumerate(["retry", "retry", "failed"], 1):
        fake = Fake([Response(503, {}, b"")] * 2)
        run_fetch(conn, tmp_path, client(tmp_path, fake, max_retries=1), run_id=f"F{i}", max_attempts=3)
        r = rows(conn)["D0000001"]
        assert (r["status"], r["attempts"], r["error_kind"], r["run_id"]) == (want, i, "transient", f"F{i}")
    # failed is not requested again without --retry-failed
    fake = Fake([])
    res = run_fetch(conn, tmp_path, client(tmp_path, fake), run_id="F4")
    assert fake.urls == [] and res.plan.count()["failed"] == 1


def test_network_error_is_transient_and_masked(tmp_path, caplog):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"),))
    fake = Fake([urllib.error.URLError(f"boom Subscription-Key={KEY} {KEY}")])
    with caplog.at_level(logging.DEBUG):
        res = run_fetch(conn, tmp_path, client(tmp_path, fake))
    r = rows(conn)["D0000001"]
    assert (r["status"], r["error_kind"]) == ("retry", "transient")
    assert KEY not in r["last_error"] and KEY not in caplog.text and KEY not in json.dumps(res.retry)


def test_truncated_body_is_transient_and_run_continues(tmp_path):
    # http.client.IncompleteRead from r.read() aborted the whole real run before it was caught (batch 3)
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"), ("E00002", "cur", "D0000003", "1")))
    fake = Fake([http.client.IncompleteRead(b"x" * 10, 20), ZIP])
    run_fetch(conn, tmp_path, client(tmp_path, fake))
    r = rows(conn)
    assert (r["D0000001"]["status"], r["D0000001"]["error_kind"]) == ("retry", "transient")
    assert "IncompleteRead" in r["D0000001"]["last_error"] or "network error" in r["D0000001"]["last_error"]
    assert r["D0000003"]["status"] == "ok"


def test_interval_at_least_one_second(tmp_path):
    conn = setup_db(tmp_path)
    fake, clock = Fake([ZIP, ZIP, ZIP]), Clock()
    run_fetch(conn, tmp_path, client(tmp_path, fake, clock, min_interval=0.2))   # floor 1s
    assert len(fake.urls) == 3 and clock.slept == [1.0, 1.0]


# ---------------------------------------------------------------- no refetch / retry-failed / scope
def test_fetched_bodies_are_never_refetched(tmp_path):
    conn = setup_db(tmp_path)
    fake = Fake([ZIP, ZIP, ZIP])
    run_fetch(conn, tmp_path, client(tmp_path, fake))
    assert len(fake.urls) == 3 and {r["status"] for r in rows(conn).values()} == {"ok"}
    again = Fake([])
    res = run_fetch(conn, tmp_path, client(tmp_path, again), run_id="F2")
    assert again.urls == [] and res.plan.count() == {"have": 3}
    # a changed file (sha256 mismatch) is fetched again; the others are not
    (tmp_path / "cache" / "docs" / "D0000002_type1.zip").write_bytes(make_zip("changed"))
    third = Fake([ZIP])
    res = run_fetch(conn, tmp_path, client(tmp_path, third), run_id="F3")
    assert len(third.urls) == 1 and "D0000002" in third.urls[0] and res.fetched == ["D0000002"]
    assert rows(conn)["D0000002"]["attempts"] == 2


def test_cached_zip_is_adopted_without_a_call_and_invalid_one_is_refetched(tmp_path):
    conn = setup_db(tmp_path)
    docs = tmp_path / "cache" / "docs"
    docs.mkdir(parents=True)
    (docs / "D0000001_type1.zip").write_bytes(make_zip())          # e.g. killed after the file write
    (docs / "D0000002_type1.zip").write_bytes(b"not a zip")
    fake = Fake([ZIP, ZIP])
    res = run_fetch(conn, tmp_path, client(tmp_path, fake))
    assert res.adopted == ["D0000001"] and res.fetched == ["D0000002", "D0000003"] and len(fake.urls) == 2
    r = rows(conn)["D0000001"]
    assert (r["status"], r["attempts"]) == ("ok", 0) and r["sha256"] == fetch.sha256_file(docs / "D0000001_type1.zip")


def test_not_found_is_failed_and_only_retry_failed_requests_it(tmp_path):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"), ("E00001", "prev", "D0000002", "1")))
    fake = Fake([jresp(404, {"metadata": {"status": "404", "message": "Not Found"}}), ZIP])
    run_fetch(conn, tmp_path, client(tmp_path, fake))
    r = rows(conn)
    assert (r["D0000001"]["status"], r["D0000001"]["error_kind"]) == ("failed", "not_found")
    assert r["D0000002"]["status"] == "ok" and len(fake.urls) == 2          # no retry of the 404
    skip = Fake([])
    assert run_fetch(conn, tmp_path, client(tmp_path, skip), run_id="F2").plan.count() == {"failed": 1, "have": 1}
    assert skip.urls == []
    again = Fake([ZIP])
    res = run_fetch(conn, tmp_path, client(tmp_path, again), run_id="F3", retry_failed=True)
    assert res.fetched == ["D0000001"] and len(again.urls) == 1
    assert (rows(conn)["D0000001"]["status"], rows(conn)["D0000001"]["attempts"]) == ("ok", 2)


def test_invalid_body_is_failed(tmp_path):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "1"),))
    fake = Fake([Response(200, {"content-type": "application/octet-stream"}, b"PK broken")])
    res = run_fetch(conn, tmp_path, client(tmp_path, fake))
    assert list(res.failed) == ["D0000001"] and rows(conn)["D0000001"]["error_kind"] == "invalid"
    assert not (tmp_path / "cache" / "docs" / "D0000001_type1.zip").exists()


def test_no_xbrl_is_never_requested_and_entity_scope(tmp_path):
    conn = setup_db(tmp_path, docs=(("E00001", "cur", "D0000001", "0"), ("E00002", "cur", "D0000003", "1")))
    fake = Fake([ZIP])
    res = run_fetch(conn, tmp_path, client(tmp_path, fake), entities={"E00001"})
    assert fake.urls == [] and res.plan.count() == {"no_xbrl": 1}
    assert rows(conn)["D0000001"]["status"] == "no_xbrl" and "D0000003" not in rows(conn)


# ---------------------------------------------------------------- CLI
def _cli(tmp_path, *args):
    return main(["fetch", "--db", str(tmp_path / "t.sqlite"), "--cache", str(tmp_path / "cache"), *args])


def _no_network(monkeypatch):
    def boom(url, headers, timeout):
        raise AssertionError("network used")
    monkeypatch.setattr(edinet_api, "urllib_transport", boom)


def _snapshot(path):
    c = db.connect(path)
    try:
        return "\n".join(c.iterdump())
    finally:
        c.close()


def test_cli_dry_run_counts_without_network_or_db_writes(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    conn = setup_db(tmp_path)
    conn.close()
    docs = tmp_path / "cache" / "docs"
    docs.mkdir(parents=True)
    (docs / "D0000001_type1.zip").write_bytes(make_zip())
    _no_network(monkeypatch)
    before = _snapshot(tmp_path / "t.sqlite")
    assert _cli(tmp_path, "--dry-run") == 0          # whole run allowed for a dry run
    out = capsys.readouterr().out
    assert "dry-run: documents API calls=2" in out and "adopt=1" in out
    assert _snapshot(tmp_path / "t.sqlite") == before


def test_cli_offline_records_cache_and_leaves_rest_pending(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    conn = setup_db(tmp_path)
    conn.close()
    docs = tmp_path / "cache" / "docs"
    docs.mkdir(parents=True)
    (docs / "D0000001_type1.zip").write_bytes(make_zip())
    _no_network(monkeypatch)
    assert _cli(tmp_path, "--offline", "--entity", "E00001") == 0
    out = capsys.readouterr().out
    assert "adopted_from_cache=1" in out and "not_fetched=1" in out and "documents API calls=0" in out
    c = db.connect(tmp_path / "t.sqlite")
    st = {r[0]: r[1] for r in c.execute("SELECT doc_id, status FROM fetches")}
    run = c.execute("SELECT status FROM runs WHERE cmd = 'fetch'").fetchone()[0]
    c.close()
    assert st == {"D0000001": "ok", "D0000002": "pending"} and run == "ok"
    # second offline run: nothing new, still no network
    assert _cli(tmp_path, "--offline", "--entity", "E00001") == 0
    assert "have=1" in capsys.readouterr().out


def test_cli_refuses_unscoped_network_fetch(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("EDINET_API_KEY", KEY)
    setup_db(tmp_path).close()
    _no_network(monkeypatch)
    assert _cli(tmp_path) == 2
    c = db.connect(tmp_path / "t.sqlite")
    assert c.execute("SELECT count(*) FROM fetches").fetchone()[0] == 0
    c.close()


def test_cli_fetch_auth_stop_and_key_never_leaks(tmp_path, capsys, caplog, monkeypatch):
    monkeypatch.setenv("EDINET_API_KEY", KEY)
    setup_db(tmp_path).close()
    fake = Fake([ZIP, jresp(200, {"StatusCode": 401, "message": f"bad key {KEY}"})])
    monkeypatch.setattr(edinet_api, "urllib_transport", fake)
    with caplog.at_level(logging.DEBUG):
        rc = _cli(tmp_path, "--entity", "E00001", "E00002")
    out = capsys.readouterr().out
    assert rc == 4 and len(fake.urls) == 2 and "STOPPED" in out
    assert all(f"Subscription-Key={KEY}" in u for u in fake.urls)      # the key is sent ...
    c = db.connect(tmp_path / "t.sqlite")
    dump = "\n".join(c.iterdump())
    st = {r[0]: r[1] for r in c.execute("SELECT doc_id, status FROM fetches")}
    run = c.execute("SELECT status FROM runs WHERE cmd = 'fetch'").fetchone()[0]
    calls = c.execute("SELECT count(*) FROM api_calls").fetchone()[0]
    c.close()
    assert st == {"D0000001": "ok", "D0000002": "pending", "D0000003": "pending"} and run == "aborted" and calls == 2
    # ... but never logged, printed, stored in the DB or the api_calls.jsonl audit file
    jsonl = (tmp_path / "cache" / "api_calls.jsonl").read_text(encoding="utf-8")
    for text in (dump, out, caplog.text, jsonl):
        assert KEY not in text
