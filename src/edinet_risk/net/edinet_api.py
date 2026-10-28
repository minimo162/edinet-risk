"""EDINET API v2 client (stdlib only; plan §3 net/edinet_api.py).

- Serial, min 1s between requests (floor fixed at 1s).
- Detects failures even on HTTP 200: Content-Type, JSON metadata.status / statusCode,
  ZIP integrity, expected members.
- API key is sent as ``Subscription-Key`` query param and ``Ocp-Apim-Subscription-Key``
  header, and is masked in every log line / exception / audit record.
- Raw successful responses are cached under ``cache_dir`` and never refetched.
- Optional ``audit_hook`` receives one key-free dict per network call (e.g. to write
  the ``api_calls`` table, plan §5).
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import zipfile
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .http import (KEY_HEADER, KEY_PARAM, MIN_INTERVAL_FLOOR, RETRY_STATUSES, NetworkForbidden,  # noqa: F401
                   Response, Transport, check_online, mask_headers, mask_url, urllib_transport)

log = logging.getLogger(__name__)

BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
JST = timezone(timedelta(hours=9))


def _now_jst() -> datetime:
    return datetime.now(JST)


def in_quiet_hours(now: datetime, start: str = "00:00", end: str = "01:00") -> bool:
    """True if ``now`` (JST wall clock) is in [start, end): the nightly doclist swap (plan §6-4)."""
    t = now.astimezone(JST).time() if now.tzinfo else now.time()
    a, b = dtime.fromisoformat(start), dtime.fromisoformat(end)
    return a <= t < b if a <= b else (t >= a or t < b)


class EdinetError(Exception):
    """Any API failure, including HTTP 200 responses that carry an error."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 api_status: str | None = None, url: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.api_status = api_status
        self.url = url


class EdinetAuthError(EdinetError):
    """401/403 (HTTP or in-body). Do not retry: the key is the problem."""


class EdinetNotFound(EdinetError):
    """400/404 (HTTP or in-body). Do not retry."""


class DoclistQuietHours(EdinetError):
    """documents.json is not called during the nightly swap (default 00:00-01:00 JST, plan §6-4)."""



def _json_status(obj) -> tuple[str | None, str]:
    """Return (status, message) from either error shape EDINET uses."""
    if not isinstance(obj, dict):
        return None, ""
    md = obj.get("metadata")
    if isinstance(md, dict) and "status" in md:
        return str(md.get("status")), str(md.get("message", ""))
    # bad key: HTTP 200 + {"StatusCode": 401, "message": ...} (observed live; case varies)
    low = {k.lower(): v for k, v in obj.items()}
    if "statuscode" in low:
        return str(low["statuscode"]), str(low.get("message", ""))
    return None, ""


def _peek_api_status(resp: Response) -> str | None:
    """In-body status of a JSON response (for the audit log only; never raises)."""
    if "json" not in resp.content_type.lower() and resp.body[:1] not in (b"{", b"["):
        return None
    try:
        return _json_status(json.loads(resp.body.decode("utf-8")))[0]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _raise_for_api_status(status: str | None, message: str, http_status: int, url: str):
    if status is None or status == "200":
        return
    msg = f"EDINET API error status={status} message={message!r} (HTTP {http_status}) url={url}"
    if status in ("401", "403"):
        raise EdinetAuthError(msg, http_status=http_status, api_status=status, url=url)
    if status in ("400", "404"):
        raise EdinetNotFound(msg, http_status=http_status, api_status=status, url=url)
    raise EdinetError(msg, http_status=http_status, api_status=status, url=url)


def check_doclist_response(resp: Response, url: str) -> dict:
    """Validate a documents.json response; return the parsed JSON."""
    masked = mask_url(url)
    try:
        obj = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        if resp.status != 200:
            _raise_http(resp.status, masked)
        raise EdinetError(f"documents.json: body is not JSON (HTTP {resp.status}, "
                          f"Content-Type={resp.content_type!r}): {e} url={masked}",
                          http_status=resp.status, url=masked) from None
    status, message = _json_status(obj)
    _raise_for_api_status(status, message, resp.status, masked)
    if resp.status != 200:
        _raise_http(resp.status, masked)
    if status is None or "results" not in obj:
        raise EdinetError(f"documents.json: unexpected JSON shape (no metadata.status/results) url={masked}",
                          http_status=resp.status, url=masked)
    return obj


