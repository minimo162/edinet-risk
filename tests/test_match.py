"""match.ngram_tfidf / hungarian / lis / matcher on small synthetic inputs (plan §6-6, §7, §8-1 match).

The hand-written manual Excel (gold) has not been received yet, so matcher cases are synthetic pairs with
known answers. scipy (dev only, §7) is the Hungarian oracle when installed; a brute-force oracle always runs.
"""
import itertools
import json
import math
import random
from pathlib import Path

import pytest

from edinet_risk import m1_match
from edinet_risk.config import Thresholds, load_thresholds
from edinet_risk.match.hungarian import assignment_value, linear_sum_assignment
from edinet_risk.match.lis import lis_indices, not_in_lis
from edinet_risk.match.matcher import LABELS, MATCHER_VER, MItem, containment, match
from edinet_risk.match.ngram_tfidf import Tfidf, cosine, ngrams, normalize, similarity_matrix
from edinet_risk.store import db

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- ngram_tfidf
def test_normalize_is_nfkc_without_whitespace():
    assert normalize("（１）　為替 の\n変動ＡＢ") == "(1)為替の変動AB"


def test_ngrams_counts_2_to_3():
    assert ngrams("ａｂａｂ", 2, 3) == {"ab": 2, "ba": 1, "aba": 1, "bab": 1}
    assert ngrams("あ", 2, 3) == {"あ": 1}       # shorter than nmin -> whole string
    assert ngrams("", 2, 3) == {}


def test_tfidf_hand_computed():
    m = Tfidf(["aab", "abc"], 1, 1)              # N=2, df a=2 b=2 c=1
    assert m.idf("a") == pytest.approx(1.0)
    assert m.idf("c") == pytest.approx(math.log(3 / 2) + 1)
    assert m.idf("z") == pytest.approx(math.log(3) + 1)  # unseen
    ic = math.log(1.5) + 1
    expected = (2 * 1 + 1 * 1) / (math.sqrt(5) * math.sqrt(2 + ic * ic))
    assert m.similarity("aab", "abc") == pytest.approx(expected)
    v = m.vector("aab")
    assert sum(w * w for w in v.values()) == pytest.approx(1.0)


def test_cosine_bounds_and_matrix():
    m = Tfidf(["為替の変動", "金利の変動", "自然災害"])
    assert m.similarity("為替の変動", "為替 の 変動") == pytest.approx(1.0)
    assert m.similarity("為替の変動", "自然災害") == 0.0
    assert m.similarity("", "為替") == 0.0
    assert cosine({}, {"a": 1.0}) == 0.0
    mat = similarity_matrix(["為替の変動", "自然災害"], ["自然災害", "為替変動", "金利"])
    assert len(mat) == 2 and len(mat[0]) == 3
    assert mat[1][0] == pytest.approx(1.0)
    assert mat[0][1] > mat[0][2] == 0.0


# ---------------------------------------------------------------- hungarian
def brute_force(cost, maximize=False):
    n, m = len(cost), len(cost[0])
    best = None
    if n <= m:
        for cols in itertools.permutations(range(m), n):
            v = sum(cost[i][c] for i, c in enumerate(cols))
            best = v if best is None or (v > best if maximize else v < best) else best
    else:
        for rows in itertools.permutations(range(n), m):
            v = sum(cost[r][j] for j, r in enumerate(rows))
            best = v if best is None or (v > best if maximize else v < best) else best
    return best


def check_valid(cost, rows, cols):
    n, m = len(cost), len(cost[0])
    assert len(rows) == len(cols) == min(n, m)
    assert rows == sorted(rows) and len(set(rows)) == len(rows) and len(set(cols)) == len(cols)
    assert all(0 <= r < n for r in rows) and all(0 <= c < m for c in cols)


def test_hungarian_known_matrix():
    cost = [[4, 1, 3], [2, 0, 5], [3, 2, 2]]     # scipy docs example: optimum 5
    rows, cols = linear_sum_assignment(cost)
    assert (rows, cols) == ([0, 1, 2], [1, 0, 2])
    assert assignment_value(cost, rows, cols) == 5


def test_hungarian_rectangular_and_maximize():
    cost = [[0.9, 0.1, 0.2, 0.0], [0.1, 0.8, 0.0, 0.3]]
    rows, cols = linear_sum_assignment(cost, maximize=True)
    assert (rows, cols) == ([0, 1], [0, 1])
    t = [list(c) for c in zip(*cost)]            # 4 x 2
    rows, cols = linear_sum_assignment(t, maximize=True)
    assert (rows, cols) == ([0, 1], [0, 1])


