"""M2 step N: ``extract`` — BusinessRisks text of every fetched type=1 body -> ``extracts`` (offline).

Same extraction as M0/M1 (``extract.find_textblocks`` + ``html_to_text``, ``EXTRACTOR_VER``) and the same
row shape as ``m1_compare._extract_row``. Targets are the ``fetches`` rows with type=1, status=ok and an
existing file. A row that already exists for (doc_id, EXTRACTOR_VER) with the same ZIP hash is kept
(no re-extraction); ``force`` re-extracts.

Outcomes are rows, not exceptions, so monitor / resolve see them:
- exactly one TextBlock: the text, hit_count=1;
- several: hit_count=N, the first fact's text, warning ``multi_hit`` (monitor lists it);
- none / no PublicDoc XBRL / unreadable ZIP: text '' and char_len=0 (``empty_extract``) with the reason in
  ``warnings_json``.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..extract import EXTRACTOR_VER, find_textblocks, html_to_text
from ..store import db

log = logging.getLogger(__name__)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def extract_row(doc_id: str, path: Path) -> dict:
    raw = Path(path).read_bytes()
    row = {"doc_id": doc_id, "extractor_ver": EXTRACTOR_VER, "input_sha256": _sha(raw), "source": "xbrl",
           "raw_html_sha": None, "text": "", "char_len": 0, "img_count": 0, "hit_count": 0}
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(path)
        members = [n for n in zf.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")]
        found = [(html, ctx, m) for m in members for html, ctx in find_textblocks(zf.read(m))]
    except Exception as e:   # BadZipFile, OSError, ET.ParseError ...: recorded, not raised
        warnings.append(f"unreadable: {type(e).__name__}: {e}"[:300])
        return row | {"warnings_json": db.dumps(warnings)}
    if not members:
        warnings.append("no XBRL/PublicDoc/*.xbrl")
    elif not found:
        warnings.append("no BusinessRisksTextBlock")
    else:
        html = found[0][0]
        if len(found) > 1:
            warnings.append(f"multi_hit: {len(found)} facts, first used ({found[0][1]})")
        text, _ = html_to_text(html)
        row |= {"raw_html_sha": hashlib.sha256(html.encode("utf-8")).hexdigest(), "text": text,
                "char_len": len(text), "img_count": html.lower().count("<img"), "hit_count": len(found)}
        if not text:
            warnings.append("empty text")
    return row | {"warnings_json": db.dumps(warnings)}


@dataclass
class Result:
    extracted: int = 0
    kept: int = 0
    missing_file: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    multi: list[str] = field(default_factory=list)


def run(conn: sqlite3.Connection, *, doc_ids: set[str] | None = None, force: bool = False,
        batch: int = 50) -> Result:
    """Extract every fetched body not yet extracted; one transaction per ``batch`` rows (resumable)."""
    res = Result()
    have = {r[0]: r[1] for r in conn.execute("SELECT doc_id, input_sha256 FROM extracts WHERE extractor_ver = ?",
                                             (EXTRACTOR_VER,))}
    todo = []
    for r in conn.execute("SELECT doc_id, path, sha256 FROM fetches WHERE type = 1 AND status = 'ok' "
                          "ORDER BY doc_id").fetchall():
        d = r["doc_id"]
        if doc_ids is not None and d not in doc_ids:
            continue
        if not r["path"] or not Path(r["path"]).exists():
            res.missing_file.append(d)
        elif not force and have.get(d) == r["sha256"]:
            res.kept += 1
        else:
            todo.append((d, Path(r["path"])))
    for i in range(0, len(todo), batch):
        rows = [extract_row(d, p) for d, p in todo[i:i + batch]]
        with db.transaction(conn):
            for row in rows:
                db.upsert(conn, "extracts", row, key=("doc_id", "extractor_ver"))
        for row in rows:
            res.extracted += 1
            if row["char_len"] == 0 or row["hit_count"] == 0:
                res.empty.append(row["doc_id"])
            if row["hit_count"] > 1:
                res.multi.append(row["doc_id"])
    return res