def check_document_response(resp: Response, url: str, doc_type: int) -> bytes:
    """Validate a documents/{docID} response (ZIP expected). Return the ZIP bytes."""
    masked = mask_url(url)
    ct = resp.content_type.lower()
    if "json" in ct or resp.body[:1] in (b"{", b"["):
        try:
            obj = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            obj = None
        status, message = _json_status(obj)
        _raise_for_api_status(status, message, resp.status, masked)
        if resp.status != 200:   # e.g. HTTP 401/403/429 with a JSON body that has no status field
            _raise_http(resp.status, masked)
        raise EdinetError(f"document: JSON returned where ZIP expected (HTTP {resp.status}, "
                          f"Content-Type={resp.content_type!r}, status={status}) url={masked}",
                          http_status=resp.status, api_status=status, url=masked)
    if resp.status != 200:
        _raise_http(resp.status, masked)
    if "zip" not in ct and "octet-stream" not in ct:
        raise EdinetError(f"document: unexpected Content-Type {resp.content_type!r} url={masked}",
                          http_status=resp.status, url=masked)
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        bad = zf.testzip()
    except zipfile.BadZipFile as e:
        raise EdinetError(f"document: not a valid ZIP ({e}) url={masked}", http_status=resp.status, url=masked) from None
    if bad is not None:
        raise EdinetError(f"document: ZIP CRC error in {bad} url={masked}", http_status=resp.status, url=masked)
    names = zf.namelist()
    if doc_type == 1:
        if not any(n.startswith("XBRL/PublicDoc/") and n.endswith((".xbrl", ".htm")) for n in names):
            raise EdinetError(f"document: no XBRL/PublicDoc/*.xbrl|.htm in ZIP url={masked}",
                              http_status=resp.status, url=masked)
    elif doc_type == 5:
        if not any(n.lower().endswith(".csv") for n in names):
            raise EdinetError(f"document: no CSV in type=5 ZIP url={masked}", http_status=resp.status, url=masked)
    return resp.body


def backoff_delay(attempt: int, base: float, cap: float | None = None) -> float:
    """Seconds to wait before retry ``attempt + 1`` (0-based): base * 2**attempt, capped (plan §6-4)."""
    d = base * (2 ** attempt)
    return d if cap is None else min(d, cap)


def _raise_http(code: int, masked: str):
    msg = f"HTTP {code} url={masked}"
    if code in (401, 403):
        raise EdinetAuthError(msg, http_status=code, url=masked)
    if code in (400, 404):
        raise EdinetNotFound(msg, http_status=code, url=masked)
    raise EdinetError(msg, http_status=code, url=masked)


