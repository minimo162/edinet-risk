import io
import zipfile

from edinet_risk.truncation import (CSV_LIMIT, classify_limit, compare_doc, compare_fact, csv_comparable,
                                    write_note)

NS = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor"


def zipped(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    buf.seek(0)
    return buf


def xbrl_zip(name, html):
    from xml.sax.saxutils import escape
    x = (f'<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
         f'xmlns:jpcrp_cor="{NS}"><jpcrp_cor:{name} contextRef="FilingDateInstant">{escape(html)}'
         f'</jpcrp_cor:{name}></xbrli:xbrl>')
    return zipped({"XBRL/PublicDoc/a.xbrl": x.encode()})


def csv_zip(name, value):
    rows = [["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"],
            [f"jpcrp_cor:{name}", "x", "FilingDateInstant", "", "", "", "", "", value]]
    data = "\r\n".join("\t".join(f'"{c}"' for c in r) for r in rows).encode("utf-16")
    return zipped({"XBRL_TO_CSV/a.csv": data})


BODY = "リスク" * 12000  # 36,000 chars, multi-byte


def test_truncated_fact_is_char_limited_prefix():
    html = f"<p>{BODY}</p><p>末尾の段落。</p>"
    fc = compare_fact("BusinessRisksTextBlock", "FilingDateInstant", BODY[:CSV_LIMIT], html)
    assert fc.truncated and fc.csv_exactly_limit and fc.norm_csv_is_prefix and not fc.norm_equal
    assert fc.csv_utf8_bytes == 3 * CSV_LIMIT and fc.cut_pos_in_xbrl == CSV_LIMIT
    assert fc.xbrl_tail.endswith("末尾の段落。")


def test_untruncated_fact_matches_despite_entities_and_images():
    html = '<p>A&amp;B社</p><img alt="図1"/><p>本文</p>'
    fc = compare_fact("BusinessRisksTextBlock", "c", "A&amp;amp;B社 本文 ", html)
    assert fc.norm_equal and not fc.truncated and fc.csv_entity_refs == 1


def test_csv_comparable_unescapes_until_stable():
    assert csv_comparable("&amp;amp;lt;") == "<"


def test_classify_limit_reports_chars_not_bytes():
    a = compare_fact("X", "c", ("あ" * CSV_LIMIT), "<p>" + "あ" * 31000 + "</p>")
    b = compare_fact("Y", "c", ("a" * (CSV_LIMIT - 10)) + "é" * 10, "<p>" + "a" * 29990 + "é" * 40 + "</p>")
    c = compare_fact("Z", "c", "短い", "<p>短い</p>")
    v = classify_limit([a, b, c])
    assert v["truncated_facts"] == 2 and v["all_truncated_are_prefix"]
    assert "csv_chars" in v["constant_at_limit"] and "csv_utf8_bytes" not in v["constant_at_limit"]
    assert v["max_untruncated_csv_chars"] == 2


def test_compare_doc_pairs_by_element_and_context_and_note_renders(tmp_path):
    rows = compare_doc(xbrl_zip("BusinessRisksTextBlock", f"<p>{BODY}</p>"),
                       csv_zip("BusinessRisksTextBlock", BODY[:CSV_LIMIT]))
    assert len(rows) == 1 and rows[0].truncated
    p = tmp_path / "note.md"
    write_note(p, [({"docID": "S100TEST", "filerName": "テスト"}, rows)], classify_limit(rows))
    s = p.read_text(encoding="utf-8")
    assert "S100TEST" in s and "30,000" in s
