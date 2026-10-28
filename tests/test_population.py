from pathlib import Path

import pytest
from openpyxl import Workbook

from edinet_risk.__main__ import main
from edinet_risk.population import jpx
from edinet_risk.store import db

HDR = ["日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分", "17業種コード", "17業種区分",
       "規模コード", "規模区分"]
PRIME = "プライム（内国株式）"


def row(code, name, market=PRIME, sector="輸送用機器", d=20260930):
    return [d, code, name, market, 3700, sector, 6, "自動車・輸送機", 1, "TOPIX Core30"]


def write_xlsx(path: Path, rows, header=HDR) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


ROWS = [
    row(7203, "トヨタ自動車"),
    row("130A", "ヴェルトハーモニー", sector="サービス業"),       # letter code, string cell
    row(1305, "ｉＦｒｅｅＥＴＦ", market="ETF・ETN", sector="-"),
    row(1376, "カネコ種苗", market="スタンダード（内国株式）", sector="水産・農林業"),
    row(9999, "コード不一致社"),
    row(25935, "伊藤園第１種優先株式", sector="食料品"),          # class share -> excluded
    row(7267, "本田技研工業"),
]
CODES = [
    ("E02144", "72030", "トヨタ自動車株式会社", 1),
    ("E39999", "130A0", "ヴェルトハーモニー", 1),
    ("E00001", "13760", "カネコ種苗株式会社", 1),
    ("E02166", "72670", "本田技研工業株式会社", 1),
    ("E09999", "72670", "旧本田（非上場）", 0),                   # second candidate, not listed
]


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.sqlite")
    c.executemany("INSERT INTO edinet_codes (edinet_code, sec_code5, name, listed, fetched_at) "
                  "VALUES (?, ?, ?, ?, '2026-10-28T00:00:00+09:00')", CODES)
    yield c
    c.close()


def population(conn, month="2026-09"):
    return {r["sec_code4"]: dict(r) for r in conn.execute(
        "SELECT * FROM population WHERE as_of_month = ? ORDER BY sec_code4", (month,))}


def test_prime_filter_letter_codes_and_join(conn, tmp_path):
    res = jpx.load(conn, write_xlsx(tmp_path / "data_j.xlsx", ROWS), "2026-09")
    pop = population(conn)
    assert sorted(pop) == ["130A", "7203", "7267", "9999"]          # ETF / スタンダード / 優先株 dropped
    assert all(isinstance(k, str) for k in pop)
    assert pop["130A"]["edinet_code"] == "E39999" and pop["130A"]["match_method"] == "sec_code"
    assert pop["7203"] == {"as_of_month": "2026-09", "sec_code4": "7203", "name": "トヨタ自動車",
                             "market": PRIME, "sector33": "輸送用機器", "edinet_code": "E02144",
                             "match_method": "sec_code"}
    assert (pop["7267"]["edinet_code"], pop["7267"]["match_method"]) == ("E02166", "sec_code_listed")
    assert res["multi_candidates"] == {"7267": ["E02166", "E09999"]}
    assert res["match_method"] == {"sec_code": 2, "sec_code_listed": 1, "unmatched": 1}
    assert res["excluded_class_shares"] == [("25935", "伊藤園第１種優先株式")]
    assert res["list_date"] == "2026-09-30" and res["file_rows"] == len(ROWS)


def test_unmatched_code_has_null_edinet_code(conn, tmp_path):
    res = jpx.load(conn, write_xlsx(tmp_path / "d.xlsx", ROWS), "2026-09")
    p = population(conn)["9999"]
    assert p["edinet_code"] is None and p["match_method"] == "unmatched"
    assert res["unmatched"] == [("9999", "コード不一致社")]


def test_ambiguous_when_no_single_listed_candidate(conn, tmp_path):
    conn.execute("INSERT INTO edinet_codes (edinet_code, sec_code5, name, listed, fetched_at) "
                 "VALUES ('E08888', '72030', '別会社', 1, 'x')")
    jpx.load(conn, write_xlsx(tmp_path / "d.xlsx", ROWS), "2026-09")
    p = population(conn)["7203"]
    assert p["edinet_code"] is None and p["match_method"] == "ambiguous"


def test_reload_same_month_replaces(conn, tmp_path):
    f = write_xlsx(tmp_path / "d.xlsx", ROWS)
    jpx.load(conn, f, "2026-09")
    first = population(conn)
    res = jpx.load(conn, f, "2026-09")
    assert population(conn) == first and res["replaced"] == len(first)
    # a smaller list for the same month replaces (not merges); other months are untouched
    jpx.load(conn, write_xlsx(tmp_path / "d8.xlsx", [row(7203, "トヨタ自動車", d=20260831)]), "2026-08")
    jpx.load(conn, write_xlsx(tmp_path / "d2.xlsx", [row(7203, "トヨタ自動車")]), "2026-09")
    assert sorted(population(conn)) == ["7203"]
    assert sorted(population(conn, "2026-08")) == ["7203"]


def test_date_must_match_as_of_month(conn, tmp_path):
    with pytest.raises(jpx.JpxError, match="not in --as-of-month 2026-10"):
        jpx.load(conn, write_xlsx(tmp_path / "d.xlsx", ROWS), "2026-10")
    assert population(conn, "2026-10") == {}


@pytest.mark.parametrize("bad", ["2026-9", "202609", "2026-13", "26-09", ""])
def test_as_of_month_format(bad):
    with pytest.raises(jpx.JpxError):
        jpx.check_as_of_month(bad)


def test_bad_code_in_prime_is_error(conn, tmp_path):
    with pytest.raises(jpx.JpxError, match="コード"):
        jpx.load(conn, write_xlsx(tmp_path / "d.xlsx", ROWS + [row("12-4", "壊れ")]), "2026-09")
    assert population(conn) == {}


def test_legacy_xls_and_missing_header(conn, tmp_path):
    xls = tmp_path / "data_j.xls"
    xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
    with pytest.raises(jpx.JpxError, match=r"\.xls"):
        jpx.load(conn, xls, "2026-09")
    with pytest.raises(jpx.JpxError, match="header"):
        jpx.load(conn, write_xlsx(tmp_path / "h.xlsx", ROWS, header=["a", "b", "c", "d"]), "2026-09")


def test_empty_edinet_codes_is_error(tmp_path):
    c = db.open_db(tmp_path / "e.sqlite")
    try:
        with pytest.raises(jpx.JpxError, match="codes sync"):
            jpx.load(c, write_xlsx(tmp_path / "d.xlsx", ROWS), "2026-09")
    finally:
        c.close()


def test_cli_load_and_required_as_of_month(conn, tmp_path, capsys):
    f = write_xlsx(tmp_path / "d.xlsx", ROWS)
    dbp = tmp_path / "t.sqlite"
    with pytest.raises(SystemExit) as e:
        main(["population", "load", "--jpx", str(f), "--db", str(dbp)])
    assert e.value.code == 2 and "--as-of-month" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        main(["population", "load", "--jpx", str(f), "--db", str(dbp), "--as-of-month", "2026/09"])
    assert e.value.code == 2 and "YYYY-MM" in capsys.readouterr().err
    assert main(["population", "load", "--jpx", str(f), "--db", str(dbp), "--as-of-month", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "4 rows" in out and "sec_code=2" in out and "unmatched=1" in out and "unmatched 9999" in out
    assert main(["population", "load", "--jpx", str(tmp_path / "nope.xlsx"), "--db", str(dbp),
                 "--as-of-month", "2026-09"]) == 2