class EdinetClient:
    def __init__(self, api_key: str | None = None, cache_dir: str | Path = "cache", *,
                 min_interval: float = 1.0, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 max_retries: int = 5, backoff_base: float = 60.0, timeout: float = 120.0,
                 backoff_max: float | None = None,
                 base_url: str = BASE_URL, audit_hook: Callable[[dict], None] | None = None,
                 doclist_quiet: tuple[str, str] = ("00:00", "01:00"),
                 wallclock: Callable[[], datetime] | None = None):
        self._key = api_key if api_key is not None else os.environ.get("EDINET_API_KEY", "")
        if not self._key:
            raise EdinetAuthError("EDINET_API_KEY is not set")
        self.cache_dir = Path(cache_dir)
        self.min_interval = max(MIN_INTERVAL_FLOOR, min_interval)
        self.transport = transport or urllib_transport
        self.sleep, self.clock = sleep, clock
        self.max_retries, self.backoff_base, self.timeout = max_retries, backoff_base, timeout
        self.backoff_max = backoff_max
        self.base_url = base_url.rstrip("/")
        self.audit_hook = audit_hook
        self.doclist_quiet = doclist_quiet
        self.wallclock = wallclock or (lambda: _now_jst())
        self._last: float | None = None
        self.calls = 0  # network calls actually made

    # -- low level -------------------------------------------------------------
    def _throttle(self):
        if self._last is not None:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _get(self, path: str, params: dict) -> tuple[Response, str]:
        q = dict(params)
        q[KEY_PARAM] = self._key
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode(q)}"
        headers = {KEY_HEADER: self._key, "User-Agent": "edinet-risk/0.0.1"}
        masked = mask_url(url)
        check_online(f"GET {masked}")   # before any transport, injected ones included
        for attempt in range(self.max_retries + 1):
            self._throttle()
            t0 = time.monotonic()
            try:
                resp = self.transport(url, headers, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
                # HTTPException: e.g. IncompleteRead when the body is cut off mid-transfer
                # str(e) never contains the URL from urllib, but mask anyway
                raise EdinetError(f"network error: {mask_url(str(e))} url={masked}", url=masked) from None
            self.calls += 1
            elapsed = int((time.monotonic() - t0) * 1000)
            self._audit(masked, resp, elapsed, path, params)
            log.info("GET %s -> HTTP %s %s %d bytes %dms", masked, resp.status,
                     resp.content_type, len(resp.body), elapsed)
            if resp.status in RETRY_STATUSES and attempt < self.max_retries:
                delay = backoff_delay(attempt, self.backoff_base, self.backoff_max)
                log.warning("HTTP %s, backing off %.0fs (attempt %d)", resp.status, delay, attempt + 1)
                self.sleep(delay)
                continue
            return resp, url
        return resp, url  # pragma: no cover

    def _audit(self, masked_url: str, resp: Response, elapsed_ms: int, path: str = "", params: dict | None = None):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "url": masked_url,
               "http_status": resp.status, "content_type": resp.content_type,
               "bytes": len(resp.body), "elapsed_ms": elapsed_ms}
        with open(self.cache_dir / "api_calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.audit_hook is not None:
            # params never contain the key (it is added in _get); drop it defensively anyway
            safe = {k: v for k, v in (params or {}).items() if k.lower() != KEY_PARAM.lower()}
            self.audit_hook({**rec, "endpoint": path.split("/")[0] if path else "",
                             "params_masked": json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str),
                             "api_status": _peek_api_status(resp)})

    @staticmethod
    def _write_atomic(path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    # -- endpoints -------------------------------------------------------------
    def doclist_path(self, date: str) -> Path:
        return self.cache_dir / "doclist" / f"{date}.json"

    def document_path(self, doc_id: str, doc_type: int) -> Path:
        return self.cache_dir / "docs" / f"{doc_id}_type{doc_type}.zip"

    def list_documents(self, date: str, *, refresh: bool = False) -> dict:
        """documents.json?date=...&type=2 (cached; ``refresh`` fetches again and replaces the cache file).

        Raises ``DoclistQuietHours`` instead of calling the API during the nightly swap (§6-4).
        """
        p = self.doclist_path(date)
        if p.exists() and not refresh:
            return json.loads(p.read_text(encoding="utf-8"))
        now = self.wallclock()
        if in_quiet_hours(now, *self.doclist_quiet):
            raise DoclistQuietHours(f"documents.json is not called between {self.doclist_quiet[0]} and "
                                    f"{self.doclist_quiet[1]} JST (now {now:%H:%M}); date={date}")
        resp, url = self._get("documents.json", {"date": date, "type": 2})
        obj = check_doclist_response(resp, url)
        self._write_atomic(p, resp.body)
        return obj

    def get_document(self, doc_id: str, doc_type: int, *, refresh: bool = False) -> Path:
        """documents/{docID}?type=N (1=XBRL zip, 5=CSV zip). Returns cached ZIP path.

        ``refresh`` calls the API even if the cache file exists (it is replaced only by a valid body).
        """
        if not re.fullmatch(r"[A-Za-z0-9]+", doc_id):
            raise ValueError(f"invalid docID {doc_id!r}")
        p = self.document_path(doc_id, doc_type)
        if p.exists() and not refresh:
            return p
        resp, url = self._get(f"documents/{doc_id}", {"type": doc_type})
        body = check_document_response(resp, url, doc_type)
        self._write_atomic(p, body)
        return p