def test_hungarian_degenerate_inputs():
    assert linear_sum_assignment([]) == ([], [])
    assert linear_sum_assignment([[]]) == ([], [])
    assert linear_sum_assignment([[7.5]]) == ([0], [0])
    with pytest.raises(ValueError):
        linear_sum_assignment([[1, 2], [3]])
    with pytest.raises(ValueError):
        linear_sum_assignment([[1, math.inf]])
    with pytest.raises(ValueError):
        linear_sum_assignment([[math.nan]])


def test_hungarian_matches_brute_force_small():
    rng = random.Random(20261028)
    for _ in range(400):
        n, m = rng.randint(1, 6), rng.randint(1, 6)
        ints = rng.random() < 0.3                # many ties
        cost = [[rng.randint(0, 3) if ints else rng.random() for _ in range(m)] for _ in range(n)]
        maximize = rng.random() < 0.5
        rows, cols = linear_sum_assignment(cost, maximize=maximize)
        check_valid(cost, rows, cols)
        assert assignment_value(cost, rows, cols) == pytest.approx(brute_force(cost, maximize))


def test_hungarian_matches_scipy_oracle():
    so = pytest.importorskip("scipy.optimize")
    rng = random.Random(7)
    for k in range(1500):
        n = rng.randint(1, 60) if k % 10 == 0 else rng.randint(1, 20)
        m = max(1, n + rng.randint(-5, 5))
        cost = [[rng.random() for _ in range(m)] for _ in range(n)]
        maximize = k % 2 == 0
        rows, cols = linear_sum_assignment(cost, maximize=maximize)
        check_valid(cost, rows, cols)
        r2, c2 = so.linear_sum_assignment(cost, maximize=maximize)
        assert assignment_value(cost, rows, cols) == pytest.approx(
            float(sum(cost[r][c] for r, c in zip(r2, c2))), abs=1e-9)


# ---------------------------------------------------------------- lis
def lis_len_dp(seq):
    best = [1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] < seq[i]:
                best[i] = max(best[i], best[j] + 1)
    return max(best, default=0)


@pytest.mark.parametrize("seq,expected", [
    ([], []), ([5], [0]), ([1, 2, 3], [0, 1, 2]), ([3, 2, 1], [2]),
    ([0, 8, 4, 12, 2, 10, 6, 14], [0, 4, 6, 7]),
    ([0, 3, 1, 2, 4], [0, 2, 3, 4]),             # item at position 1 moved forward
])
def test_lis_examples(seq, expected):
    got = lis_indices(seq)
    assert len(got) == len(expected) == lis_len_dp(seq)
    assert got == expected


def test_lis_property_random():
    rng = random.Random(3)
    for _ in range(300):
        seq = [rng.randint(0, 15) for _ in range(rng.randint(0, 25))]
        idx = lis_indices(seq)
        assert idx == sorted(idx)
        assert all(seq[a] < seq[b] for a, b in zip(idx, idx[1:]))
        assert len(idx) == lis_len_dp(seq)
        assert not_in_lis(seq) == set(range(len(seq))) - set(idx)


# ---------------------------------------------------------------- matcher (synthetic, known answers)
BODIES = {
    "fx": "当社グループは海外売上高の比率が高く、為替相場の変動は円換算後の売上高と利益に影響を及ぼします。"
          "為替予約等によりリスクの低減に努めていますが、急激な変動は業績に影響を与える可能性があります。",
    "raw": "鋼材、アルミ、樹脂、貴金属などの原材料価格が高騰した場合、製造原価が上昇します。"
           "調達先の分散と長期契約により安定調達に努めていますが、価格転嫁が遅れると利益が圧迫されます。",
    "cyber": "サイバー攻撃やシステム障害により生産・販売活動が停止する可能性があります。"
             "情報セキュリティ委員会を設置し、監視体制の強化と従業員教育を継続して実施しています。",
    "quake": "大規模な地震、台風、洪水などの自然災害が発生した場合、生産拠点や仕入先が被災し操業が停止するおそれがあります。"
             "事業継続計画を策定し、定期的な訓練を行っています。",
    "law": "各国の排出ガス規制、安全基準、通商政策の変更に対応するための費用が増加する可能性があります。"
           "法令順守体制を整備し、規制動向を継続的に監視しています。",
    "talent": "優秀な人材の確保と育成ができない場合、技術開発や事業運営に支障をきたすおそれがあります。"
              "多様な人材の採用と研修制度の充実に取り組んでいます。",
}


