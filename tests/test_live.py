"""Live checks against the real API. Run with EDINET_LIVE=1 (needs EDINET_API_KEY)."""
import os

import pytest

from edinet_risk.client import EdinetAuthError, EdinetClient, EdinetError

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("EDINET_LIVE") != "1" or not os.environ.get("EDINET_API_KEY"),
                                 reason="set EDINET_LIVE=1 and EDINET_API_KEY")]


def test_live_bad_docid_detected(tmp_path, caplog):
    c = EdinetClient(cache_dir=tmp_path)
    with pytest.raises(EdinetError) as ei:
        c.get_document("S100ZZZZ", 1)
    assert not c.document_path("S100ZZZZ", 1).exists()
    key = os.environ["EDINET_API_KEY"]
    assert key not in str(ei.value) and key not in caplog.text
    print(f"bad docID -> {type(ei.value).__name__}: http={ei.value.http_status} api={ei.value.api_status}")


def test_live_bad_key_detected(tmp_path):
    c = EdinetClient(api_key="invalid-key-for-test", cache_dir=tmp_path, backoff_base=1, max_retries=0)
    with pytest.raises(EdinetAuthError) as ei:
        c.list_documents("2026-06-23")
    print(f"bad key -> http={ei.value.http_status} api={ei.value.api_status}")
