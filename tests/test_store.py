import csv
import sqlite3
from pathlib import Path

import pytest

from edinet_risk.__main__ import main
from edinet_risk.store import db, migrate

EXPECTED_TABLES = {"runs", "api_calls", "edinet_codes", "code_aggregation", "successor_map", "doclist_raw",
                   "documents", "fetches", "filing_selection", "extracts", "items", "matches",
                   "sentence_changes", "metrics", "population"}
LATEST = 3


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    yield c
    c.close()


def add_doc(conn, doc_id="S100AAAA"):
    db.upsert(conn, "documents", {"doc_id": doc_id, "edinet_code": "E02144"}, key=["doc_id"])


def test_open_db_creates_schema_with_pragmas(conn, tmp_path):
    assert set(db.tables(conn)) == EXPECTED_TABLES
    assert migrate.current_version(conn) == migrate.latest_version() == LATEST
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path):
    p = tmp_path / "t.sqlite"
    c = db.connect(p)
    assert migrate.migrate(c) == [1, 2, 3]
    assert migrate.current_version(c) == LATEST
    assert migrate.migrate(c) == []
    c.close()
    c = db.open_db(p)  # reopen: nothing to apply, data layout unchanged
    assert set(db.tables(c)) == EXPECTED_TABLES
    c.close()


