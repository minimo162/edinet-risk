"""照合シート (plan §3 report/reconcile.py, §4 M1-1): items, first / last item name, full-text match.

One row per filer x period. ``全文一致`` checks that nothing is lost between the extracted text and the
report: (a) preamble + all items rebuild the extracted text exactly (segmenter is lossless), and (b) for
every pair, the text shown on each side of the diff equals the source items after NFKC / whitespace
normalisation (comparison only, §3-1), and every leaf item appears in exactly one pair.

Gold from a manual Excel is out of scope for M1 (not needed, per instruction): the ``手作業`` columns stay
empty and ``判定`` is ``対象外`` unless a gold dict (``tools/gold_import.py`` JSON) is passed.
"""
from __future__ import annotations

from ..diff.minor import norm_notation

HEADER = ("会社", "期", "docID", "版", "字数", "分割", "項目数", "先頭の項目名", "末尾の項目名", "全文一致",
          "手作業の項目数", "手作業の先頭", "手作業の末尾", "判定", "備考")
GOLD_MISSING = "対象外"


def _period(pe: str | None) -> str:
    return f"{pe[:4]}年{int(pe[5:7])}月期" if pe else "?"


def side_text(pair: dict, side: str) -> str:
    """Concatenated text of one side of a pair as shown in the diff (equal + that side's own segments)."""
    own = "prev_only" if side == "prev" else "cur_only"
    return "".join(b[f"gap_{side}"] + "".join(t for op, t in b["segments"] if op in ("equal", own))
                   for b in pair["blocks"])


def side_check(filer: dict, side: str) -> tuple[bool, list[str]]:
    notes = []
    sd = filer[side]
    if not sd["lossless"]:
        notes.append("項目分割で本文が欠落")
    key = "cur_items" if side == "cur" else "prev_items"
    seen = [i for p in filer["pairs"] for i in p[key]]
    leaves = [it["idx"] for it in sd["items"]]
    if sorted(seen) != sorted(leaves):
        notes.append("対応表に出ない／重複する項目あり")
    for p in filer["pairs"]:
        if norm_notation(side_text(p, side)) != norm_notation(p[f"{side}_src"]):
            notes.append(f"ペア{p['pair_idx']}の差分表示が原文と不一致")
    return not notes, notes


def rows(data: dict, gold: dict | None = None) -> list[tuple]:
    """Rows of the 照合 sheet (``HEADER`` order). ``gold``: {docID: {n_items, first, last}}."""
    out = []
    for f in data["filers"]:
        for side, kind in (("prev", f["prev_kind"]), ("cur", f["cur_kind"])):
            sd = f[side]
            doc = f[f"{side}_doc"]
            if sd is None:
                out.append((f["label"], "当期" if side == "cur" else "前期", doc or "-", kind or "-", None, "-", None, "-", "-", "-",
                            None, None, None, "-", f["skip"] or ""))
                continue
            items = sd["items"]
            first = items[0]["heading_raw"] if items else ""
            last = items[-1]["heading_raw"] if items else ""
            ok, notes = side_check(f, side)
            g = (gold or {}).get(doc)
            if g is None:
                gn = gf = gl = None
                verdict = GOLD_MISSING
            else:
                gn, gf, gl = g.get("n_items"), g.get("first"), g.get("last")
                hit = (gn == len(items), norm_notation(gf or "") == norm_notation(first),
                       norm_notation(gl or "") == norm_notation(last))
                verdict = "一致" if all(hit) else "不一致(" + ",".join(
                    n for n, h in zip(("項目数", "先頭", "末尾"), hit) if not h) + ")"
            out.append((f["label"], _period(sd["period_end"]), doc, kind or "-", sd["chars"], sd["segmentation"],
                        len(items), first, last, "○" if ok else "×", gn, gf, gl, verdict, "；".join(notes)))
    return out


def summary(rs: list[tuple]) -> dict:
    full = [r for r in rs if r[9] in ("○", "×")]
    return {"rows": len(rs), "fulltext_ok": sum(r[9] == "○" for r in full), "fulltext_rows": len(full),
            "gold_ok": sum(r[13] == "一致" for r in rs), "gold_missing": sum(r[13] == GOLD_MISSING for r in rs)}
