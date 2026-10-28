"""segment.headings / segment.segmenter on synthetic BusinessRisks texts (plan §4 M1, §8 segment)."""
import json

import pytest

from edinet_risk import m1_segment
from edinet_risk.segment.headings import classify, normalize_heading
from edinet_risk.segment.segmenter import segment


def tile_ok(text, seg):
    """Preamble + item spans reproduce the input exactly, in order, without gaps."""
    pos = len(seg.preamble)
    assert text[:pos] == seg.preamble
    for it in seg.items:
        assert it.start_off == pos
        assert text[it.start_off:it.end_off].lstrip(" 　\t").startswith(it.heading_raw)
        pos = it.end_off
    assert pos == len(text)


@pytest.mark.parametrize("line,family,number,title", [
    ("(1) 為替の変動", "paren_num", 1, "為替の変動"),
    ("（１２）　訴訟など法的手続き", "paren_num", 12, "訴訟など法的手続き"),
    ("　(19)　自然災害と関連する損害", "paren_num", 19, "自然災害と関連する損害"),
    ("①自動車市場の競争激化", "circled", 1, "自動車市場の競争激化"),
    ("⑳　感染症等の発生", "circled", 20, "感染症等の発生"),
    ("１．世界経済や景気の急激な変動", "num_dot", 1, "世界経済や景気の急激な変動"),
    ("2. 原材料", "num_dot", 2, "原材料"),
    ("(ア) 子会社", "paren_kana", 1, "子会社"),
    ("イ．海外", "kana_dot", 2, "海外"),
    ("市場及び事業に関するリスク", "plain", None, "市場及び事業に関するリスク"),
])
def test_classify_heading_patterns(line, family, number, title):
    c = classify(line + "\n")
    assert (c.family, c.number, c.title) == (family, number, title)


@pytest.mark.parametrize("line", [
    "", "　", "＜リスク＞", "＜対応策＞", "・ストライキ等の労働争議", "※1：CMzO（最高モノづくり責任者）",
    "当社グループは、世界各国で事業を行っています。",            # sentence
    "(1) 当社グループは為替の影響を受けます。",                  # numbered but a sentence
    "(2) " + "あ" * 90,                                           # numbered but too long
    "移行リスク\t規制\t燃費規制の強化",                          # table row
])
def test_classify_rejects_body_lines(line):
    assert classify(line) is None


@pytest.mark.parametrize("line", [
    "2018年以降は品質改革に取り組み",  # digits without a dot
    "1.5倍に増加",                      # decimal, not a list marker
    "（品質方針）",                     # parenthesised word, not a number
])
def test_classify_not_numbered(line):
    c = classify(line)
    assert c is None or not c.numbered  # at most a plain candidate; segmenter keeps it only before a numbered line


def test_normalize_heading():
    assert normalize_heading("為替 の　変動") == "為替の変動"
    assert normalize_heading("＜事業等のリスク＞") == "事業等のリスク"
    assert normalize_heading("ＣＡＳＥ") == "CASE"


TWO_LEVEL = """以下のリスクがあります。
なお、将来に関する事項は提出日現在の判断です。
（１）市場および事業に関するリスク
①自動車市場の競争激化
世界の自動車市場では激しい競争が繰り広げられています。
②自動車市場の需要変動
需要は変動します。
二段落目です。
（２）金融・経済のリスク
①為替および金利変動の影響
為替の影響を受けます。
"""


def test_two_level_numbered():
    seg = segment(TWO_LEVEL)
    assert seg.segmentation == "normal" and seg.families == ["paren_num", "circled"]
    assert seg.preamble.startswith("以下のリスク") and seg.preamble.endswith("判断です。\n")
    assert [(it.level, it.heading_norm, it.is_leaf) for it in seg.items] == [
        (1, "市場および事業に関するリスク", False), (2, "自動車市場の競争激化", True),
        (2, "自動車市場の需要変動", True), (1, "金融・経済のリスク", False), (2, "為替および金利変動の影響", True)]
    leaf = seg.items[2]
    assert leaf.body == "需要は変動します。\n二段落目です。"
    assert leaf.parents == ("市場および事業に関するリスク",)
    assert seg.items[4].number == 1 and seg.items[4].parents == ("金融・経済のリスク",)
    tile_ok(TWO_LEVEL, seg)


def test_plain_group_heading_and_restarted_numbering():
    text = ("当社グループのリスクは次のとおりです。\n"
            "市場及び事業に関するリスク\n(1) 経済情勢\n本文A。\n(2) 調達\n本文B。\n"
            "金融・経済に係るリスク\n(1) 為替\n本文C。\n")
    seg = segment(text)
    assert [(it.level, it.family, it.heading_norm) for it in seg.items] == [
        (1, "plain", "市場及び事業に関するリスク"), (2, "paren_num", "経済情勢"), (2, "paren_num", "調達"),
        (1, "plain", "金融・経済に係るリスク"), (2, "paren_num", "為替")]
    assert [it.heading_norm for it in seg.leaves] == ["経済情勢", "調達", "為替"]
    assert seg.leaves[2].parents == ("金融・経済に係るリスク",)
    tile_ok(text, seg)


def test_plain_line_followed_by_body_is_not_a_heading():
    # SUBARU: 「気候変動に関して認識している主な機会」 sits inside ⑯ and is followed by body text
    text = ("①気候変動\n＜リスク＞\n移行リスクがあります。\n気候変動に関して認識している主な機会\n"
            "機会もあります。\n＜対応策＞\n対応します。\n②感染症等の発生\n感染症のリスク。\n")
    seg = segment(text)
    assert [it.heading_norm for it in seg.items] == ["気候変動", "感染症等の発生"]
    assert "主な機会\n機会もあります。\n＜対応策＞\n対応します。" in seg.items[0].body
    tile_ok(text, seg)


