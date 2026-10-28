"""Backward-compatible shim (M0 code imports from here).

The implementation moved to ``edinet_risk.net.http`` / ``edinet_risk.net.edinet_api``
(plan §3). New code should import from ``edinet_risk.net``.
"""
from __future__ import annotations

from .net.edinet_api import (BASE_URL, EdinetAuthError, EdinetClient, EdinetError,  # noqa: F401
                             EdinetNotFound, _json_status, _raise_for_api_status, _raise_http,
                             check_doclist_response, check_document_response)
from .net.http import (KEY_HEADER, KEY_PARAM, MIN_INTERVAL_FLOOR, RETRY_STATUSES, Response,  # noqa: F401
                       Transport, _ssl_context, mask_headers, mask_url, urllib_transport)