def it(idx, head, key_or_body, number=None):
    body = BODIES.get(key_or_body, key_or_body)
    return MItem(idx, f"({number or idx + 1}) {head}", normalize(head), body, number or idx + 1)


def base():
    return [it(0, "為替の変動", "fx"), it(1, "原材料価格", "raw"), it(2, "情報セキュリティ", "cyber"),
            it(3, "自然災害", "quake"), it(4, "法規制", "law")]


def labels(res):
    return [(p.label, p.prev_items, p.cur_items) for p in res.pairs]


def covers_each_item_once(res, n_cur, n_prev):
    cur = sorted(i for p in res.pairs for i in p.cur_items)
    prev = sorted(i for p in res.pairs for i in p.prev_items)
    return cur == sorted(set(cur)) and prev == sorted(set(prev)) and len(cur) == n_cur and len(prev) == n_prev


def test_identical_lists_all_same_by_anchor():
    res = match(base(), base())
    assert [p.label for p in res.pairs] == ["same"] * 5
    assert {p.stage for p in res.pairs} == {"anchor"}
    assert all(p.score_s == pytest.approx(1.0) and not p.review_flag and not p.renumbered for p in res.pairs)
    assert res.matcher_ver == MATCHER_VER and res.thresholds_version == Thresholds().version


def test_modified_body_keeps_anchor():
    cur = base()
    cur[1] = it(1, "原材料価格", BODIES["raw"].replace("長期契約", "複数年契約と在庫の積み増し"))
    p = match(cur, base()).pairs[1]
    assert (p.label, p.stage, p.prev_items, p.cur_items) == ("modified", "anchor", [1], [1])
    assert 0.3 <= p.body_sim < 1.0


def test_whitespace_only_change_is_same():
    cur = base()
    cur[0] = it(0, "為替 の 変動", BODIES["fx"].replace("。", "。\n"))
    assert match(cur, base()).pairs[0].label == "same"


def test_retitled_via_assignment():
    cur = base()
    cur[2] = it(2, "サイバーセキュリティ", "cyber")
    p = match(cur, base()).pairs[2]
    assert (p.label, p.stage, p.prev_items, p.cur_items) == ("retitled", "assign", [2], [2])
    assert p.score_s >= 0.55 and not p.review_flag


def test_moved_is_the_item_outside_the_lis():
    prev = base()
    cur = [it(0, "為替の変動", "fx", 1), it(1, "自然災害", "quake", 2), it(2, "原材料価格", "raw", 3),
           it(3, "情報セキュリティ", "cyber", 4), it(4, "法規制", "law", 5)]
    res = match(cur, prev)
    got = {(p.label, p.prev_items[0], p.cur_items[0]) for p in res.pairs}
    assert ("moved", 3, 1) in got
    assert sum(p.label == "moved" for p in res.pairs) == 1
    renum = {(p.prev_items[0], p.cur_items[0]) for p in res.pairs if p.renumbered}
    assert renum == {(3, 1), (1, 2), (2, 3)}     # numbering shifted, recorded separately from the label


def test_added_and_deleted():
    prev = base()
    cur = base()[:4] + [it(4, "人材の確保", "talent")]
    res = match(cur, prev)
    assert ("added", [], [4]) in labels(res)
    assert ("deleted", [4], []) in labels(res)
    dl = next(p for p in res.pairs if p.label == "deleted")
    assert dl.stage is None and dl.score_s is None
    assert covers_each_item_once(res, 5, 5)


def test_deleted_is_listed_after_its_predecessor():
    prev = base()
    cur = [x for x in base() if x.idx != 2]
    cur = [MItem(k, x.heading_raw, x.heading_norm, x.body, x.number) for k, x in enumerate(cur)]
    res = match(cur, prev)
    assert [p.label for p in res.pairs] == ["same", "same", "deleted", "same", "same"]
    assert res.pairs[2].prev_items == [2]


def test_split_one_prev_into_two_cur():
    a, b = BODIES["fx"], BODIES["raw"]
    prev = [it(0, "市場リスク", a + b), it(1, "自然災害", "quake")]
    cur = [it(0, "為替の変動", a), it(1, "原材料価格", b), it(2, "自然災害", "quake")]
    res = match(cur, prev)
    sp = res.pairs[0]
    assert (sp.label, sp.stage, sp.prev_items, sp.cur_items) == ("split", "contain", [0], [0, 1])
    assert res.pairs[1].label == "same"
    assert covers_each_item_once(res, 3, 2)


