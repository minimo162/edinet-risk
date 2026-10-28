"""Throttled GET for public EDINET files that need no API key (code lists; plan §3 population/edinet_codes.py).

Same rules as ``EdinetClient`` (plan §6-4): serial, min 1s between requests (floor fixed at 1s),
exponential backoff on 429/5xx, no retry on 4xx, ``--offline`` honoured before any transport.
The API key is never sent: these files are on the public download host, not the API.
"""
from __future__ import annotations

import logging
import time
import urllib.error
from datetime import datetime, timezone
from typing import Callable

from .edinet_api import EdinetError, _raise_http, backoff_delay
from .http import MIN_INTERVAL_FLOOR, RETRY_STATUSES, Response, Transport, check_online, mask_url, urllib_transport

log = logging.getLogger(__name__)


class Downloader:
    def __init__(self, *, min_interval: float = 1.0, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 max_retries: int = 5, backoff_base: float = 60.0, timeout: float = 120.0,
                 backoff_max: float | None = None, audit_hook: Callable[[dict], None] | None = None):
        self.min_interval = max(MIN_INTERVAL_FLOOR, min_interval)
        self.transport = transport or urllib_transport
        self.sleep, self.clock = sleep, clock
        self.max_retries, self.backoff_base, self.timeout = max_retries, backoff_base, timeout
        self.backoff_max = backoff_max
        self.audit_hook = audit_hook
        self._last: float | None = None
        self.calls = 0  # network calls actually made

    def _throttle(self):
        if self._last is not None:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def get(self, url: str, endpoint: str) -> Response:
        """GET ``url``; return the HTTP 200 response or raise ``EdinetError`` (after retries for 429/5xx)."""
        masked = mask_url(url)  # no key in these URLs; mask anyway
        check_online(f"GET {masked}")
        headers = {"User-Agent": "edinet-risk/0.0.1"}
        for attempt in range(self.max_retries + 1):
            self._throttle()
            t0 = time.monotonic()
            try:
                resp = self.transport(url, headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                raise EdinetError(f"network error: {mask_url(str(e))} url={masked}", url=masked) from None
            self.calls += 1
            elapsed = int((time.monotonic() - t0) * 1000)
            log.info("GET %s -> HTTP %s %s %d bytes %dms", masked, resp.status,
                     resp.content_type, len(resp.body), elapsed)
            if self.audit_hook is not None:
                self.audit_hook({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": masked,
                                 "endpoint": endpoint, "params_masked": "{}", "http_status": resp.status,
                                 "content_type": resp.content_type, "api_status": None,
                                 "bytes": len(resp.body), "elapsed_ms": elapsed})
            if resp.status in RETRY_STATUSES and attempt < self.max_retries:
                delay = backoff_delay(attempt, self.backoff_base, self.backoff_max)
                log.warning("HTTP %s, backing off %.0fs (attempt %d)", resp.status, delay, attempt + 1)
                self.sleep(delay)
                continue
            if resp.status != 200:
                _raise_http(resp.status, masked)
            return resp
        raise AssertionError("unreachable")  # pragma: no cover