def test_newer_database_is_rejected(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    c.execute("PRAGMA user_version = 99")
    with pytest.raises(migrate.MigrationError, match="newer"):
        migrate.migrate(c)


def test_failed_step_rolls_back(tmp_path, monkeypatch):
    real = migrate.migrations()
    monkeypatch.setattr(migrate, "migrations", lambda: real + [(4, "0004_bad.sql", "CREATE TABLE x(a); SELECT nope FROM missing;")])
    c = db.connect(tmp_path / "t.sqlite")
    with pytest.raises(migrate.MigrationError, match="0004_bad"):
        migrate.migrate(c)
    assert migrate.current_version(c) == 3  # v1-v3 committed, v4 rolled back entirely
    assert "x" not in db.tables(c)
    assert not c.in_transaction


def test_memory_db(tmp_path):
    c = db.open_db(":memory:")
    assert migrate.current_version(c) == LATEST


def test_upsert_inserts_then_updates(conn):
    db.upsert(conn, "documents", {"doc_id": "S1", "filer_name": "A", "form_code": "030000"}, key=["doc_id"])
    db.upsert(conn, "documents", {"doc_id": "S1", "filer_name": "B"}, key=["doc_id"])
    row = conn.execute("SELECT filer_name, form_code FROM documents WHERE doc_id='S1'").fetchone()
    assert (row["filer_name"], row["form_code"]) == ("B", "030000")  # columns not in row are kept
    db.insert_ignore(conn, "documents", {"doc_id": "S1", "filer_name": "C"}, key=["doc_id"])
    assert conn.execute("SELECT filer_name FROM documents").fetchone()[0] == "B"


def test_upsert_rejects_bad_identifiers(conn):
    with pytest.raises(ValueError):
        db.upsert(conn, "documents; DROP TABLE runs", {"doc_id": "S1"}, key=["doc_id"])
    with pytest.raises(ValueError):
        db.upsert(conn, "documents", {"doc_id": "S1", "x=1--": 2}, key=["doc_id"])
    with pytest.raises(ValueError, match="missing"):
        db.upsert(conn, "documents", {"filer_name": "A"}, key=["doc_id"])


def test_transaction_commit_and_rollback(conn):
    with db.transaction(conn):
        add_doc(conn, "S1")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            add_doc(conn, "S2")
            raise RuntimeError("boom")
    assert [r[0] for r in conn.execute("SELECT doc_id FROM documents")] == ["S1"]
    with db.transaction(conn):
        with pytest.raises(RuntimeError, match="nested"):
            with db.transaction(conn):
                pass


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO fetches (doc_id, type, status) VALUES ('NOPE', 1, 'ok')")
    add_doc(conn)
    conn.execute("INSERT INTO fetches (doc_id, type, status) VALUES ('S100AAAA', 1, 'pending')")


def test_check_constraints(conn):
    add_doc(conn, "C")
    add_doc(conn, "P")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO fetches (doc_id, type, status) VALUES ('C', 1, 'weird')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO matches (cur_doc, prev_doc, matcher_ver, pair_idx, input_sha256, label) "
                     "VALUES ('C', 'P', 'm1', 0, 'h', 'unknown')")
    conn.execute("INSERT INTO matches (cur_doc, prev_doc, matcher_ver, pair_idx, input_sha256, label, stage) "
                 "VALUES ('C', 'P', 'm1', 0, 'h', 'same', 'anchor')")
    # sentence_changes: add has only the cur side, del only prev, mod both
    ins = ("INSERT INTO sentence_changes (change_id, cur_doc, prev_doc, differ_ver, pair_idx, op, "
           "cur_evidence_id, prev_evidence_id) VALUES (?, 'C', 'P', 'd1', 0, ?, ?, ?)")
    conn.execute(ins, ("a", "add", "cur:C:0-5:abcd1234", None))
    conn.execute(ins, ("m", "mod", "cur:C:0-5:abcd1234", "prev:P:0-5:abcd1234"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("bad", "add", "cur:C:0-5:abcd1234", "prev:P:0-5:abcd1234"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO items (doc_id, segmenter_ver, item_idx, input_sha256, level, body, start_off, "
                     "end_off, segmentation) VALUES ('C', 's1', 0, 'h', 1, 'x', 10, 5, 'normal')")


def test_doclist_raw_is_append_only_by_key(conn):
    ins = ("INSERT INTO doclist_raw (file_date, seq_number, fetched_at, doc_id, ope_date_time, record_json) "
           "VALUES ('2026-06-20', 1, ?, 'S1', NULL, '{}')")
    conn.execute(ins, ("2026-06-21T00:00:00+09:00",))
    conn.execute(ins, ("2026-07-01T00:00:00+09:00",))  # re-scan of the same file keeps both rows
    assert conn.execute("SELECT COUNT(*) FROM doclist_raw").fetchone()[0] == 2


def test_runs_and_api_call_recorder(conn):
    db.start_run(conn, "r1", "init-db", code_version="0.0.1", fy="2026-03", args={"b": 1, "a": 2})
    rec = db.api_call_recorder(conn, "r1")
    rec({"at": "2026-06-20T03:00:00+00:00", "endpoint": "documents.json", "params_masked": '{"date":"2026-06-20"}',
         "http_status": 200, "content_type": "application/json", "api_status": "200", "bytes": 10, "elapsed_ms": 5})
    db.finish_run(conn, "r1", "ok")
    run = conn.execute("SELECT status, args_json, finished_at FROM runs").fetchone()
    assert run["status"] == "ok" and run["args_json"] == '{"a":2,"b":1}' and run["finished_at"].endswith("+09:00")
    call = conn.execute("SELECT run_id, endpoint, at FROM api_calls").fetchone()
    assert tuple(call) == ("r1", "documents.json", "2026-06-20T12:00:00+09:00")
    with pytest.raises(sqlite3.IntegrityError):
        db.finish_run(conn, "r1", "bogus")


def test_cli_init_db(tmp_path, capsys):
    p = tmp_path / "sub" / "x.sqlite"
    assert main(["init-db", "--db", str(p)]) == 0
    assert "v0 -> v3" in capsys.readouterr().out
    assert main(["init-db", "--db", str(p)]) == 0
    assert "v3 -> v3 (applied: none)" in capsys.readouterr().out


def test_cli_init_db_uses_settings(tmp_path, capsys):
    cfg = tmp_path / "s.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8")
    assert main(["init-db", "--config", str(cfg)]) == 0
    assert (tmp_path / "data" / "edinet_risk.sqlite").exists()
    cfg.write_text('[paths]\napi_key = "x"\n', encoding="utf-8")
    assert main(["init-db", "--config", str(cfg)]) == 1


# ---------------------------------------------------------------- v2: population (M2)
def test_upgrade_v1_database_keeps_data(tmp_path):
    p = tmp_path / "t.sqlite"
    c = db.connect(p)
    assert migrate.migrate(c, target=1) == [1]  # an M1-era database
    add_doc(c, "S1")
    db.start_run(c, "r1", "m1-run", code_version="0.0.1")
    assert "population" not in db.tables(c)
    c.close()
    c = db.open_db(p)
    assert migrate.current_version(c) == LATEST
    assert set(db.tables(c)) == EXPECTED_TABLES
    assert [r[0] for r in c.execute("SELECT doc_id FROM documents")] == ["S1"]
    assert c.execute("SELECT status FROM runs").fetchone()[0] == "running"
    assert migrate.migrate(c) == []
    c.close()


def test_population_columns_follow_plan(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(population)")]
    assert cols == ["as_of_month", "sec_code4", "name", "market", "sector33", "edinet_code", "match_method"]
    pk = sorted((r[5], r[1]) for r in conn.execute("PRAGMA table_info(population)") if r[5])
    assert [c for _, c in pk] == ["as_of_month", "sec_code4"]


def test_population_sec_code4_is_text_and_allows_letters(conn):
    ins = "INSERT INTO population (as_of_month, sec_code4, name, edinet_code) VALUES (?, ?, ?, ?)"
    conn.execute(ins, ("2026-09", "130A", "英字コード社", None))  # unmatched: edinet_code NULL
    conn.execute(ins, ("2026-09", 7203, "トヨタ自動車", "E02144"))  # an int still lands as TEXT
    conn.execute(ins, ("2026-08", "130A", "英字コード社", "E99999"))  # another base month
    rows = conn.execute("SELECT sec_code4, typeof(sec_code4) FROM population WHERE as_of_month = '2026-09' "
                        "ORDER BY sec_code4").fetchall()
    assert [tuple(r) for r in rows] == [("130A", "text"), ("7203", "text")]
    assert conn.execute("SELECT sec_code4 || '0' FROM population WHERE sec_code4 = '130A' "
                        "AND as_of_month = '2026-09'").fetchone()[0] == "130A0"
    with pytest.raises(sqlite3.IntegrityError):  # primary key
        conn.execute(ins, ("2026-09", "130A", "dup", None))
    for bad in ("130a", "72030", "720", "A30A"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, ("2026-09", bad, "x", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("2026/09", "1301", "x", None))


def test_successor_map_csv_header_matches_table(conn):
    path = Path(__file__).resolve().parents[1] / "config" / "successor_map.csv"
    with path.open(encoding="utf-8", newline="") as f:
        header = next(csv.reader(f))
    assert header == [r[1] for r in conn.execute("PRAGMA table_info(successor_map)")]