def test_merge_two_prev_into_one_cur():
    a, b = BODIES["fx"], BODIES["raw"]
    prev = [it(0, "為替の変動", a), it(1, "原材料価格", b), it(2, "自然災害", "quake")]
    cur = [it(0, "市場リスク", a + "\n" + b), it(1, "自然災害", "quake")]
    res = match(cur, prev)
    mg = res.pairs[0]
    assert (mg.label, mg.stage, mg.prev_items, mg.cur_items) == ("merged", "contain", [0, 1], [0])
    assert covers_each_item_once(res, 2, 3)


def test_unrelated_items_stay_unmatched():
    prev = [it(0, "為替の変動", "fx")]
    cur = [it(0, "人材の確保", "talent")]
    assert sorted(p.label for p in match(cur, prev).pairs) == ["added", "deleted"]


def test_review_band_from_thresholds():
    cur = base()
    cur[2] = it(2, "サイバーセキュリティ", "cyber")
    strict = Thresholds(accept_min=0.99, review_min=0.2)
    p = match(cur, base(), strict).pairs[2]
    assert p.stage == "assign" and p.review_flag
    # rejected by the assignment (S < review_min) but rescued 1:1 by containment, flagged for review
    p = match(cur, base(), Thresholds(accept_min=0.99, review_min=0.99)).pairs[2]
    assert (p.label, p.stage, p.review_flag) == ("retitled", "contain", True)


def test_duplicate_headings_are_not_anchors_but_assigned_by_body():
    prev = [it(0, "市場環境の影響", "fx"), it(1, "市場環境の影響", "raw")]
    cur = [it(0, "市場環境の影響", "raw"), it(1, "市場環境の影響", "fx")]
    res = match(cur, prev)
    pairs = {(p.prev_items[0], p.cur_items[0]): p for p in res.pairs}
    assert set(pairs) == {(0, 1), (1, 0)}
    assert {p.stage for p in res.pairs} == {"assign"}
    assert sorted(p.label for p in res.pairs) == ["moved", "same"]


def test_low_body_sim_blocks_anchor():
    prev = [it(0, "為替の変動", "fx")]
    cur = [it(0, "為替の変動", "当社は円建て取引を拡大し、通貨の影響はほぼなくなりました。")]
    p = match(cur, prev).pairs
    assert all(x.stage != "anchor" for x in p)


def test_fallback_items_without_headings_use_body_only():
    prev = [MItem(0, "", "", BODIES["fx"]), MItem(1, "", "", BODIES["raw"])]
    cur = [MItem(0, "", "", BODIES["raw"]), MItem(1, "", "", BODIES["fx"])]
    res = match(cur, prev)
    assert {p.stage for p in res.pairs} == {"assign"}
    assert all(p.score_s == pytest.approx(p.body_sim) for p in res.pairs)


def test_containment():
    a, b = BODIES["fx"], BODIES["raw"]
    assert containment(a, a + b) == pytest.approx(1.0)
    assert containment(a + b, a) == pytest.approx(len(normalize(a)) / len(normalize(a + b)))
    assert containment(a, b) < 0.2              # short stock phrases do not count
    assert containment("", a) == 0.0


def test_input_hash_and_determinism():
    r1, r2 = match(base(), base()), match(base(), base())
    assert r1.input_sha256 == r2.input_sha256 and labels(r1) == labels(r2)
    assert match(base(), base(), Thresholds(accept_min=0.6)).input_sha256 != r1.input_sha256


def test_accepts_dicts_and_segmenter_items():
    from edinet_risk.segment.segmenter import segment
    text = "(1) 為替の変動\n" + BODIES["fx"] + "\n(2) 自然災害\n" + BODIES["quake"] + "\n"
    leaves = segment(text).leaves
    res = match(leaves, [vars(x) for x in leaves], load_thresholds(ROOT / "config" / "thresholds.toml"))
    assert [p.label for p in res.pairs] == ["same", "same"]
    assert res.thresholds_version == "m1-initial"


def test_random_inputs_cover_every_item_once():
    rng = random.Random(11)
    keys = list(BODIES)
    for _ in range(40):
        prev = [it(k, f"見出し{rng.randint(0, 6)}", rng.choice(keys)) for k in range(rng.randint(0, 6))]
        cur = [it(k, f"見出し{rng.randint(0, 6)}", rng.choice(keys)) for k in range(rng.randint(0, 6))]
        res = match(cur, prev)
        assert covers_each_item_once(res, len(cur), len(prev))
        assert all(p.label in LABELS for p in res.pairs)


