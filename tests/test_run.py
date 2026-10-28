"""``run`` and ``--offline`` (plan §3-2, §4 M1-5/6; M1 step I).

Synthetic filer (doclist day file + type=1 ZIPs in a temp cache) for the pipeline, the fetch-only-missing
rule and the network guard; the 7 filers run offline on the saved doclists / ZIPs when present.
Idempotency = ``m1_run.fingerprint`` (table contents without run_id, Excel cell snapshot, HTML), never the
xlsx binary.
"""
import json
import socket
from pathlib import Path

import pytest

pytest.importorskip("openpyxl")

from edinet_risk import m1_run  # noqa: E402
from edinet_risk.__main__ import main  # noqa: E402
from edinet_risk.net import edinet_api  # noqa: E402
from edinet_risk.net.http import NetworkForbidden, Response, forbid_network, is_offline  # noqa: E402
from edinet_risk.store import db  # noqa: E402

from test_compare_db import _type1  # noqa: E402
from test_report import CUR, PREV, TH  # noqa: E402
from test_resolve import _window_dates, day_file, rec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FILTER = ROOT / "config" / "filing_filter.toml"
KEY = "dummy-key-0123456789"


def _setup(tmp: Path) -> m1_run.Inputs:
    cache = tmp / "cache"
    (cache / "doclist").mkdir(parents=True)
    (cache / "doclist" / "2026-06-20.json").write_text(json.dumps(day_file([
        rec("C0000001", seq=1),
        rec("P0000001", seq=2, ps="2024-04-01", pe="2025-03-31", submit="2025-06-20 10:00")])), encoding="utf-8")
    _type1(cache, "C0000001", CUR)
    _type1(cache, "P0000001", PREV)
    targets = tmp / "targets.toml"
    targets.write_text('[[filer]]\nlabel = "テスト自動車"\nedinetCode = "E00001"\n\n'
                       '[[period]]\nperiodEnd = "2026-03-31"\nscan_from = "2026-06-10"\nscan_to = "2026-06-30"\n',
                       encoding="utf-8")
    (tmp / "m0").mkdir()
    return m1_run.Inputs(targets=targets, cache_dir=cache, filter_path=FILTER, thresholds=TH, m0_dir=tmp / "m0",
                         out_dir=tmp / "out", db_path=tmp / "db.sqlite")


def _all_text(run_dir: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in run_dir.rglob("*") if p.is_file()
                     and p.suffix in (".json", ".md", ".csv", ".html"))


# ---------------------------------------------------------------- pipeline + idempotency (M1-5/6)
def test_run_offline_twice_is_idempotent(tmp_path):
    inp = _setup(tmp_path)
    a = m1_run.run(inp, offline=True, run_id="T-0001")
    b = m1_run.run(inp, offline=True, run_id="T-0002")
    for r in (a, b):
        assert r["doc_api_calls"] == 0 and r["network_calls"] == 0 and r["api_calls"] == {}
        assert r["fetch"] == {"cached": ["C0000001", "P0000001"], "fetched": [], "missing": {}}
        f = r["compare"]["data"]["filers"][0]
        assert f["skip"] is None and f["cur"]["source"] == "cache_zip"
        assert r["report"]["reconcile"]["fulltext_ok"] == r["report"]["reconcile"]["fulltext_rows"] == 2
    fa, fb = a["fingerprint"], b["fingerprint"]
    assert m1_run.diff_fingerprints(fa, fb) == []
    assert fa["digest"] == fb["digest"] and fa["excel"]["cells"] > 0 and len(fa["html"]) == 2
    t = fa["tables"]
    assert set(m1_run.EXCLUDED_TABLES).isdisjoint(t)
    assert t["filing_selection"]["rows"] == 2           # this run's rows only (run_id column dropped)
    assert min(t[x]["rows"] for x in ("documents", "extracts", "items", "matches", "sentence_changes")) > 0
    assert json.loads((b["run_dir"] / m1_run.FINGERPRINT).read_text(encoding="utf-8"))["digest"] == fb["digest"]
    assert m1_run.previous_fingerprint(inp.out_dir, "T-0002")["run_id"] == "T-0001"
    conn = db.connect(inp.db_path)
    try:
        runs = {r["run_id"]: dict(r) for r in conn.execute("SELECT * FROM runs")}
    finally:
        conn.close()
    assert set(runs) == {"T-0001", "T-0002"}                      # one runs row per run (resolve / compare share it)
    assert all(r["cmd"] == "run" and r["status"] == "ok" and r["as_of"] == "2026-09-30" for r in runs.values())
    assert json.loads(runs["T-0001"]["args_json"])["offline"] is True
    assert runs["T-0001"]["meta_synced_at"] == "2026-10-01T00:01:00+09:00"


