import io
import json
import logging
import zipfile

import pytest

from edinet_risk.client import (EdinetAuthError, EdinetClient, EdinetError, EdinetNotFound, Response,
                                check_doclist_response, check_document_response, mask_url)

KEY = "TESTKEY-0123456789abcdef"
URL = f"https://api.edinet-fsa.go.jp/api/v2/documents/S100XXXX?type=1&Subscription-Key={KEY}"


def jresp(obj, status=200, ct="application/json; charset=utf-8"):
    return Response(status, {"content-type": ct}, json.dumps(obj).encode())


def make_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


GOOD_ZIP = make_zip({"XBRL/PublicDoc/x.xbrl": b"<xbrl/>"})


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(tmp_path, responses, **kw):
    fc = FakeClock()
    t = FakeTransport(responses)
    c = EdinetClient(api_key=KEY, cache_dir=tmp_path, transport=t, sleep=fc.sleep, clock=fc.clock, **kw)
    return c, t, fc


# --- HTTP 200 with error JSON -----------------------------------------------------
def test_doclist_http200_with_metadata_error(tmp_path):
    body = {"metadata": {"status": "404", "message": "Not Found"}}
    with pytest.raises(EdinetNotFound) as ei:
        check_doclist_response(jresp(body), URL)
    assert ei.value.http_status == 200 and ei.value.api_status == "404"


def test_doclist_http200_with_statuscode_401_is_auth_error(tmp_path):
    body = {"statusCode": 401, "message": "Access denied due to invalid subscription key."}
    with pytest.raises(EdinetAuthError):
        check_doclist_response(jresp(body), URL)


def test_doclist_http200_observed_bad_key_shape():
    # verbatim shape returned live for an invalid key (HTTP 200, capital S)
    body = {"StatusCode": 401, "message": "Access denied due to invalid subscription key."}
    with pytest.raises(EdinetAuthError):
        check_doclist_response(jresp(body), URL)


def test_doclist_http200_non_json(tmp_path):
    with pytest.raises(EdinetError, match="not JSON"):
        check_doclist_response(Response(200, {"content-type": "text/html"}, b"<html>maintenance</html>"), URL)


def test_doclist_ok():
    obj = check_doclist_response(jresp({"metadata": {"status": "200"}, "results": []}), URL)
    assert obj["results"] == []


def test_document_http200_json_error_when_zip_expected():
    body = {"metadata": {"status": "404", "message": "Not Found"}}
    with pytest.raises(EdinetNotFound):
        check_document_response(jresp(body), URL, 1)


def test_document_json_content_type_without_status_is_error():
    with pytest.raises(EdinetError, match="JSON returned where ZIP expected"):
        check_document_response(jresp({"foo": 1}), URL, 1)


def test_document_json_body_mislabelled_octet_stream():
    body = {"statusCode": 401, "message": "invalid key"}
    with pytest.raises(EdinetAuthError):
        check_document_response(jresp(body, ct="application/octet-stream"), URL, 1)


def test_document_wrong_content_type():
    with pytest.raises(EdinetError, match="unexpected Content-Type"):
        check_document_response(Response(200, {"content-type": "text/html"}, GOOD_ZIP), URL, 1)


def test_document_not_a_zip():
    with pytest.raises(EdinetError, match="not a valid ZIP"):
        check_document_response(Response(200, {"content-type": "application/octet-stream"}, b"PK\x03\x04junk"), URL, 1)


def test_document_zip_without_publicdoc_xbrl():
    z = make_zip({"XBRL/AuditDoc/a.xbrl": b"<x/>"})
    with pytest.raises(EdinetError, match="PublicDoc"):
        check_document_response(Response(200, {"content-type": "application/octet-stream"}, z), URL, 1)


def test_document_ok():
    assert check_document_response(Response(200, {"content-type": "application/octet-stream"}, GOOD_ZIP), URL, 1)


# --- masking, caching, throttling -------------------------------------------------
def test_mask_url():
    assert KEY not in mask_url(URL) and "Subscription-Key=***" in mask_url(URL)


def test_key_sent_as_param_and_header_but_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c, t, _ = client(tmp_path, [jresp({"metadata": {"status": "404", "message": "x"}})])
    with pytest.raises(EdinetError) as ei:
        c.get_document("S100BAD0", 1)
    url, headers = t.calls[0]
    assert f"Subscription-Key={KEY}" in url and headers["Ocp-Apim-Subscription-Key"] == KEY
    assert KEY not in caplog.text and KEY not in str(ei.value)
    assert KEY not in (tmp_path / "api_calls.jsonl").read_text()
    assert not c.document_path("S100BAD0", 1).exists()  # errors are not cached


def test_cache_prevents_refetch(tmp_path):
    ok = jresp({"metadata": {"status": "200"}, "results": [{"docID": "A"}]})
    c, t, _ = client(tmp_path, [ok])
    c.list_documents("2026-06-23")
    c.list_documents("2026-06-23")
    assert len(t.calls) == 1 and c.calls == 1


def test_min_interval_one_second_floor(tmp_path):
    ok = lambda: jresp({"metadata": {"status": "200"}, "results": []})
    c, t, fc = client(tmp_path, [ok(), ok()], min_interval=0.1)
    assert c.min_interval == 1.0
    c.list_documents("2026-06-20")
    c.list_documents("2026-06-21")
    assert fc.sleeps == [1.0]


def test_backoff_on_429_then_success(tmp_path):
    ok = jresp({"metadata": {"status": "200"}, "results": []})
    c, t, fc = client(tmp_path, [Response(429, {}, b""), Response(503, {}, b""), ok])
    c.list_documents("2026-06-20")
    assert [s for s in fc.sleeps if s >= 60] == [60.0, 120.0]


def test_http_401_no_retry(tmp_path):
    c, t, _ = client(tmp_path, [Response(401, {"content-type": "application/json"},
                                         b'{"statusCode":401,"message":"bad key"}')])
    with pytest.raises(EdinetAuthError):
        c.list_documents("2026-06-20")
    assert len(t.calls) == 1
