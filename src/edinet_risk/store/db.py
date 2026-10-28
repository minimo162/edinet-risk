"""SQLite helpers: connection setup (WAL, foreign keys), transactions, upsert (plan §5, §6-3).

Connections are opened with ``isolation_level=None`` so that transactions are explicit:
use ``with transaction(conn): ...`` (one company / one stage per transaction, §6-3).
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from .migrate import migrate

# Fixed offset, not zoneinfo: embeddable Python on Windows has no tzdata (plan §3-1).
JST = timezone(timedelta(hours=9))

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _to_jst(ts: str | None) -> str:
    if not ts:
        return now_iso()
    dt = datetime.fromisoformat(ts)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(JST).isoformat(timespec="seconds")


def dumps(obj) -> str:
    """Canonical JSON for *_json columns (stable hashes across runs)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(path: str | Path, *, busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
    """Open a connection with the project PRAGMAs. Does not migrate (see ``open_db``)."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":  # e.g. some network filesystems
            raise sqlite3.OperationalError(f"could not enable WAL (journal_mode={mode}) for {path}")
    return conn


def open_db(path: str | Path) -> sqlite3.Connection:
    """connect() + migrate to the latest schema."""
    conn = connect(path)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """BEGIN [IMMEDIATE] ... COMMIT, ROLLBACK on any exception. Not re-entrant."""
    if conn.in_transaction:
        raise RuntimeError("nested transaction")
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"invalid SQL identifier {name!r}")
    return name


def upsert(conn: sqlite3.Connection, table: str, row: Mapping, key: Iterable[str],
           update: Iterable[str] | None = None) -> None:
    """INSERT ... ON CONFLICT(key) DO UPDATE SET <update cols> = excluded.<col>.

    ``update`` defaults to every non-key column in ``row``; pass ``()`` for insert-or-ignore.
    """
    cols = [_ident(c) for c in row]
    key = [_ident(k) for k in key]
    missing = [k for k in key if k not in row]
    if missing:
        raise ValueError(f"upsert into {table}: key column(s) {missing} missing from row")
    upd = [c for c in cols if c not in key] if update is None else [_ident(c) for c in update]
    sql = (f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
           f"ON CONFLICT ({', '.join(key)}) ")
    sql += ("DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in upd)) if upd else "DO NOTHING"
    conn.execute(sql, [row[c] for c in cols])


def insert_ignore(conn: sqlite3.Connection, table: str, row: Mapping, key: Iterable[str]) -> None:
    upsert(conn, table, row, key, update=())


def tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


# ---------------------------------------------------------------- runs / audit
def start_run(conn: sqlite3.Connection, run_id: str, cmd: str, *, code_version: str, fy: str | None = None,
              as_of: str | None = None, args: Mapping | None = None) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO runs (run_id, started_at, cmd, fy, as_of, args_json, code_version) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, now_iso(), cmd, fy, as_of, dumps(dict(args or {})), code_version))


def finish_run(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?", (status, now_iso(), run_id))


def api_call_recorder(conn: sqlite3.Connection, run_id: str | None):
    """Return an ``EdinetClient(audit_hook=...)`` callback that writes ``api_calls`` rows.

    The record handed over by the client is already key-free (masked URL, params without key).
    """
    def record(rec: Mapping) -> None:
        row = (run_id, rec.get("endpoint", ""), rec.get("params_masked", "{}"), rec.get("http_status"),
               rec.get("content_type"), rec.get("api_status"), rec.get("bytes"), rec.get("elapsed_ms"),
               _to_jst(rec.get("at")))
        sql = ("INSERT INTO api_calls (run_id, endpoint, params_masked, http_status, content_type, api_status, "
               "bytes, elapsed_ms, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        if conn.in_transaction:  # part of the caller's transaction
            conn.execute(sql, row)
        else:
            with transaction(conn):
                conn.execute(sql, row)
    return record
