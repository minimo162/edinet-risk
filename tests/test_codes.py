import io
import json
import zipfile
from pathlib import Path

import pytest

from edinet_risk.__main__ import main
from edinet_risk.net import http
from edinet_risk.net.download import Downloader
from edinet_risk.net.edinet_api import EdinetError
from edinet_risk.net.http import Response
from edinet_risk.population import edinet_codes as ec
from edinet_risk.store import db

FIX = Path(__file__).resolve().parent / "fixtures" / "codes"
ZIP = (FIX / "Edinetcode.zip").read_bytes()
AGG = (FIX / "ESE140190.csv").read_bytes()


class Fake:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls, self.headers = [], []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        self.headers.append(headers)
        return self.responses.pop(0)


def ok(body, ct="application/octet-stream"):
    return Response(200, {"content-type": ct}, body)


def downloader(fake, **kw):
    return Downloader(transport=fake, sleep=lambda s: None, **kw)


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    yield c
    c.close()


def make_zip(lines, name="EdinetcodeDlInfo.csv"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, ("\r\n".join(lines) + "\r\n").encode("cp932"))
    return buf.getvalue()


HDR = "ＥＤＩＮＥＴコード,提出者種別,上場区分,連結の有無,資本金,決算日,提出者名,提出者名（英字）,提出者名（ヨミ）,所在地,提出者業種,証券コード,提出者法人番号"


def row(code, listed="上場", sec="13760"):
    return f'"{code}","内国法人・組合","{listed}","有","1","3月31日","名","N","ヨミ","所","業","{sec}","1"'


# ---------------------------------------------------------------- parsers
def test_parse_fixture_zip():
    cl = ec.parse_edinetcode_zip(ZIP)
    assert cl.list_date == "2026-10-28" and cl.declared_count == 5
    by = {r["edinet_code"]: r for r in cl.rows}
    assert by["E99001"] == {"edinet_code": "E99001", "sec_code5": "130A0", "name": "テスト新興株式会社", "listed": 1}
    assert by["E99002"]["listed"] == 0 and by["E99002"]["sec_code5"] is None
    assert by["E99003"]["listed"] is None and by["E99003"]["name"] == "テスト　太郎"  # stored as in source


def test_parse_aggregation_fixture():
    assert ec.parse_aggregation_csv(AGG) == [
        {"old_code": "E99005", "new_code": "E99004", "source_date": "2023-10-12"},
        {"old_code": "E99006", "new_code": "E99004", "source_date": "2025-08-20"}]


def test_parse_rejects_count_mismatch_bad_codes_and_wrong_encoding():
    with pytest.raises(ec.CodesError, match="件数 3"):
        ec.parse_edinetcode_zip(make_zip(["ダウンロード実行日,2026年10月28日現在,件数,3件", HDR, row("E00001")]))
    with pytest.raises(ec.CodesError, match="証券コード"):
        ec.parse_edinetcode_zip(make_zip(["ダウンロード実行日,2026年10月28日現在,件数,1件", HDR,
                                          row("E00001", sec="13A")]))
    with pytest.raises(ec.CodesError, match="duplicate"):
        ec.parse_edinetcode_zip(make_zip(["ダウンロード実行日,2026年10月28日現在,件数,2件", HDR,
                                          row("E00001"), row("E00001")]))
    with pytest.raises(ec.CodesError, match="metadata row"):
        ec.parse_edinetcode_zip(make_zip([HDR, row("E00001")]))
    with pytest.raises(ec.CodesError, match="not a valid ZIP"):
        ec.parse_edinetcode_zip(b"<html>maintenance</html>")
    with pytest.raises(ec.CodesError, match="cp932"):
        ec.parse_aggregation_csv("集約処理日,廃止EDINETコード,継続EDINETコード\n".encode("utf-8") + b"\xff\xff")
    with pytest.raises(ec.CodesError, match="header"):
        ec.parse_aggregation_csv("<html>not found</html>".encode("cp932"))


def test_parse_lowercase_letter_sec_code_is_upper_cased():
    cl = ec.parse_edinetcode_zip(make_zip(["ダウンロード実行日,2026年10月28日現在,件数,1件", HDR,
                                           row("E00001", sec="130a0")]))
    assert cl.rows[0]["sec_code5"] == "130A0"


# ---------------------------------------------------------------- sync
def test_offline_import_from_file_and_idempotent(conn, tmp_path):
    with http.forbid_network():
        r1 = ec.sync(conn, tmp_path / "cache", from_file=FIX / "Edinetcode.zip",
                     aggregation_file=FIX / "ESE140190.csv")
        dump1 = list(conn.iterdump())
        r2 = ec.sync(conn, tmp_path / "cache", from_file=FIX / "Edinetcode.zip",
                     aggregation_file=FIX / "ESE140190.csv")
    assert (r1["edinet_codes"]["new"], r1["edinet_codes"]["listed"]) == (5, 3)
    assert r1["code_aggregation"]["new"] == 2
    assert (r2["edinet_codes"]["new"], r2["edinet_codes"]["changed"], r2["edinet_codes"]["unchanged"]) == (0, 0, 5)
    assert r2["code_aggregation"]["unchanged"] == 2
    assert list(conn.iterdump()) == dump1
    assert conn.execute("SELECT sec_code5 FROM edinet_codes WHERE edinet_code='E99001'").fetchone()[0] == "130A0"
    assert conn.execute("SELECT count(*) FROM edinet_codes WHERE listed = 1").fetchone()[0] == 3
    assert not (tmp_path / "cache" / "codes").exists()  # --from-file does not touch the cache
    assert r1["network_calls"] == 0


