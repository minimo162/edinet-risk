import io
import json
import re
import zipfile
from pathlib import Path

from edinet_risk import client
from edinet_risk.net import edinet_api, http
from edinet_risk.net.http import Response
from edinet_risk.store import db

KEY = "TESTKEY-0123456789abcdef"
SRC = Path(__file__).resolve().parents[1] / "src" / "edinet_risk"


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL/PublicDoc/x.xbrl", b"<xbrl/>")
    return buf.getvalue()


class Fake:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, url, headers, timeout):
        return self.responses.pop(0)


def test_client_shim_reexports_same_objects():
    assert client.EdinetClient is edinet_api.EdinetClient
    assert client.EdinetError is edinet_api.EdinetError
    assert client.Response is http.Response and client.mask_url is http.mask_url
    assert client.check_document_response is edinet_api.check_document_response


def test_audit_hook_writes_key_free_api_calls(tmp_path):
    conn = db.open_db(tmp_path / "t.sqlite")
    db.start_run(conn, "r1", "test", code_version="t")
    doclist = {"metadata": {"status": "200", "message": "OK"}, "results": []}
    fake = Fake([Response(200, {"content-type": "application/json"}, json.dumps(doclist).encode()),
                 Response(200, {"content-type": "application/octet-stream"}, make_zip())])
    c = edinet_api.EdinetClient(KEY, tmp_path / "cache", transport=fake, sleep=lambda s: None,
                                audit_hook=db.api_call_recorder(conn, "r1"))
    c.list_documents("2026-06-20")
    c.get_document("S100ABCD", 1)
    rows = [dict(r) for r in conn.execute("SELECT * FROM api_calls ORDER BY id")]
    assert [(r["endpoint"], r["api_status"], r["http_status"]) for r in rows] == \
        [("documents.json", "200", 200), ("documents", None, 200)]
    assert json.loads(rows[0]["params_masked"]) == {"date": "2026-06-20", "type": 2}
    dump = "\n".join(conn.iterdump())
    assert KEY not in dump
    assert KEY not in (tmp_path / "cache" / "api_calls.jsonl").read_text(encoding="utf-8")
    conn.close()


def test_audit_hook_records_in_body_error_status(tmp_path):
    seen = []
    fake = Fake([Response(200, {"content-type": "application/json"}, b'{"StatusCode": 401, "message": "bad"}')])
    c = edinet_api.EdinetClient(KEY, tmp_path, transport=fake, sleep=lambda s: None, audit_hook=seen.append)
    try:
        c.list_documents("2026-06-20")
    except edinet_api.EdinetAuthError:
        pass
    assert seen[0]["api_status"] == "401" and KEY not in json.dumps(seen)


def _py_sources():
    return {p: p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}


def test_no_zoneinfo_import():  # plan §3-1: embeddable Python has no tzdata
    bad = [p for p, s in _py_sources().items() if re.search(r"^\s*(from|import)\s+zoneinfo\b", s, re.M)]
    assert not bad


def test_no_unverified_tls():  # plan §6-8
    pat = re.compile(r"_create_unverified_context|CERT_NONE|check_hostname\s*=\s*False")
    assert not [p for p, s in _py_sources().items() if pat.search(s)]
