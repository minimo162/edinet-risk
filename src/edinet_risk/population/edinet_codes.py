"""`codes sync`: EDINET code list (Edinetcode.zip) and code aggregation list (ESE140190.csv) -> SQLite.

Sources (official download host, no API key needed):
  * ``Edinetcode.zip`` -> ``EdinetcodeDlInfo.csv``. cp932. Row 1 is metadata
    (``ダウンロード実行日,2026年10月28日現在,件数,11433件``), row 2 the header
    (``ＥＤＩＮＥＴコード,提出者種別,上場区分,…,証券コード,提出者法人番号``), then one quoted row per code.
    ``上場区分`` is ``上場`` / ``非上場`` / empty (individuals, non-filers). ``証券コード`` is 5 characters
    and may contain letters (``130A0``).
  * ``ESE140190.csv`` (EDINETコード集約一覧). cp932. Row 1 is the title, row 2 the header
    (``集約処理日,廃止EDINETコード,継続EDINETコード``), then ``YYYY-MM-DD,E…,E…``.

Cache (plan §6-3): ``{cache}/codes/{name}`` plus ``{cache}/codes/manifest.json`` with url, sha256, size and
fetched_at per file. A cached file is used as-is unless ``refresh``; a file whose sha256 does not match the
manifest is an error. A download is validated (parsed) before it is written to the cache.

DB: ``edinet_codes`` / ``code_aggregation`` are upserted in one transaction; only new or changed rows are
written, so re-importing the same files changes nothing. Codes that disappeared from the list are kept
(reported as ``stale``). ``fetched_at`` is when the source file was downloaded (file mtime for
``--from-file``), not the import time, so the tables depend only on the input files.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..net.download import Downloader
from ..net.edinet_api import EdinetError
from ..net.http import is_offline
from ..store.db import JST, now_iso, transaction, upsert

EDINETCODE_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
AGGREGATION_URL = "https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140190.csv"
EDINETCODE_FILE = "Edinetcode.zip"
AGGREGATION_FILE = "ESE140190.csv"
ENCODING = "cp932"

_EDINET_CODE_RE = re.compile(r"^E\d{5}$")
_SEC_CODE5_RE = re.compile(r"^[0-9][0-9A-Z]{3}[0-9]$")
_LISTED = {"上場": 1, "非上場": 0, "": None}


class CodesError(Exception):
    """Unusable code list (format, cache, offline without cache). Never contains secrets."""


@dataclass
class CodeList:
    list_date: str | None      # ダウンロード実行日 as YYYY-MM-DD
    declared_count: int
    rows: list[dict]           # edinet_code, sec_code5, name, listed


@dataclass
class Source:
    data: bytes
    origin: str                # 'cache' | 'download' | 'file'
    path: Path
    sha256: str
    fetched_at: str


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip()


def _decode(data: bytes, what: str) -> list[list[str]]:
    try:
        text = data.decode(ENCODING)
    except UnicodeDecodeError as e:
        raise CodesError(f"{what}: not {ENCODING} text ({e})") from None
    return list(csv.reader(io.StringIO(text, newline="")))


def _header_index(rows: list[list[str]], required: list[str], what: str) -> tuple[int, dict[str, int]]:
    for i, row in enumerate(rows[:5]):
        cells = [_norm(c) for c in row]
        if all(r in cells for r in required):
            return i, {r: cells.index(r) for r in required}
    raise CodesError(f"{what}: header with columns {required} not found in the first rows")


# ---------------------------------------------------------------- parsers
def parse_edinetcode_zip(data: bytes) -> CodeList:
    what = EDINETCODE_FILE
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        bad = zf.testzip()
    except zipfile.BadZipFile as e:
        raise CodesError(f"{what}: not a valid ZIP ({e})") from None
    if bad is not None:
        raise CodesError(f"{what}: ZIP CRC error in {bad}")
    csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(csvs) != 1:
        raise CodesError(f"{what}: expected exactly one CSV in the ZIP, got {csvs}")
    rows = _decode(zf.read(csvs[0]), f"{what}/{csvs[0]}")
    what = f"{what}/{csvs[0]}"

    hi, col = _header_index(rows, ["EDINETコード", "上場区分", "提出者名", "証券コード"], what)
    meta = [_norm(c) for c in (rows[0] if hi > 0 else [])]
    if "件数" not in meta or meta.index("件数") + 1 >= len(meta):
        raise CodesError(f"{what}: metadata row (ダウンロード実行日…件数) not found")
    m = re.fullmatch(r"([\d,]+)件?", meta[meta.index("件数") + 1])
    if not m:
        raise CodesError(f"{what}: unreadable 件数 {meta[meta.index('件数') + 1]!r}")
    declared = int(m.group(1).replace(",", ""))
    list_date = None
    if "ダウンロード実行日" in meta and meta.index("ダウンロード実行日") + 1 < len(meta):
        d = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", meta[meta.index("ダウンロード実行日") + 1])
        if d:
            list_date = date(*map(int, d.groups())).isoformat()

    out, seen, bad_rows = [], set(), []
    width = max(col.values()) + 1
    for n, row in enumerate(rows[hi + 1:], start=hi + 2):
        if not any(c.strip() for c in row):
            continue
        if len(row) < width:
            bad_rows.append(f"line {n}: {len(row)} columns")
            continue
        code = row[col["EDINETコード"]].strip()
        sec = _norm(row[col["証券コード"]]).upper()
        listed_raw = row[col["上場区分"]].strip()
        if not _EDINET_CODE_RE.match(code):
            bad_rows.append(f"line {n}: EDINETコード {code!r}")
        elif code in seen:
            bad_rows.append(f"line {n}: duplicate {code}")
        elif sec and not _SEC_CODE5_RE.match(sec):
            bad_rows.append(f"line {n}: 証券コード {sec!r}")
        elif listed_raw not in _LISTED:
            bad_rows.append(f"line {n}: 上場区分 {listed_raw!r}")
        else:
            seen.add(code)
            out.append({"edinet_code": code, "sec_code5": sec or None, "name": row[col["提出者名"]].strip(),
                        "listed": _LISTED[listed_raw]})
    if bad_rows:
        raise CodesError(f"{what}: {len(bad_rows)} bad row(s): " + "; ".join(bad_rows[:5]))
    if len(out) != declared:
        raise CodesError(f"{what}: {len(out)} rows but the metadata row says 件数 {declared} (truncated file?)")
    return CodeList(list_date, declared, out)


def parse_aggregation_csv(data: bytes) -> list[dict]:
    what = AGGREGATION_FILE
    rows = _decode(data, what)
    hi, col = _header_index(rows, ["集約処理日", "廃止EDINETコード", "継続EDINETコード"], what)
    out: dict[str, dict] = {}
    bad_rows = []
    for n, row in enumerate(rows[hi + 1:], start=hi + 2):
        if not any(c.strip() for c in row):
            continue
        try:
            d = date.fromisoformat(_norm(row[col["集約処理日"]]).replace("/", "-")).isoformat()
            old, new = _norm(row[col["廃止EDINETコード"]]), _norm(row[col["継続EDINETコード"]])
        except (IndexError, ValueError):
            bad_rows.append(f"line {n}: {row!r}")
            continue
        if not (_EDINET_CODE_RE.match(old) and _EDINET_CODE_RE.match(new)) or old == new:
            bad_rows.append(f"line {n}: {old!r} -> {new!r}")
            continue
        prev = out.get(old)
        if prev is not None and prev["new_code"] != new and prev["source_date"] == d:
            bad_rows.append(f"line {n}: {old} aggregated into both {prev['new_code']} and {new} on {d}")
        elif prev is None or d > prev["source_date"]:
            out[old] = {"old_code": old, "new_code": new, "source_date": d}
    if bad_rows:
        raise CodesError(f"{what}: {len(bad_rows)} bad row(s): " + "; ".join(bad_rows[:5]))
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- cache
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_path(codes_dir: Path) -> Path:
    return codes_dir / "manifest.json"


def _read_manifest(codes_dir: Path) -> dict:
    p = _manifest_path(codes_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CodesError(f"{p}: unreadable manifest ({e}); rerun with --refresh") from None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _load_or_fetch(codes_dir: Path, name: str, url: str, parse, *, refresh: bool,
                   downloader: Downloader | None) -> tuple[Source, object]:
    path = codes_dir / name
    entry = _read_manifest(codes_dir).get(name)
    if path.exists() and not refresh:
        data = path.read_bytes()
        if entry is None or entry.get("sha256") != _sha256(data):
            raise CodesError(f"{path}: sha256 does not match {_manifest_path(codes_dir)} "
                             "(edited or partial file); rerun with --refresh")
        return Source(data, "cache", path, entry["sha256"], entry["fetched_at"]), parse(data)
    if is_offline():
        raise CodesError(f"--offline: {path} is not cached" + (" (and --refresh needs the network)" if refresh else "")
                         + "; run `codes sync` online once, or import a local copy with --from-file")
    if downloader is None:
        raise CodesError(f"{path} is not cached and no downloader is configured")
    resp = downloader.get(url, endpoint=f"codes/{name}")
    parsed = parse(resp.body)  # validate before caching (an HTML error page never lands in the cache)
    src = Source(resp.body, "download", path, _sha256(resp.body), now_iso())
    _write_atomic(path, resp.body)
    manifest = _read_manifest(codes_dir)
    manifest[name] = {"url": url, "sha256": src.sha256, "bytes": len(resp.body), "fetched_at": src.fetched_at,
                      "content_type": resp.content_type}
    _write_atomic(_manifest_path(codes_dir), json.dumps(manifest, ensure_ascii=False, indent=2,
                                                        sort_keys=True).encode("utf-8"))
    return src, parsed


def _from_file(path: Path, parse) -> tuple[Source, object]:
    if not path.is_file():
        raise CodesError(f"--from-file: {path} not found")
    data = path.read_bytes()
    mtime = datetime.fromtimestamp(path.stat().st_mtime, JST).isoformat(timespec="seconds")
    return Source(data, "file", path, _sha256(data), mtime), parse(data)


# ---------------------------------------------------------------- sync
def _upsert_changed(conn: sqlite3.Connection, table: str, key: str, rows: list[dict]) -> dict:
    cols = list(rows[0]) if rows else []
    existing = {r[key]: dict(r) for r in conn.execute(f"SELECT * FROM {table}")}
    new = changed = 0
    for row in rows:
        old = existing.get(row[key])
        if old is not None and all(old[c] == row[c] for c in cols):
            continue
        new += old is None
        changed += old is not None
        upsert(conn, table, row, key=[key])
    keys = {r[key] for r in rows}
    return {"rows": len(rows), "new": new, "changed": changed,
            "unchanged": len(rows) - new - changed, "stale": sum(1 for k in existing if k not in keys)}


def sync(conn: sqlite3.Connection, cache_dir: Path, *, downloader: Downloader | None = None, refresh: bool = False,
         from_file: Path | None = None, aggregation_file: Path | None = None) -> dict:
    """Load both lists (file / cache / download) and upsert them. Returns a summary dict."""
    codes_dir = Path(cache_dir) / "codes"
    if from_file is not None:
        csrc, codes = _from_file(from_file, parse_edinetcode_zip)
    else:
        csrc, codes = _load_or_fetch(codes_dir, EDINETCODE_FILE, EDINETCODE_URL, parse_edinetcode_zip,
                                     refresh=refresh, downloader=downloader)
    if aggregation_file is not None:
        asrc, agg = _from_file(aggregation_file, parse_aggregation_csv)
    else:
        asrc, agg = _load_or_fetch(codes_dir, AGGREGATION_FILE, AGGREGATION_URL, parse_aggregation_csv,
                                   refresh=refresh, downloader=downloader)

    code_rows = [{**r, "fetched_at": csrc.fetched_at} for r in codes.rows]
    with transaction(conn):
        c_stats = _upsert_changed(conn, "edinet_codes", "edinet_code", code_rows)
        a_stats = _upsert_changed(conn, "code_aggregation", "old_code", agg)
    known = {r["edinet_code"] for r in codes.rows}
    return {
        "edinet_codes": {**c_stats, "listed": sum(r["listed"] == 1 for r in codes.rows),
                         "unlisted": sum(r["listed"] == 0 for r in codes.rows),
                         "listed_blank": sum(r["listed"] is None for r in codes.rows),
                         "with_sec_code": sum(r["sec_code5"] is not None for r in codes.rows),
                         "list_date": codes.list_date, "origin": csrc.origin, "path": str(csrc.path),
                         "sha256": csrc.sha256, "fetched_at": csrc.fetched_at},
        "code_aggregation": {**a_stats, "origin": asrc.origin, "path": str(asrc.path), "sha256": asrc.sha256,
                             "fetched_at": asrc.fetched_at,
                             "new_code_unknown": sorted({r["new_code"] for r in agg} - known)},
        "network_calls": downloader.calls if downloader is not None else 0,
    }


def fiscal_year_ends(data: bytes) -> dict[str, str]:
    """edinet_code -> 決算日 as 'MM-DD' from Edinetcode.zip (not stored in edinet_codes; used by resolve)."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
    rows = _decode(zf.read(name), f"{EDINETCODE_FILE}/{name}")
    hi, col = _header_index(rows, ["EDINETコード", "決算日"], EDINETCODE_FILE)
    out = {}
    for r in rows[hi + 1:]:
        if len(r) <= max(col.values()):
            continue
        m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", _norm(r[col["決算日"]]))
        if m:
            out[_norm(r[col["EDINETコード"]])] = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return out
