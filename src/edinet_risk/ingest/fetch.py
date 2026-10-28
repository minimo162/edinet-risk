"""`fetch`: type=1 bodies of the adopted current / previous versions + the retry queue (plan §3-2, §6-2〜6-4).

Targets are the ``filing_selection`` doc_ids (cur and prev rows) of one resolve run, optionally limited to
some entities. Nothing else is fetched: no correction history, no doclist-wide downloads (§6-2).

Per docID the plan is one of:

- ``have``     fetches.status=ok, the file exists and its sha256 matches -> nothing to do (never refetched, §6-3)
- ``adopt``    no ok row but a valid ZIP is already in the cache (M0/M1 downloads, or a run killed between
               the file write and the DB write) -> verified (3-point check) and recorded as ok, no API call
- ``fetch``    no row, pending, retry, or an ok row whose file is gone / changed -> one API request
- ``failed``   fetches.status=failed -> skipped unless ``retry_failed``
- ``no_xbrl``  xbrlFlag=0 -> never requested (§6-2)

Execution is serial through one ``EdinetClient`` (1s floor between requests, 429/5xx exponential backoff,
401/403 in HTTP or in the JSON body stops the run at once). Every outcome is committed per docID, so a
killed run resumes where it stopped (M2-7). Messages written to the DB are key-masked twice: by the client
(``mask_url``) and by ``scrub`` with the key itself.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..net.edinet_api import EdinetAuthError, EdinetError, EdinetNotFound, check_document_response
from ..net.http import RETRY_STATUSES, Response, mask_url
from ..store import db

log = logging.getLogger(__name__)

PROCESSOR_VERSION = "fetch-1"
DOC_TYPE = 1
ACTIONS = ("have", "adopt", "fetch", "failed", "no_xbrl")


def scrub(text: str | None, key: str | None) -> str | None:
    """Key-free copy of ``text`` (URL param masked, and the key string itself replaced)."""
    if text is None:
        return None
    text = mask_url(text)
    return text.replace(key, "***") if key else text


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_path(cache_dir: Path, doc_id: str) -> Path:
    """Same place as ``EdinetClient.document_path`` (M0/M1 cache, read by resolve/compare)."""
    return Path(cache_dir) / "docs" / f"{doc_id}_type{DOC_TYPE}.zip"


# ---------------------------------------------------------------- targets
def latest_resolve_run(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT r.run_id FROM runs r WHERE r.cmd = 'resolve' AND r.status = 'ok' "
                     "AND EXISTS (SELECT 1 FROM filing_selection f WHERE f.run_id = r.run_id) "
                     "ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1").fetchone()
    return r[0] if r else None


@dataclass
class Target:
    doc_id: str
    entity_id: str
    roles: tuple[str, ...]
    xbrl_flag: str | None


def selection_targets(conn: sqlite3.Connection, run_id: str, entities: Iterable[str] | None = None, *,
                      include_pending: bool = False) -> list[Target]:
    """Distinct doc_ids of the cur / prev rows of ``run_id`` (``entities``: entity_ids to keep; None = all).

    ``include_pending``: also the versions in ``reason_basis_json.texts_pending`` (the other versions of a
    corrected filing, whose bodies resolve needs for correction_partial / risk_changed_by_correction); their
    role is ``{cur|prev}_pending``.
    """
    keep = None if entities is None else set(entities)
    by_doc: dict[str, dict] = {}
    pending: dict[str, tuple[str, str]] = {}
    for r in conn.execute("SELECT f.entity_id, f.role, f.doc_id, f.reason_basis_json, d.xbrl_flag "
                          "FROM filing_selection f LEFT JOIN documents d ON d.doc_id = f.doc_id "
                          "WHERE f.run_id = ? AND f.doc_id IS NOT NULL ORDER BY f.doc_id, f.role", (run_id,)):
        if keep is not None and r["entity_id"] not in keep:
            continue
        t = by_doc.setdefault(r["doc_id"], {"entity_id": r["entity_id"], "roles": [], "xbrl_flag": r["xbrl_flag"]})
        t["roles"].append(r["role"])
        if include_pending:
            for d in json.loads(r["reason_basis_json"] or "{}").get("texts_pending") or []:
                if d != r["doc_id"]:
                    pending.setdefault(d, (r["entity_id"], f"{r['role']}_pending"))
    for d, (ent, role) in pending.items():
        if d in by_doc:
            continue
        flag = conn.execute("SELECT xbrl_flag FROM documents WHERE doc_id = ?", (d,)).fetchone()
        by_doc[d] = {"entity_id": ent, "roles": [role], "xbrl_flag": flag[0] if flag else None}
    return [Target(d, v["entity_id"], tuple(v["roles"]), v["xbrl_flag"]) for d, v in sorted(by_doc.items())]


# ---------------------------------------------------------------- plan
@dataclass
class PlanItem:
    target: Target
    action: str
    why: str = ""
    row: dict | None = None    # current fetches row


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)

    def count(self) -> Counter:
        return Counter(i.action for i in self.items)

    @property
    def calls(self) -> int:
        """API requests needed (backoff retries not counted)."""
        return sum(1 for i in self.items if i.action == "fetch")


def _valid_zip(path: Path) -> str | None:
    """None if ``path`` passes the same checks as an API response (§6-2), else the reason."""
    try:
        check_document_response(Response(200, {"content-type": "application/zip"}, path.read_bytes()),
                                str(path), DOC_TYPE)
    except (EdinetError, OSError) as e:
        return str(e)
    return None


def plan_fetch(conn: sqlite3.Connection, targets: list[Target], cache_dir: Path, *,
               retry_failed: bool = False) -> Plan:
    """Decide the action per docID from ``fetches`` and the cache (reads only; safe for --dry-run)."""
    rows = {r["doc_id"]: dict(r) for r in conn.execute(
        "SELECT doc_id, status, path, sha256, attempts FROM fetches WHERE type = ?", (DOC_TYPE,))}
    plan = Plan()
    for t in targets:
        row = rows.get(t.doc_id)
        p = zip_path(cache_dir, t.doc_id)
        if t.xbrl_flag == "0":
            plan.items.append(PlanItem(t, "no_xbrl", "xbrlFlag=0", row))
            continue
        status = row["status"] if row else None
        if status == "ok":
            if p.exists() and row["sha256"] and sha256_file(p) == row["sha256"]:
                plan.items.append(PlanItem(t, "have", "", row))
            else:
                why = "cached file missing" if not p.exists() else "cached file differs from the recorded sha256"
                plan.items.append(PlanItem(t, "fetch", why, row))
            continue
        if status == "failed" and not retry_failed:
            plan.items.append(PlanItem(t, "failed", "fetches.status=failed (use --retry-failed)", row))
            continue
        if p.exists():
            bad = _valid_zip(p)
            if bad is None:
                plan.items.append(PlanItem(t, "adopt", "valid ZIP already in the cache", row))
                continue
            plan.items.append(PlanItem(t, "fetch", f"cached file invalid: {bad}", row))
            continue
        plan.items.append(PlanItem(t, "fetch", {None: "not fetched yet", "pending": "pending", "retry": "retry queue",
                                                "failed": "--retry-failed", "no_xbrl": "xbrlFlag changed"}
                                   .get(status, str(status)), row))
    return plan


# ---------------------------------------------------------------- execute
@dataclass
class Result:
    run_id: str
    plan: Plan
    fetched: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)     # doc_id -> masked error (status failed)
    retry: dict[str, str] = field(default_factory=dict)      # doc_id -> masked error (status retry)
    not_fetched: list[str] = field(default_factory=list)     # needed a call but no client (offline) / stopped
    stopped: str | None = None                                # 'auth' after 401/403
    stop_error: str | None = None


def _write(conn: sqlite3.Connection, doc_id: str, run_id: str, **cols) -> None:
    if conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone() is None:
        return   # fetches.doc_id references documents (filing_selection rows always have one)
    with db.transaction(conn):
        db.upsert(conn, "fetches", {"doc_id": doc_id, "type": DOC_TYPE, "run_id": run_id, **cols},
                  key=("doc_id", "type"))


def _record_ok(conn, item: PlanItem, path: Path, run_id: str, *, attempted: bool) -> None:
    attempts = (item.row or {}).get("attempts") or 0
    now = db.now_iso()
    extra = {"last_http_status": 200, "last_attempt_at": now} if attempted else {}
    _write(conn, item.target.doc_id, run_id, status="ok", path=path.as_posix(), sha256=sha256_file(path),
           size=path.stat().st_size, attempts=attempts + (1 if attempted else 0), last_error=None,
           error_kind=None, fetched_at=now, **extra)


def _classify(e: EdinetError) -> str:
    if isinstance(e, EdinetAuthError):
        return "auth"
    if isinstance(e, EdinetNotFound):
        return "not_found"
    if e.http_status in RETRY_STATUSES or (e.api_status or "") in {str(s) for s in RETRY_STATUSES} \
            or (e.http_status is None and str(e).startswith("network error")):
        return "transient"
    return "invalid"


def execute(conn: sqlite3.Connection, plan: Plan, cache_dir: Path, *, run_id: str, client=None,
            key: str | None = None, max_attempts: int = 3) -> Result:
    """Carry out ``plan``. ``client`` None (``--offline``) -> fetch items stay queued as pending."""
    res = Result(run_id, plan)
    for item in plan.items:
        d = item.target.doc_id
        prev = item.row or {}
        if item.action == "no_xbrl":
            if prev.get("status") != "no_xbrl":
                _write(conn, d, run_id, status="no_xbrl", last_error="xbrlFlag=0: not requested (§6-2)",
                       error_kind=None, attempts=prev.get("attempts") or 0)
            continue
        if item.action == "adopt":
            _record_ok(conn, item, zip_path(cache_dir, d), run_id, attempted=False)
            res.adopted.append(d)
            continue
        if item.action != "fetch":
            continue
        if client is None or res.stopped:
            if not prev:
                _write(conn, d, run_id, status="pending", attempts=0)
            res.not_fetched.append(d)
            continue
        now = db.now_iso()
        try:
            p = client.get_document(d, DOC_TYPE, refresh=True)
        except EdinetError as e:
            msg, kind = scrub(str(e), key), _classify(e)
            if kind == "auth":
                # the key is the problem, not the document: keep it queued as it was, stop everything (§6-4)
                _write(conn, d, run_id, status=prev.get("status") if prev.get("status") in ("retry", "failed")
                       else "pending", attempts=prev.get("attempts") or 0, last_error=msg, error_kind="auth",
                       last_http_status=e.http_status, last_attempt_at=now)
                res.stopped, res.stop_error = "auth", msg
                res.not_fetched.append(d)
                log.error("stopping: %s", msg)
                continue
            attempts = (prev.get("attempts") or 0) + 1
            status = "retry" if kind == "transient" and attempts < max_attempts else "failed"
            _write(conn, d, run_id, status=status, attempts=attempts, last_error=msg, error_kind=kind,
                   last_http_status=e.http_status, last_attempt_at=now, path=None, sha256=None, size=None,
                   fetched_at=None)
            (res.retry if status == "retry" else res.failed)[d] = msg
            log.warning("fetch %s %s (%s, attempt %d): %s", d, status, kind, attempts, msg)
            continue
        except ValueError as e:   # invalid docID
            _write(conn, d, run_id, status="failed", attempts=(prev.get("attempts") or 0) + 1,
                   last_error=scrub(str(e), key), error_kind="invalid", last_attempt_at=now)
            res.failed[d] = scrub(str(e), key)
            continue
        _record_ok(conn, item, Path(p), run_id, attempted=True)
        res.fetched.append(d)
    return res


def queue_counts(conn: sqlite3.Connection, doc_ids: Iterable[str] | None = None) -> dict[str, int]:
    """fetches status counts (type=1), optionally for some docIDs."""
    rows = conn.execute("SELECT doc_id, status FROM fetches WHERE type = ?", (DOC_TYPE,)).fetchall()
    keep = None if doc_ids is None else set(doc_ids)
    return dict(sorted(Counter(r["status"] for r in rows if keep is None or r["doc_id"] in keep).items()))
