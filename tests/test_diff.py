"""diff/: sentence split + evidence IDs, alignment, minor changes, character diff (plan §3 diff/, §6-6, §7)."""
import json

import pytest

from edinet_risk.diff import chardiff, minor, sentences
from edinet_risk.diff.chardiff import Segment
from edinet_risk.diff.sentences import Sentence

DOC_PREV = "（１）為替変動リスク\n当社は2025年3月期において為替の影響を受けました。円高が進むと利益が減少します。\n"
DOC_CUR = "（１）為替変動リスク\n当社は2026年3月期において為替の影響を受けました。円高が進むと営業利益が大きく減少します。\n" \
          "新たに地政学リスクが高まっています。\n"


def _sents(text: str) -> list[Sentence]:
    return sentences.split_sentences(text)


# ---------------------------------------------------------------- minor

@pytest.mark.parametrize("prev,cur,kind", [
    ("同じ文。", "同じ文。", "identical"),
    ("円高が 進む。", "円高が　進む。\n", "whitespace"),
    ("ＡＢＣ社の売上―高。", "abc社の売上-高。", "notation"),
    ("2025年3月期の売上は1,234億円。", "２０２６年３月期の売上は2,345億円。", "number"),
    ("令和6年に開始。", "令和元年に開始。", "number"),
    ("円高が進む。", "円安が進む。", None),
    ("一部の地域。", "二部の地域。", None),   # kanji numerals are words, not masked
])
def test_minor_classify(prev, cur, kind):
    assert minor.classify(prev, cur) == kind
    assert minor.is_minor(prev, cur) is (kind is not None)


def test_minor_kinds_order():
    assert minor.KINDS == ("identical", "whitespace", "notation", "number")
    assert minor.mask_numbers("令和7年 3.5%") == "<NUM>年<NUM>%"


# ---------------------------------------------------------------- sentences

def test_split_on_kuten_and_newline_with_offsets():
    text = "  見出し\n第一文です。第二文（注記。）です。「引用。」\n\n　最後"
    ss = sentences.split_sentences(text, base=100)
    assert [s.text for s in ss] == ["見出し", "第一文です。", "第二文（注記。）", "です。", "「引用。」", "最後"]
    for s in ss:
        assert text[s.start - 100:s.end - 100] == s.text


def test_split_blank_and_trailing():
    assert sentences.split_sentences("") == []
    assert sentences.split_sentences(" \n　\n") == []
    assert [s.text for s in sentences.split_sentences("終わりなし")] == ["終わりなし"]


def test_item_sentences_use_document_offsets():
    doc = "前文。\n（１）A\n本文A。\n（２）B\n本文B1。本文B2。\n"
    a, b = doc.index("（１）"), doc.index("（２）")
    items = [{"start_off": b, "end_off": len(doc)}, {"start_off": a, "end_off": b}]  # given order is kept
    ss = sentences.item_sentences(doc, items)
    assert [s.text for s in ss] == ["（２）B", "本文B1。", "本文B2。", "（１）A", "本文A。"]
    assert all(doc[s.start:s.end] == s.text for s in ss)


def test_evidence_id_round_trip():
    ss = _sents(DOC_CUR)
    s = ss[2]
    eid = sentences.evidence_id("cur", "S100TEST", s)
    side, doc, st, en, h = sentences.parse_evidence_id(eid)
    assert (side, doc, st, en) == ("cur", "S100TEST", s.start, s.end) and len(h) == 8
    assert sentences.resolve_evidence(eid, DOC_CUR) == s.text
    with pytest.raises(ValueError):
        sentences.resolve_evidence(eid, DOC_CUR.replace("営業", "経常"))
    with pytest.raises(ValueError):
        sentences.evidence_id("both", "S100TEST", s)
    with pytest.raises(ValueError):
        sentences.parse_evidence_id("cur:S100TEST:1-2")


def test_align_covers_everything_in_order():
    p, c = _sents(DOC_PREV), _sents(DOC_CUR)
    rows = sentences.align(p, c)
    assert [r.op for r in rows] == ["equal", "mod", "mod", "add"]
    assert [r.minor_kind for r in rows] == [None, "number", None, None]
    assert sorted(r.prev for r in rows if r.prev is not None) == list(range(len(p)))
    assert sorted(r.cur for r in rows if r.cur is not None) == list(range(len(c)))


def test_align_replaced_block_pairs_by_similarity():
    p = _sents("全く別の話題について述べます。\n供給網の混乱により部品調達が遅延する可能性があります。")
    c = _sents("供給網の混乱により部品調達が大幅に遅延する可能性があります。\n新しい規制が導入されました。")
    rows = sentences.align(p, c)
    assert [(r.op, r.prev, r.cur) for r in rows] == [("del", 0, None), ("mod", 1, 0), ("add", None, 1)]


def test_align_one_side_empty():
    c = _sents("新設の項目。二文目。")
    assert [r.op for r in sentences.align([], c)] == ["add", "add"]
    assert [r.op for r in sentences.align(c, [])] == ["del", "del"]
    assert sentences.align([], []) == []