def test_download_caches_with_manifest_then_reuses(conn, tmp_path):
    fake = Fake([ok(ZIP), ok(AGG, "text/csv")])
    d = downloader(fake, audit_hook=db.api_call_recorder(conn, None))
    r1 = ec.sync(conn, tmp_path / "cache", downloader=d)
    assert fake.urls == [ec.EDINETCODE_URL, ec.AGGREGATION_URL]
    assert all("Subscription-Key" not in u for u in fake.urls)
    assert all(not any("subscription" in k.lower() for k in h) for h in fake.headers)
    codes = tmp_path / "cache" / "codes"
    man = json.loads((codes / "manifest.json").read_text(encoding="utf-8"))
    assert man[ec.EDINETCODE_FILE]["sha256"] == r1["edinet_codes"]["sha256"]
    assert man[ec.EDINETCODE_FILE]["url"] == ec.EDINETCODE_URL and man[ec.AGGREGATION_FILE]["fetched_at"]
    assert (codes / ec.EDINETCODE_FILE).read_bytes() == ZIP
    rows = [dict(r) for r in conn.execute("SELECT endpoint, http_status FROM api_calls ORDER BY id")]
    assert rows == [{"endpoint": "codes/Edinetcode.zip", "http_status": 200},
                    {"endpoint": "codes/ESE140190.csv", "http_status": 200}]
    dump1 = list(conn.iterdump())

    with http.forbid_network():  # second run: cache only, no network, no DB change
        r2 = ec.sync(conn, tmp_path / "cache", downloader=downloader(Fake([])))
    assert r2["edinet_codes"]["origin"] == "cache" and r2["network_calls"] == 0
    assert r2["edinet_codes"]["fetched_at"] == r1["edinet_codes"]["fetched_at"]
    assert list(conn.iterdump()) == dump1


def test_refresh_downloads_again(conn, tmp_path):
    ec.sync(conn, tmp_path / "cache", downloader=downloader(Fake([ok(ZIP), ok(AGG)])))
    fake = Fake([ok(ZIP), ok(AGG)])
    r = ec.sync(conn, tmp_path / "cache", downloader=downloader(fake), refresh=True)
    assert len(fake.urls) == 2 and r["edinet_codes"]["origin"] == "download"


def test_bad_download_is_not_cached(conn, tmp_path):
    d = downloader(Fake([ok(b"<html>error</html>", "text/html")]))
    with pytest.raises(ec.CodesError, match="ZIP"):
        ec.sync(conn, tmp_path / "cache", downloader=d)
    assert not (tmp_path / "cache" / "codes" / ec.EDINETCODE_FILE).exists()
    assert conn.execute("SELECT count(*) FROM edinet_codes").fetchone()[0] == 0


def test_tampered_cache_is_an_error(conn, tmp_path):
    ec.sync(conn, tmp_path / "cache", downloader=downloader(Fake([ok(ZIP), ok(AGG)])))
    (tmp_path / "cache" / "codes" / ec.AGGREGATION_FILE).write_bytes(AGG + b"2026-01-01,E99007,E99004\r\n")
    with pytest.raises(ec.CodesError, match="sha256"):
        ec.sync(conn, tmp_path / "cache", downloader=downloader(Fake([])))


def test_offline_without_cache_is_a_clear_error(conn, tmp_path):
    fake = Fake([])
    with http.forbid_network(), pytest.raises(ec.CodesError, match="--offline: .*not cached"):
        ec.sync(conn, tmp_path / "cache", downloader=downloader(fake))
    assert fake.urls == []


def test_downloader_throttles_and_backs_off():
    slept, now = [], [0.0]
    fake = Fake([Response(503, {}, b""), ok(b"x"), ok(b"y")])
    d = Downloader(transport=fake, sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                   clock=lambda: now[0], min_interval=0.2)
    assert d.get("https://example.invalid/a", "a").body == b"x"
    d.get("https://example.invalid/b", "b")
    assert slept[0] == 60.0 and all(s >= 1.0 for s in slept[1:]) and d.calls == 3  # floor 1s even if 0.2 set
    with pytest.raises(EdinetError, match="HTTP 404"):
        downloader(Fake([Response(404, {}, b"")])).get("https://example.invalid/c", "c")


# ---------------------------------------------------------------- CLI
def test_cli_offline_from_file_and_missing_cache(tmp_path, capsys):
    dbp, cache = tmp_path / "c.sqlite", tmp_path / "cache"
    args = ["--offline", "codes", "sync", "--db", str(dbp), "--cache", str(cache)]
    assert main(args + ["--from-file", str(FIX / "Edinetcode.zip"),
                        "--aggregation-file", str(FIX / "ESE140190.csv")]) == 0
    out = capsys.readouterr().out
    assert "edinet_codes: 5 rows" in out and "listed=3" in out and "network calls this run: 0" in out
    assert main(args) == 2  # nothing cached
