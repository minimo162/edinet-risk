"""Forward-only schema migrations keyed by ``PRAGMA user_version`` (plan §5).

Version 1 is the baseline ``schema.sql``. Later changes go into
``store/migrations/NNNN_<name>.sql`` (NNNN >= 0002) and are applied in order.
Each step runs in a single transaction together with the ``user_version`` bump,
so a failed step leaves the database at the previous version.
"""
from __future__ import annotations

import re
import sqlite3
from importlib import resources

_STEP_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


def migrations() -> list[tuple[int, str, str]]:
    """[(version, name, sql)] in ascending version order."""
    pkg = resources.files(__package__)
    steps = [(1, "schema.sql", (pkg / "schema.sql").read_text(encoding="utf-8"))]
    mdir = pkg / "migrations"
    if mdir.is_dir():
        for f in sorted(mdir.iterdir(), key=lambda p: p.name):
            m = _STEP_RE.match(f.name)
            if m:
                steps.append((int(m.group(1)), f.name, f.read_text(encoding="utf-8")))
    versions = [v for v, _, _ in steps]
    if versions != list(range(1, len(steps) + 1)):
        raise MigrationError(f"migration versions must be contiguous from 1, got {versions}")
    return steps


def latest_version() -> int:
    return len(migrations())


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, target: int | None = None) -> list[int]:
    """Bring the schema up to ``target`` (default: latest). Returns the versions applied."""
    steps = migrations()
    target = len(steps) if target is None else target
    cur = current_version(conn)
    if cur > len(steps):
        raise MigrationError(f"database schema v{cur} is newer than this code (v{len(steps)}); "
                             "upgrade edinet-risk instead of downgrading the database")
    if conn.in_transaction:
        raise MigrationError("migrate() must not run inside an open transaction")
    applied = []
    for version, name, sql in steps:
        if version <= cur or version > target:
            continue
        # executescript() runs statements verbatim; the explicit BEGIN/COMMIT makes the step atomic
        # (user_version is part of the database header and is rolled back with the transaction).
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.rollback()
            raise MigrationError(f"migration {name} (v{version}) failed: {e}") from e
        applied.append(version)
    return applied