def test_fingerprint_detects_a_changed_output(tmp_path):
    inp = _setup(tmp_path)
    a = m1_run.run(inp, offline=True, run_id="T-0001")
    page = sorted((a["run_dir"] / "html").glob("diff_*.html"))[0]
    page.write_text(page.read_text(encoding="utf-8") + "x", encoding="utf-8")
    conn = db.connect(inp.db_path)
    try:
        with db.transaction(conn):
            conn.execute("UPDATE items SET body = body || 'x' WHERE item_idx = 0")
        again = m1_run.fingerprint(conn, "T-0001", a["run_dir"])
    finally:
        conn.close()
    assert m1_run.diff_fingerprints(a["fingerprint"], again) == ["table:items", f"html:html/{page.name}"]


# ---------------------------------------------------------------- fetch only what is missing
def _fake_transport(zip_path: Path, calls: list):
    def transport(url, headers, timeout):
        calls.append(url)
        return Response(200, {"content-type": "application/octet-stream"}, zip_path.read_bytes())
    return transport


def test_online_run_fetches_missing_zip_once(tmp_path, monkeypatch):
    inp = _setup(tmp_path)
    zp = inp.cache_dir / "docs" / "C0000001_type1.zip"
    saved = tmp_path / "saved.zip"
    zp.rename(saved)
    calls = []
    monkeypatch.setenv("EDINET_API_KEY", KEY)
    monkeypatch.setattr(edinet_api, "urllib_transport", _fake_transport(saved, calls))
    a = m1_run.run(inp, run_id="T-0001")
    assert a["doc_api_calls"] == 1 and a["network_calls"] == 1 and len(calls) == 1
    assert a["fetch"]["fetched"] == ["C0000001"] and "Subscription-Key=" + KEY in calls[0]
    assert zp.exists() and a["compare"]["data"]["filers"][0]["skip"] is None
    b = m1_run.run(inp, run_id="T-0002")
    assert b["doc_api_calls"] == 0 and b["network_calls"] == 0 and len(calls) == 1   # 2nd run: no document call
    assert m1_run.diff_fingerprints(a["fingerprint"], b["fingerprint"]) == []
    conn = db.connect(inp.db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM api_calls")]
        fetches = [dict(r) for r in conn.execute("SELECT * FROM fetches")]
    finally:
        conn.close()
    assert [(r["run_id"], r["endpoint"], r["http_status"]) for r in rows] == [("T-0001", "documents", 200)]
    assert KEY not in json.dumps(rows + fetches, default=str)
    assert KEY not in _all_text(a["run_dir"]) + _all_text(b["run_dir"])


def test_fetch_step_records_fetches_row(tmp_path, monkeypatch):
    inp = _setup(tmp_path)
    zp = inp.cache_dir / "docs" / "P0000001_type1.zip"
    saved = tmp_path / "saved.zip"
    zp.rename(saved)
    monkeypatch.setattr(edinet_api, "urllib_transport", _fake_transport(saved, []))
    conn = db.open_db(inp.db_path)
    try:
        db.upsert(conn, "documents", {"doc_id": "P0000001"}, key=["doc_id"])
        client = edinet_api.EdinetClient(api_key=KEY, cache_dir=inp.cache_dir)
        out = m1_run.fetch_documents(["P0000001", "C0000001", None, "P0000001"], inp.cache_dir, conn, client=client)
        assert out == {"cached": ["C0000001"], "fetched": ["P0000001"], "missing": {}}
        row = dict(conn.execute("SELECT * FROM fetches").fetchone())
        assert (row["doc_id"], row["status"], row["attempts"], row["size"]) == ("P0000001", "ok", 1, saved.stat().st_size)
        again = m1_run.fetch_documents(["P0000001"], inp.cache_dir, conn, client=client)
        assert again["cached"] == ["P0000001"] and client.calls == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- --offline guard
def test_offline_run_with_missing_zip_reports_instead_of_fetching(tmp_path, monkeypatch):
    inp = _setup(tmp_path)
    (inp.cache_dir / "docs" / "C0000001_type1.zip").unlink()
    monkeypatch.setenv("EDINET_API_KEY", KEY)
    hit = []
    monkeypatch.setattr(edinet_api, "urllib_transport", lambda *a: hit.append(a))
    r = m1_run.run(inp, offline=True, run_id="T-0001")
    assert hit == [] and r["doc_api_calls"] == 0
    assert r["fetch"]["missing"] == {"C0000001": "type=1 not in cache (--offline)"}
    assert "offline" in r["compare"]["data"]["filers"][0]["skip"]


def test_forbid_network_raises_clear_error_and_restores():
    orig = socket.create_connection
    client = edinet_api.EdinetClient(api_key=KEY, cache_dir="/nonexistent", transport=lambda *a: pytest.fail("net"))
    with forbid_network():
        assert is_offline()
        with pytest.raises(NetworkForbidden, match="--offline") as ei:
            client.list_documents("2026-06-20")
        assert KEY not in str(ei.value) and "Subscription-Key=***" in str(ei.value)
        with pytest.raises(NetworkForbidden):
            socket.create_connection(("api.edinet-fsa.go.jp", 443))
        with pytest.raises(NetworkForbidden):
            edinet_api.urllib_transport("https://example.invalid/?Subscription-Key=" + KEY, {}, 1.0)
        with forbid_network():   # nested
            pass
        assert is_offline()
    assert not is_offline() and socket.create_connection is orig
    assert client.calls == 0


def test_cli_offline_on_a_networked_command_exits_3(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("EDINET_API_KEY", KEY)
    rc = main(["--log-level", "WARNING", "--offline", "m0_spike", "--filer", "テスト", "--years", "2026",
               "--cache", str(tmp_path / "cache"), "--out", str(tmp_path / "out")])
    assert rc == 3
    assert "--offline: network access is forbidden" in caplog.text and KEY not in caplog.text
    assert not is_offline()


def _cli(inp: m1_run.Inputs, pre=(), post=()) -> list[str]:
    return ["--log-level", "WARNING", *pre, "run", *post, "--targets", str(inp.targets),
            "--filter", str(inp.filter_path), "--thresholds", str(inp.thresholds), "--m0", str(inp.m0_dir),
            "--cache", str(inp.cache_dir), "--db", str(inp.db_path), "--out", str(inp.out_dir)]


def test_cli_run_offline_twice(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    inp = _setup(tmp_path)
    assert main(_cli(inp, ["--offline"], ["--run-id", "T-0001", "--as-of", "2026-09-30"])) == 0   # global --offline
    out1 = capsys.readouterr().out
    assert "api_calls this run: documents=0" in out1 and "--offline" in out1 and "idempotency" not in out1
    assert main(_cli(inp, post=["--offline", "--run-id", "T-0002"])) == 0                    # run --offline
    out2 = capsys.readouterr().out
    assert "documents=0" in out2 and "idempotency vs run T-0001: identical" in out2


# ---------------------------------------------------------------- the 7 filers (saved doclists + ZIPs, offline)
@pytest.mark.skipif(not (ROOT / "cache" / "doclist" / "2026-06-22.json").exists()
                    or not (ROOT / "cache" / "docs" / "S100YF9D_type1.zip").exists(), reason="M0 cache absent")
def test_transport7_run_offline_twice(tmp_path, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    cache = tmp_path / "cache"
    (cache / "doclist").mkdir(parents=True)
    for d in _window_dates():
        src = ROOT / "cache" / "doclist" / f"{d}.json"
        if src.exists():
            (cache / "doclist" / src.name).symlink_to(src)
    (cache / "docs").symlink_to(ROOT / "cache" / "docs")
    inp = m1_run.Inputs(targets=ROOT / "config" / "transport7.toml", cache_dir=cache, filter_path=FILTER,
                        thresholds=TH, m0_dir=ROOT / "out" / "m0", out_dir=tmp_path / "out",
                        db_path=tmp_path / "db.sqlite")
    a = m1_run.run(inp, offline=True, run_id="seven1")
    b = m1_run.run(inp, offline=True, run_id="seven2")
    for r in (a, b):
        assert r["doc_api_calls"] == 0 and r["network_calls"] == 0
        assert len(r["fetch"]["cached"]) == 14 and not r["fetch"]["missing"]
        assert all(f["skip"] is None for f in r["compare"]["data"]["filers"])
        assert r["report"]["reconcile"]["fulltext_ok"] == 14
    assert m1_run.diff_fingerprints(a["fingerprint"], b["fingerprint"]) == []
    assert len(a["fingerprint"]["html"]) == 8                     # index + 7 filers
