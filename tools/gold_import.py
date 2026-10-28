"""手作業の Excel → 正解 JSON（plan §3 tools/gold_import.py, §4 M1, §8-2）. Interface + stub only.

The manual Excel (7 filers x 2 periods) has not been received yet, so the converter cannot know its
layout. This module fixes the interface and fails loudly instead of producing an empty gold set:

    python tools/gold_import.py --excel <手作業.xlsx> --out tests/golden/seven/gold.json

Gold JSON (consumed by ``edinet_risk.m1_report.load_gold`` / ``report.reconcile``)::

    {"gold_ver": "gold-1", "source": "<file name>", "sha256": "<of the Excel>",
     "docs":  {"<docID>": {"n_items": int, "first": str, "last": str,
                           "items": [{"heading": str, "start_norm": int}]}},     # boundary F1
     "pairs": {"<cur docID>|<prev docID>": [{"cur": [heading...], "prev": [heading...],
                                              "label": "same|modified|...", "changed": bool}]}}

Exit codes: 0 ok, 2 Excel not received / not found, 3 layout not implemented yet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

GOLD_VER = "gold-1"


class GoldNotReceived(FileNotFoundError):
    """The manual Excel is not available (not delivered yet, or the path is wrong)."""


class GoldLayoutUnknown(NotImplementedError):
    """The Excel exists, but its sheet layout has not been mapped yet (needs the real file)."""


def convert(excel_path: Path | None) -> dict:
    """Manual Excel -> gold dict (schema above). Stub until the Excel is received."""
    if excel_path is None or not Path(excel_path).exists():
        raise GoldNotReceived(
            f"手作業の Excel が見つかりません（{excel_path or '未指定'}）。手作業の7社分の Excel は未受領です"
            "（計画 §4 M1 の前提）。受領後に --excel でパスを指定してください。正解 JSON は作成していません。")
    raise GoldLayoutUnknown(
        f"{Path(excel_path).name}: 手作業 Excel のシート構成への対応は未実装です（受領後に実ファイルを見て"
        "列の対応を実装する）。正解 JSON は作成していません。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tools/gold_import.py", description=__doc__.splitlines()[0])
    ap.add_argument("--excel", type=Path, default=None, help="手作業の Excel（任意。M1 では不要）")
    ap.add_argument("--out", type=Path, default=Path("tests/golden/seven/gold.json"))
    a = ap.parse_args(argv)
    try:
        convert(a.excel)
    except GoldNotReceived as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except GoldLayoutUnknown as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