def test_group_heading_between_levels():
    # SUBARU 2026: (4) > plain group > ①
    text = ("(1)　考え方\n本文。\n(2)　主要な事業等のリスク\n導入。\n経済・金融環境の変動に関連するリスク\n"
            "①主要市場の経済動向\n本文。\n業界および事業活動に関連するリスク\n②特定の事業への集中\n本文。\n")
    seg = segment(text)
    assert [(it.level, it.heading_norm, it.is_leaf) for it in seg.items] == [
        (1, "考え方", True), (1, "主要な事業等のリスク", False), (2, "経済・金融環境の変動に関連するリスク", False),
        (3, "主要市場の経済動向", True), (2, "業界および事業活動に関連するリスク", False), (3, "特定の事業への集中", True)]
    assert seg.items[5].parents == ("主要な事業等のリスク", "業界および事業活動に関連するリスク")
    assert seg.items[1].body == "導入。"
    tile_ok(text, seg)


def test_consecutive_same_family_lines_are_a_list_not_headings():
    # SUBARU 品質方針: 「１．…」「２．…」「３．…」 back to back inside ⑦
    text = ("①商品の責任\n（品質方針）\n私たちは品質を大切にします\n１．安心な商品をお届けします\n"
            "２．お客様の声に耳を傾けます\n\n３．法令を遵守します\nその後の本文。\n②サプライチェーン\n本文。\n")
    seg = segment(text)
    assert [it.heading_norm for it in seg.items] == ["商品の責任", "サプライチェーン"]
    assert "３．法令を遵守します" in seg.items[0].body
    tile_ok(text, seg)


def test_num_dot_over_paren_num_and_leaf_at_upper_level():
    # Nissan: １． > (1); 「２．」 has no children and is itself an item
    text = ("前文。\n１．世界経済\n(1) 経済状況\n本文。\n(2) 資源\n本文。\n２．自動車市場\n本文のみ。\n"
            "３．金融市場\n(1) 為替\n本文。\n")
    seg = segment(text)
    assert seg.families == ["num_dot", "paren_num"]
    assert [(it.level, it.heading_norm) for it in seg.leaves] == [
        (2, "経済状況"), (2, "資源"), (1, "自動車市場"), (2, "為替")]
    tile_ok(text, seg)


def test_fallback_when_no_heading():
    text = "当社は様々なリスクにさらされています。\n\n為替の変動は業績に影響します。\n原材料価格も影響します。\n"
    seg = segment(text)
    assert seg.segmentation == "fallback"
    assert [it.body for it in seg.items] == ["当社は様々なリスクにさらされています。", "為替の変動は業績に影響します。",
                                             "原材料価格も影響します。"]
    assert all(it.segmentation == "fallback" and it.heading_raw == "" for it in seg.items)
    tile_ok(text, seg)


def test_empty_text():
    seg = segment("")
    assert seg.segmentation == "empty" and seg.items == []


def test_crlf_and_offsets():
    text = "前文です。\r\n(1) Ａ\r\n本文Ａです。\r\n(2) Ｂ\r\n本文Ｂです。"
    seg = segment(text)
    assert [it.heading_norm for it in seg.items] == ["A", "B"]
    assert seg.items[0].body == "本文Ａです。" and seg.items[1].body == "本文Ｂです。"
    tile_ok(text, seg)


def test_m1_segment_report(tmp_path):
    m0 = tmp_path / "m0"
    m0.mkdir()
    targets = tmp_path / "t.toml"
    targets.write_text('[[filer]]\nlabel = "テスト社"\nedinetCode = "E00001"\n\n[[period]]\nlabel = "x"\n',
                       encoding="utf-8")
    for doc_id, pe, text in [("S1", "2026-03-31", TWO_LEVEL), ("S0", "2025-03-31", "見出しのない本文です。\n")]:
        (m0 / f"{doc_id}_business_risks.txt").write_text(text, encoding="utf-8")
        (m0 / f"{doc_id}_meta.json").write_text(json.dumps({"docID": doc_id, "edinetCode": "E00001",
                                                            "periodEnd": pe}), encoding="utf-8")
    out = tmp_path / "m1"
    rows = m1_segment.run(m0, targets, out)
    assert [r["docID"] for r in rows] == ["S0", "S1"]
    md = (out / "segment_summary.md").read_text(encoding="utf-8")
    assert "| テスト社 | 2026年3月期 | S1 |" in md and "| 3 | 5 |" in md
    assert "①自動車市場の競争激化 | ①為替および金利変動の影響 |" in md
    assert "fallback（段落単位）" in md
    dump = json.loads((out / "segments_transport7.json").read_text(encoding="utf-8"))
    assert dump[1]["items"][1]["heading_norm"] == "自動車市場の競争激化"


M0_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "out" / "m0"


@pytest.mark.skipif(not list(M0_DIR.glob("*_business_risks.txt")), reason="out/m0 texts not present")
def test_m0_texts_segment_without_fallback():
    for p in sorted(M0_DIR.glob("*_business_risks.txt")):
        text = p.read_text(encoding="utf-8")
        seg = segment(text)
        assert seg.segmentation == "normal", p.name
        assert len(seg.leaves) >= 10, p.name
        tile_ok(text, seg)
