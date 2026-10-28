import json
import zipfile
from collections import Counter
from datetime import date

import pytest

from edinet_risk.client import EdinetAuthError, EdinetClient, Response
from edinet_risk.m0_scan import build_crosstab, fetch_window, load_formcode_table, write_report

OK_BODY = json.dumps({"metadata": {"status": "200"}, "results": [
    {"docID": "S1", "docTypeCode": "130", "ordinanceCode": "010", "formCode": "030001", "filerName": "A"},
    {"docID": "S2", "docTypeCode": "120", "ordinanceCode": "010", "formCode": "999999", "filerName": "B"},
    {"docID": "S3", "docTypeCode": None, "ordinanceCode": None, "formCode": None},
]}).encode()


def make_client(tmp_path, script):
    """script: list of Response objects returned in order."""
    it = iter(script)
    return EdinetClient(api_key="SECRET", cache_dir=tmp_path / "cache", transport=lambda u, h, t: next(it),
                        sleep=lambda s: None, max_retries=0)


def ok():
    return Response(200, {"content-type": "application/json"}, OK_BODY)


def test_fetch_window_retries_and_records_failures(tmp_path):
    bad = Response(500, {"content-type": "text/html"}, b"<html>")
    c = make_client(tmp_path, [bad, ok(), bad, bad, ok()])
    sleeps = []
    good, failures = fetch_window(c, date(2025, 6, 1), date(2025, 6, 3), attempts=2, sleep=sleeps.append)
    assert good == ["2025-06-01", "2025-06-03"]
    assert [f["date"] for f in failures] == ["2025-06-02"]
    assert "SECRET" not in json.dumps(failures)
    # resumable: cached days are not refetched
    c2 = make_client(tmp_path, [ok()])
    good2, f2 = fetch_window(c2, date(2025, 6, 1), date(2025, 6, 3), attempts=1)
    assert len(good2) == 3 and not f2 and c2.calls == 1


def test_fetch_window_aborts_on_auth(tmp_path):
    c = make_client(tmp_path, [Response(200, {"content-type": "application/json"},
                                        b'{"StatusCode": 401, "message": "bad key"}')])
    with pytest.raises(EdinetAuthError):
        fetch_window(c, date(2025, 6, 1), date(2025, 6, 1))


def _xlsx(path):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    strings = ["府令コード", "様式コード\n（コード値）", "様式名", "書類種別", "訂正有価証券報告書", "010", "030001",
               "702001", "XBRLの修正（内国会社－有価証券報告書）"]
    sst = "".join(f"<si><t>{s}</t><rPh sb=\"0\" eb=\"1\"><t>フリガナ</t></rPh></si>" for s in strings)

    def row(n, vals):
        return f'<row r="{n}">' + "".join(f'<c r="{chr(66 + i)}{n}" t="s"><v>{v}</v></c>' for i, v in enumerate(vals)) + "</row>"
    sheet = f"<worksheet {ns}><sheetData>{row(1, [0, 1, 2, 3])}{row(2, [5, 6, 4, 4])}{row(3, [5, 7, 8, 4])}</sheetData></worksheet>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>{sst}</sst>")
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="様式コードリスト" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def test_table_and_report(tmp_path):
    p = tmp_path / "t.xlsx"
    _xlsx(p)
    table = load_formcode_table(p)
    assert table[("010", "702001")]["name"] == "XBRLの修正（内国会社－有価証券報告書）"  # furigana skipped
    assert table[("010", "030001")]["docTypeCodes"] == {"130"}
    c = make_client(tmp_path, [ok()])
    good, _ = fetch_window(c, date(2025, 6, 1), date(2025, 6, 1))
    ct, ex, hits = build_crosstab(c, good)
    unexpected = write_report(tmp_path / "out", ct, ex, hits, table, date(2025, 6, 1), date(2025, 6, 1), good, [], "t")
    assert [k for k, _ in unexpected] == [("120", "010", "999999")]
    md = (tmp_path / "out" / "formcode_crosstab.md").read_text(encoding="utf-8")
    assert "702001: 0 件（zero within period" in md and "S1" in md


def test_watch_formcode_labelled_per_ordinance(tmp_path):
    """030000 is 有価証券報告書 under 010 but a 大量保有報告書 under 060; the label must not be shared."""
    table = {("010", "030000"): {"docTypeCodes": {"120"}, "docTypeNames": set(), "name": "有価証券報告書"},
             ("060", "030000"): {"docTypeCodes": {"350"}, "docTypeNames": set(), "name": "大量保有報告書（特例対象株券等）"}}
    recs = [("A1", "120", "010"), ("A2", "120", "010"), ("B1", "350", "060")]
    hits = {"030000": [{"docID": d, "docTypeCode": dt, "ordinanceCode": oc, "filerName": d, "listDate": "2025-06-02"}
                       for d, dt, oc in recs], "030001": [], "702001": []}
    ct = Counter({(dt, oc, "030000"): n for dt, oc, n in [("120", "010", 2), ("350", "060", 1)]})
    ex = {k: ("X", "f", "d") for k in ct}
    write_report(tmp_path, ct, ex, hits, table, date(2025, 6, 1), date(2025, 6, 2), [], [], "t")
    line = next(ln for ln in (tmp_path / "formcode_crosstab.md").read_text(encoding="utf-8").splitlines()
                if ln.startswith("- **030000**"))
    assert "（公式: 有価証券報告書）" not in line.split("—")[0]
    assert "120/010 2件（公式: 有価証券報告書） 例 A1" in line
    assert "350/060 1件（公式: 大量保有報告書（特例対象株券等）） 例 B1" in line
    assert "docTypeCode と ordinanceCode を併用" in (tmp_path / "formcode_crosstab.md").read_text(encoding="utf-8")