# ---------------------------------------------------------------- m1_match runner (synthetic files, offline)
def write_inputs(tmp_path, prev_items, cur_items):
    def seg(doc, items):
        return {"label": "テスト", "docID": doc, "periodEnd": None,
                "items": [vars(x) | {"is_leaf": True} for x in items]}
    (tmp_path / "segments.json").write_text(json.dumps([seg("SCUR", cur_items), seg("SPREV", prev_items)],
                                                       ensure_ascii=False), encoding="utf-8")
    (tmp_path / "resolve.json").write_text(json.dumps({"rows": [
        {"label": "テスト", "cur_doc_id": "SCUR", "prev_doc_id": "SPREV", "prev_kind": "original"},
        {"label": "前期なし", "cur_doc_id": "SX", "prev_doc_id": None, "no_prev_reason": "new_listing"},
    ]}, ensure_ascii=False), encoding="utf-8")


def test_m1_match_run_writes_outputs_and_db(tmp_path):
    cur = base()[:4] + [it(4, "人材の確保", "talent")]
    write_inputs(tmp_path, base(), cur)
    dbp = tmp_path / "t.sqlite"
    conn = db.open_db(dbp)
    for d in ("SCUR", "SPREV"):
        db.upsert(conn, "documents", {"doc_id": d}, key=["doc_id"])
    conn.close()
    res = m1_match.run(tmp_path / "resolve.json", tmp_path / "segments.json", tmp_path / "cache",
                       ROOT / "config" / "thresholds.toml", tmp_path / "out", dbp)
    assert res["saved_rows"] == 6
    md = (tmp_path / "out" / "match_summary.md").read_text(encoding="utf-8")
    assert "| テスト | SCUR | SPREV | 5 | 5 | 4 | 0 | 0 | 0 | 0 | 0 | 1 | 1 |" in md
    assert "new_listing" in md
    dump = json.loads((tmp_path / "out" / "matches_transport7.json").read_text(encoding="utf-8"))
    assert dump[0]["thresholds_version"] == "m1-initial" and len(dump[0]["pairs"]) == 6
    assert dump[1]["skip"].startswith("no pair")
    conn = db.connect(dbp)
    rows = conn.execute("SELECT label, stage, cur_items, prev_items FROM matches ORDER BY pair_idx").fetchall()
    conn.close()
    assert [r["label"] for r in rows] == ["same"] * 4 + ["deleted", "added"]
    assert json.loads(rows[5]["cur_items"]) == [4] and rows[5]["stage"] is None
    # re-run replaces rows instead of duplicating them
    assert m1_match.run(tmp_path / "resolve.json", tmp_path / "segments.json", tmp_path / "cache",
                        ROOT / "config" / "thresholds.toml", tmp_path / "out", dbp)["saved_rows"] == 6
    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 6
    conn.close()


def test_m1_match_missing_text_is_reported_offline(tmp_path):
    write_inputs(tmp_path, base(), base())
    r = json.loads((tmp_path / "resolve.json").read_text(encoding="utf-8"))
    r["rows"][0]["prev_doc_id"] = "SNOTCACHED"
    (tmp_path / "resolve.json").write_text(json.dumps(r), encoding="utf-8")
    res = m1_match.run(tmp_path / "resolve.json", tmp_path / "segments.json", tmp_path / "cache",
                       ROOT / "config" / "thresholds.toml", tmp_path / "out")
    assert res["results"][0]["result"] is None
    assert "not in cache (offline)" in res["results"][0]["skip"]


# ---------------------------------------------------------------- smoke on the 7-filer M1 outputs (if present)
@pytest.mark.skipif(not (ROOT / "out/m1/matches_transport7.json").exists(), reason="run m1_match first")
def test_transport7_matches_cover_all_leaves():
    segs = {d["docID"]: d for d in json.loads((ROOT / "out/m1/segments_transport7.json").read_text("utf-8"))}
    for rec in json.loads((ROOT / "out/m1/matches_transport7.json").read_text("utf-8")):
        assert rec["skip"] is None, rec
        cur = sorted(i for p in rec["pairs"] for i in p["cur_items"])
        n_cur = sum(it["is_leaf"] for it in segs[rec["cur_doc"]]["items"])
        assert cur == sorted(set(cur)) and len(cur) == n_cur
        assert {p["label"] for p in rec["pairs"]} <= set(LABELS)
