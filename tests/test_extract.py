import io
import zipfile

import pytest

from edinet_risk.extract import extract_from_xbrl_zip, find_csv_facts, html_to_text

NS = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor"
HTML = ('<h3 class="smt_head2">３ 【事業等のリスク】</h3><p>前文です。</p><h4>(1) 経済情勢</h4>'
        '<p>本文&amp;続き</p><table><tr><td><p>区分</p></td><td>内容</td></tr>'
        '<tr><td>A</td><td>説明</td></tr></table><ul><li>項目1</li><li>・項目2</li></ul>')


def xbrl(*facts):
    from xml.sax.saxutils import escape
    body = "".join(f'<jpcrp_cor:{n} contextRef="FilingDateInstant">{escape(v)}</jpcrp_cor:{n}>' for n, v in facts)
    return f'<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:jpcrp_cor="{NS}">{body}</xbrli:xbrl>'.encode()


def zipped(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_exact_element_match_ignores_going_concern_block():
    z = zipped({"XBRL/PublicDoc/a.xbrl": xbrl(
        ("MaterialMattersRelatingToGoingConcernEtcBusinessRisksTextBlock", "<p>GC</p>"),
        ("BusinessRisksTextBlock", HTML))})
    tb = extract_from_xbrl_zip(z)
    assert tb.html == HTML and tb.context_ref == "FilingDateInstant"


def test_missing_or_duplicate_block_raises():
    with pytest.raises(ValueError):
        extract_from_xbrl_zip(zipped({"XBRL/PublicDoc/a.xbrl": xbrl(("Other", "x"))}))


def test_html_to_text_structure():
    text, removed = html_to_text(HTML)
    assert removed
    assert text.splitlines() == ["前文です。", "(1) 経済情勢", "本文&続き", "区分\t内容", "A\t説明", "・項目1", "・項目2"]


def test_csv_utf16_tsv():
    rows = [["要素ID", "項目名", "コンテキストID", "値"],
            ["jpcrp_cor:MaterialMattersRelatingToGoingConcernEtcBusinessRisksTextBlock", "x", "c", "GC"],
            ["jpcrp_cor:BusinessRisksTextBlock", "事業等のリスク", "FilingDateInstant", "本文"]]
    data = "\r\n".join("\t".join(f'"{c}"' for c in r) for r in rows).encode("utf-16")
    facts = find_csv_facts(io.BytesIO(zipped({"XBRL_TO_CSV/a.csv": data})))
    assert len(facts) == 1 and facts[0].value == "本文" and facts[0].context_id == "FilingDateInstant"


def test_nested_table_inside_cell_is_flattened_not_dropped():
    html = ('<table><tr><td>役職</td><td><table><tr><td>孫</td><td>正義</td></tr></table></td>'
            '<td>(注3)</td></tr><tr><td>A</td><td>B</td></tr></table>')
    text, _ = html_to_text(html)
    assert text.splitlines() == ["役職\t孫 正義\t(注3)", "A\tB"]