def test_changes_ids_context_and_llm_filter():
    p, c = _sents(DOC_PREV), _sents(DOC_CUR)
    chs = sentences.changes(p, c, cur_doc="S100CUR", prev_doc="S100PRV")
    assert [(x.op, x.is_minor) for x in chs] == [("mod", True), ("mod", False), ("add", False)]
    mod, add = chs[1], chs[2]
    assert sentences.resolve_evidence(mod.cur_evidence_id, DOC_CUR) == mod.cur.text
    assert sentences.resolve_evidence(mod.prev_evidence_id, DOC_PREV) == mod.prev.text
    assert mod.cur_evidence_id.startswith("cur:S100CUR:") and mod.prev_evidence_id.startswith("prev:S100PRV:")
    assert add.prev is None and add.prev_evidence_id is None
    assert add.context == {"cur_before": c[2].text}           # last sentence: no "after"
    assert mod.context["cur_after"] == c[3].text and mod.context["prev_before"] == p[1].text
    assert all(":" not in v or "S100" not in v for v in mod.context.values())   # context carries no IDs
    assert [x.op for x in sentences.for_llm(chs)] == ["mod", "add"]


def test_change_rows_fit_schema_check():
    chs = sentences.changes(_sents(DOC_PREV), _sents("（１）為替変動リスク\n"), cur_doc="C", prev_doc="P")
    rows = [x.to_row("C", "P", 3) for x in chs]
    assert {r["op"] for r in rows} == {"del"}
    for r in rows:
        assert r["cur_evidence_id"] is None and r["prev_evidence_id"] and r["text_cur"] is None
        assert r["differ_ver"] == sentences.DIFFER_VER and r["pair_idx"] == 3 and r["is_minor"] == 0
        assert set(json.loads(r["offsets_json"])) == {"prev"}
    assert len({r["change_id"] for r in rows}) == len(rows)
    again = [x.to_row("C", "P", 3) for x in sentences.changes(_sents(DOC_PREV), _sents("（１）為替変動リスク\n"),
                                                              cur_doc="C", prev_doc="P")]
    assert [r["change_id"] for r in again] == [r["change_id"] for r in rows]   # deterministic


def test_change_rows_insert_into_sqlite():
    from edinet_risk.store import db
    conn = db.open_db(":memory:")
    for d in ("C", "P"):
        conn.execute("INSERT INTO documents (doc_id) VALUES (?)", (d,))
    p, c = _sents(DOC_PREV), _sents(DOC_CUR)
    for x in sentences.changes(p, c, cur_doc="C", prev_doc="P"):
        row = x.to_row("C", "P", 0)
        conn.execute(f"INSERT INTO sentence_changes ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
    got = conn.execute("SELECT op, is_minor FROM sentence_changes ORDER BY rowid").fetchall()
    assert [tuple(r) for r in got] == [("mod", 1), ("mod", 0), ("add", 0)]


# ---------------------------------------------------------------- chardiff

@pytest.mark.parametrize("prev,cur", [
    ("円高が進むと利益が減少します。", "円高が進むと営業利益が大きく減少します。"),
    ("", "全部新しい。"), ("全部消えた。", ""), ("同じ。", "同じ。"), ("abc", "xyz"),
])
def test_char_diff_rebuilds_both_sides(prev, cur):
    segs = chardiff.char_diff(prev, cur)
    assert chardiff.side_text(segs, "prev") == prev and chardiff.side_text(segs, "cur") == cur
    assert all(s.op in chardiff.OPS and s.text for s in segs)
    assert all(a.op != b.op for a, b in zip(segs, segs[1:]))   # adjacent same-op merged


def test_char_diff_ranges():
    assert chardiff.char_diff("利益が減少", "営業利益が大きく減少") == [
        Segment("cur_only", "営業"), Segment("equal", "利益が"), Segment("cur_only", "大きく"),
        Segment("equal", "減少")]
    assert chardiff.char_diff("円高", "円安") == [Segment("equal", "円"), Segment("prev_only", "高"),
                                               Segment("cur_only", "安")]


def test_blocks_and_markers():
    p, c = _sents(DOC_PREV), _sents(DOC_CUR)
    bs = chardiff.blocks(p, c)
    assert [(b.op, b.is_minor) for b in bs] == [("equal", False), ("mod", True), ("mod", False), ("add", False)]
    assert bs[0].segments == [Segment("equal", "（１）為替変動リスク")]
    assert bs[3].segments == [Segment("cur_only", "新たに地政学リスクが高まっています。")]
    for b in bs:   # each block rebuilds its own sentences
        assert chardiff.side_text(b.segments, "prev") == (b.prev.text if b.prev else "")
        assert chardiff.side_text(b.segments, "cur") == (b.cur.text if b.cur else "")
    text = chardiff.render_markers(bs)
    assert text.splitlines()[1] == "当社は202[-5-]{+6+}年3月期において為替の影響を受けました。"
    assert "{+営業+}" in text and text.endswith("{+新たに地政学リスクが高まっています。+}")


def test_blocks_reuse_rows():
    p, c = _sents(DOC_PREV), _sents(DOC_CUR)
    rows = sentences.align(p, c)
    assert [b.op for b in chardiff.blocks(p, c, rows)] == [r.op for r in rows]
    bs = chardiff.blocks([], _sents("新設。"))
    assert [(b.op, b.prev, b.segments) for b in bs] == [("add", None, [Segment("cur_only", "新設。")])]
