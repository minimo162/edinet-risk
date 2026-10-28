"""HTTP layer for the EDINET API (stdlib only; plan §3 net/http.py).

- TLS via ``truststore`` (OS store) when installed, else the default verified context
  (honours ``SSL_CERT_FILE``). Never disables verification.
- ``mask_url`` / ``mask_headers``: the API key travels as the ``Subscription-Key`` query
  param and ``Ocp-Apim-Subscription-Key`` header; mask before anything is logged.
- ``Transport`` is the injectable seam used by tests (no network in unit tests).
- ``forbid_network()``: ``--offline`` (plan §3-2). Any attempt to reach the network inside it raises
  ``NetworkForbidden`` — from ``EdinetClient`` (whatever the transport) and, as a backstop, from the
  socket layer.
"""
from __future__ import annotations

import contextlib
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator

MIN_INTERVAL_FLOOR = 1.0  # plan §6-4: interval floor is fixed at 1s
KEY_PARAM = "Subscription-Key"
KEY_HEADER = "Ocp-Apim-Subscription-Key"
RETRY_STATUSES = {429, 500, 502, 503, 504}

_MASK_RE = re.compile(r"(Subscription-Key=)[^&\s]*", re.IGNORECASE)


def mask_url(url: str) -> str:
    return _MASK_RE.sub(r"\1***", url)


def mask_headers(headers: dict) -> dict:
    return {k: ("***" if k.lower() == KEY_HEADER.lower() else v) for k, v in headers.items()}


@dataclass
class Response:
    status: int
    headers: dict  # lower-cased keys
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


Transport = Callable[[str, dict, float], Response]


def _ssl_context() -> ssl.SSLContext:
    try:  # OS trust store if available (corporate CA)
        import truststore  # type: ignore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()  # verifies certs; honours SSL_CERT_FILE


class NetworkForbidden(RuntimeError):
    """Network access attempted while ``--offline`` is in effect (never an EdinetError: not retried / masked away)."""


_offline_depth = 0


def is_offline() -> bool:
    return _offline_depth > 0


def check_online(what: str) -> None:
    if _offline_depth > 0:
        raise NetworkForbidden(f"--offline: network access is forbidden ({what})")


@contextlib.contextmanager
def forbid_network() -> Iterator[None]:
    """Forbid network access in this block (EdinetClient, urllib_transport and raw sockets)."""
    global _offline_depth

    def blocked(name):
        def f(*a, **kw):
            raise NetworkForbidden(f"--offline: network access is forbidden (socket.{name})")
        return f

    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo)
    _offline_depth += 1
    socket.socket.connect, socket.socket.connect_ex = blocked("connect"), blocked("connect_ex")
    socket.create_connection, socket.getaddrinfo = blocked("create_connection"), blocked("getaddrinfo")
    try:
        yield
    finally:
        _offline_depth -= 1
        if _offline_depth == 0:
            socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo = saved


def urllib_transport(url: str, headers: dict, timeout: float) -> Response:
    check_online(mask_url(url))
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")

