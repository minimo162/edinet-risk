from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from .client import EdinetError
from .net.http import NetworkForbidden, forbid_network


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m edinet_risk")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--offline", action="store_true",
                    help="forbid network access (any attempt is an error, exit code 3)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("m0_spike", help="M0: fetch one filer's 有報 and extract BusinessRisks")
    s.add_argument("--filer", required=True, help="filerName substring, e.g. マツダ")
    s.add_argument("--years", type=int, nargs="+", default=[2026, 2025], help="filing years to scan (late June)")
    s.add_argument("--out", type=Path, default=Path("out/m0"))
    s.add_argument("--cache", type=Path, default=Path("cache"))
    s.add_argument("--filter", type=Path, default=Path("config/filing_filter.toml"))
    t = sub.add_parser("m0_truncation", help="M0 (H1): compare CSV vs XBRL TextBlocks to find the 30,000-char cut")
    t.add_argument("doc_ids", nargs="+", help="docIDs (filer metadata is taken from the cached doclists)")
    t.add_argument("--out", type=Path, default=Path("out/m0"))
    t.add_argument("--cache", type=Path, default=Path("cache"))
    mm = sub.add_parser("m0_multi", help="M0 step 2: BusinessRisks for several filers x periods (by edinetCode)")
    mm.add_argument("--targets", type=Path, default=Path("config/transport7.toml"))
    mm.add_argument("--out", type=Path, default=Path("out/m0"))
    mm.add_argument("--cache", type=Path, default=Path("cache"))
    mm.add_argument("--filter", type=Path, default=Path("config/filing_filter.toml"))
    sc = sub.add_parser("m0_scan", help="M0-8: full-window doclist scan + formCode crosstab vs official table")
    sc.add_argument("--start", default="2025-06-01")
    sc.add_argument("--end", default="2026-06-30")
    sc.add_argument("--table", type=Path, default=Path("ref/ese140327/ESE140327.xlsx"))
    sc.add_argument("--no-fetch", action="store_true", help="use cached day lists only")
    sc.add_argument("--out", type=Path, default=Path("out/m0"))
    sc.add_argument("--cache", type=Path, default=Path("cache"))
    idb = sub.add_parser("init-db", help="create / migrate the SQLite schema (plan §5)")
    idb.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    idb.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    co = sub.add_parser("codes", help="EDINET code list + code aggregation list (plan §3-2)")
    cos = co.add_subparsers(dest="codes_cmd", required=True)
    cs = cos.add_parser("sync", help="fetch (or reuse cache/codes/) Edinetcode.zip + ESE140190.csv and upsert them")
    cs.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    cs.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    cs.add_argument("--cache", type=Path, default=None, help="cache root; files go to <cache>/codes/ "
                                                               "(default: paths.cache_dir)")
    cs.add_argument("--refresh", action="store_true", help="download again even if cached")
    cs.add_argument("--from-file", type=Path, default=None, help="import a local Edinetcode.zip (cache untouched)")
    cs.add_argument("--aggregation-file", type=Path, default=None,
                    help="import a local ESE140190.csv (cache untouched)")
    dl = sub.add_parser("doclist", help="書類一覧 (documents.json type=2) -> doclist_raw / documents (plan §3-2, §6-1)")
    dls = dl.add_subparsers(dest="doclist_cmd", required=True)
    ds = dls.add_parser("sync", help="fetch missing / not-final days (cache first), append doclist_raw, merge documents")
    rng = ds.add_mutually_exclusive_group(required=True)
    rng.add_argument("--from", dest="date_from", type=_iso_date, help="first file date YYYY-MM-DD (with --to)")
    rng.add_argument("--daily", action="store_true", help="previous day + current day (JST)")
    rng.add_argument("--rescan-month", action="store_true",
                     help="fetch every submit date of the year window(s) again (monthly re-scan)")
    rng.add_argument("--from-cache", action="store_true",
                     help="import every cached day file (<cache>/doclist/*.json); never calls the API")
    ds.add_argument("--to", dest="date_to", type=_iso_date, help="last file date (default: --from)")
    ds.add_argument("--window", choices=["current", "previous", "both"], default="current",
                    help="year window(s) for --rescan-month (settings [window.*].submit_from/submit_to)")
    ds.add_argument("--refresh", action="store_true", help="fetch every day of the range again, even if final")
    ds.add_argument("--dry-run", action="store_true", help="print the number of API calls only (no network, no DB)")
    ds.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                    help="cache only: import cached days, report the days that would need a call")
    ds.add_argument("--today", type=_iso_date, default=None, help=argparse.SUPPRESS)  # tests: JST 'today'
    ds.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    ds.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    ds.add_argument("--cache", type=Path, default=None, help="cache root (default: paths.cache_dir)")
    po = sub.add_parser("population", help="JPX prime population x EDINET codes (plan §3-2, M2)")
    pos = po.add_subparsers(dest="population_cmd", required=True)
    pl = pos.add_parser("load", help="load JPX 東証上場銘柄一覧 (data_j.xlsx), keep プライム（内国株式）, join edinet_codes")
    pl.add_argument("--jpx", type=Path, required=True, help="JPX list .xlsx (e.g. ref/jpx/data_j_202609.xlsx)")
    pl.add_argument("--as-of-month", required=True, type=_as_of_month,
                    help="base month YYYY-MM; must match the list's 日付 (rows of that month are replaced)")
    pl.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    pl.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    rv = sub.add_parser("resolve", help="adopted version + previous period + 突合/前期なし reasons (plan §3-2, §6-5; "
                                        "offline)")
    rv.add_argument("--fy", default=None, help="fiscal year label YYYY-MM (default: settings run.fy; another value "
                                               "derives the periodEnd window from it)")
    rv.add_argument("--as-of", default=None, type=_iso_date, help="base date T YYYY-MM-DD (default: settings run.as_of)")
    sc_ = rv.add_mutually_exclusive_group()
    sc_.add_argument("--entity", nargs="+", default=None, metavar="CODE", help="EDINET codes instead of the population")
    sc_.add_argument("--targets", type=Path, default=None, help="targets TOML ([[filer]] edinetCode), e.g. transport7")
    sc_.add_argument("--transport7", action="store_true", help="= --targets config/transport7.toml")
    rv.add_argument("--as-of-month", default=None, help="population base month (default: the latest loaded)")
    rv.add_argument("--successor-map", type=Path, default=Path("config/successor_map.csv"),
                    help="loaded into successor_map before resolving (missing file: table left as is)")
    rv.add_argument("--filter", type=Path, default=Path("config/filing_filter.toml"))
    rv.add_argument("--cache", type=Path, default=Path("cache"), help="cached doclist days / type=1 ZIPs")
    rv.add_argument("--config", type=Path, default=None, help="settings TOML (windows, [resolve], paths.db)")
    rv.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    rv.add_argument("--out", type=Path, default=Path("out/m2"), help="writes <out>/resolve_<run-id>/")
    rv.add_argument("--run-id", default=None)
    fe = sub.add_parser("fetch", help="type=1 bodies of the adopted / previous versions of a resolve run + retry "
                                      "queue (plan §3-2, §6-2〜6-4)")
    fsc = fe.add_mutually_exclusive_group()
    fsc.add_argument("--entity", nargs="+", default=None, metavar="CODE", help="EDINET codes (mapped to entity_id)")
    fsc.add_argument("--targets", type=Path, default=None, help="targets TOML ([[filer]] edinetCode), e.g. transport7")
    fsc.add_argument("--transport7", action="store_true", help="= --targets config/transport7.toml")
    fsc.add_argument("--all", action="store_true",
                     help="every entity of the resolve run (required to call the API without a narrower scope)")
    fe.add_argument("--run-id", default=None, help="resolve run whose filing_selection is used (default: latest ok)")
    fe.add_argument("--retry-failed", action="store_true", help="also request bodies with fetches.status=failed")
    fe.add_argument("--include-pending", action="store_true",
                    help="also the other versions of corrected filings (reason_basis_json.texts_pending), "
                         "needed by resolve for correction_partial / risk_changed_by_correction")
    fe.add_argument("--max-attempts", type=int, default=None,
                    help="transient failures stay in the retry queue up to this many attempts "
                         "(default: api.fetch_max_attempts)")
    fe.add_argument("--dry-run", action="store_true", help="print the plan and the API call count only "
                                                           "(no network, no DB writes)")
    fe.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                    help="no network: record cached bodies, keep the rest queued as pending")
    fe.add_argument("--cache", type=Path, default=None, help="cache root; bodies in <cache>/docs/ "
                                                             "(default: paths.cache_dir)")
    fe.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    fe.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    ex = sub.add_parser("extract", help="BusinessRisks text of every fetched type=1 body -> extracts (M2; offline)")
    ex.add_argument("--entity", nargs="+", default=None, metavar="CODE",
                    help="only the docs of these EDINET codes (documents.edinet_code)")
    ex.add_argument("--force", action="store_true", help="re-extract even if the ZIP hash is unchanged")
    ex.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    ex.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    mo = sub.add_parser("monitor", help="§8-3 monitoring metrics of a resolve run -> Excel + CSV (plan §3-2, M2-8; "
                                        "offline, never calls the API)")
    mo.add_argument("--run", default=None, metavar="RUN_ID",
                    help="resolve run whose filing_selection is monitored (default: latest ok)")
    mo.add_argument("--fail-on-threshold", action="store_true", help="exit code 2 when any threshold is breached")
    mo.add_argument("--thresholds", type=Path, default=None,
                    help="monitor thresholds TOML (default: paths.monitor = config/monitor.toml)")
    mo.add_argument("--since", default=None, help="count 429/5xx only in api_calls at or after this ISO date/time "
                                                  "(default: all)")
    mo.add_argument("--filter", type=Path, default=Path("config/filing_filter.toml"))
    mo.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    mo.add_argument("--db", type=Path, default=None, help="database path (overrides paths.db)")
    mo.add_argument("--out", type=Path, default=Path("out/m2"), help="writes <out>/monitor_<run-id>/")
    mr = sub.add_parser("m1_resolve", help="M1: adopted version + previous period for the 7 filers (offline)")
    mr.add_argument("--targets", type=Path, default=Path("config/transport7.toml"))
    mr.add_argument("--config", type=Path, default=None, help="settings TOML (as_of, windows, [resolve])")
    mr.add_argument("--db", type=Path, default=Path("out/m1/m1.sqlite"))
    mr.add_argument("--out", type=Path, default=Path("out/m1"))
    mr.add_argument("--cache", type=Path, default=Path("cache"))
    mr.add_argument("--filter", type=Path, default=Path("config/filing_filter.toml"))
    mr.add_argument("--fetch", action="store_true", help="fetch missing type=1 ZIPs (needs EDINET_API_KEY)")
    mr.add_argument("--correction-note", type=Path, default=None,
                    help="also write the SUBARU correction check (e.g. out/m0/subaru_amendment_note.md)")
    ms = sub.add_parser("m1_segment", help="M1: split the M0 BusinessRisks texts into items (offline)")
    ms.add_argument("--m0", type=Path, default=Path("out/m0"))
    ms.add_argument("--targets", type=Path, default=Path("config/transport7.toml"))
    ms.add_argument("--out", type=Path, default=Path("out/m1"))
    mt = sub.add_parser("m1_match", help="M1: match current vs previous items for the 7 filers (offline)")
    mt.add_argument("--resolve", type=Path, default=Path("out/m1/resolve_transport7.json"))
    mt.add_argument("--segments", type=Path, default=Path("out/m1/segments_transport7.json"))
    mt.add_argument("--cache", type=Path, default=Path("cache"))
    mt.add_argument("--thresholds", type=Path, default=Path("config/thresholds.toml"))
    mt.add_argument("--db", type=Path, default=Path("out/m1/m1.sqlite"), help="write the matches table here")
    mt.add_argument("--out", type=Path, default=Path("out/m1"))
    cp = sub.add_parser("compare", help="M1: extracted text -> segment -> match -> diff for the 7 filers (offline)")
    cp.add_argument("--resolve", type=Path, default=Path("out/m1/resolve_transport7.json"))
    cp.add_argument("--m0", type=Path, default=Path("out/m0"), help="extracted texts {docID}_business_risks.txt")
    cp.add_argument("--cache", type=Path, default=Path("cache"), help="type=1 ZIPs for versions not in --m0")
    cp.add_argument("--thresholds", type=Path, default=Path("config/thresholds.toml"))
    cp.add_argument("--out", type=Path, default=Path("out/m1"), help="writes <out>/run_<run-id>/compare.json")
    cp.add_argument("--run-id", default=None, help="default: JST timestamp YYYYmmddTHHMMSS")
    cp.add_argument("--db", type=Path, default=None,
                    help="also write extracts/items/matches/sentence_changes here (default: <out>/m1.sqlite "
                         "if it exists, e.g. from m1_resolve)")
    rp = sub.add_parser("report", help="M1: Excel + HTML + 照合 from a compare run (offline)")
    rp.add_argument("--run", type=Path, default=None, help="run directory (default: newest out/m1/run_*)")
    rp.add_argument("--out", type=Path, default=None, help="output directory (default: the run directory)")
    rp.add_argument("--gold", type=Path, default=None, help="gold JSON from tools/gold_import.py (optional; not used in M1)")
    ru = sub.add_parser("run", help="M1: resolve -> fetch (missing ZIPs only) -> compare -> report in one go")
    ru.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                    help="forbid network access; missing ZIPs are reported, not fetched")
    ru.add_argument("--fy", default=None, help="fiscal year label, e.g. 2026-03 (default: settings run.fy)")
    ru.add_argument("--as-of", default=None, help="base date T YYYY-MM-DD (default: settings run.as_of)")
    ru.add_argument("--config", type=Path, default=None, help="settings TOML (as_of, windows, [resolve])")
    ru.add_argument("--targets", type=Path, default=Path("config/transport7.toml"))
    ru.add_argument("--filter", type=Path, default=Path("config/filing_filter.toml"))
    ru.add_argument("--thresholds", type=Path, default=Path("config/thresholds.toml"))
    ru.add_argument("--m0", type=Path, default=Path("out/m0"), help="extracted texts {docID}_business_risks.txt")
    ru.add_argument("--cache", type=Path, default=Path("cache"))
    ru.add_argument("--db", type=Path, default=Path("out/m1/m1.sqlite"))
    ru.add_argument("--out", type=Path, default=Path("out/m1"), help="writes <out>/run_<run-id>/")
    ru.add_argument("--run-id", default=None, help="default: JST timestamp + random suffix")
    ru.add_argument("--gold", type=Path, default=None, help="gold JSON from tools/gold_import.py (optional; not used in M1)")
    pu = sub.add_parser("publish", help="weekly peer publication helpers for scripts/publish_peer.ps1 "
                                        "(docs/publish_peer.spec.md; offline)")
    pus = pu.add_subparsers(dest="publish_cmd", required=True)
    pc = pus.add_parser("config", help="print the [publish] settings and resolved paths as JSON (never the API key)")
    pc.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    pt = pus.add_parser("targets", help="merge publish.targets (transport7 + 依頼社リスト) into one targets TOML")
    pt.add_argument("--targets", type=Path, nargs="+", default=None, help="default: settings publish.targets")
    pt.add_argument("--out", type=Path, required=True, help="merged targets TOML to write")
    pt.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    pt.add_argument("--db", type=Path, default=None, help="database for secCode lookup (overrides paths.db)")
    pb = pus.add_parser("build", help="write 社一覧.xlsx, 社別/*.md, 週次要約.md into publish/ and print the "
                                      "sources of risk_diff.xlsx / monitor.xlsx as JSON")
    pb.add_argument("--out-root", type=Path, required=True, help="CLI output root (resolve_*/, m1/run_*/, monitor_*/)")
    pb.add_argument("--resolve-run", required=True, help="resolve run id (monitor run is the newest for it)")
    pb.add_argument("--compare-run", default=None, help="compare run id (default: = --resolve-run)")
    pb.add_argument("--targets", type=Path, required=True, help="merged targets TOML (publish targets --out)")
    pb.add_argument("--publish-dir", type=Path, required=True, help="local publish/ folder")
    pb.add_argument("--config", type=Path, default=None, help="settings TOML (default: config/settings.toml)")
    pb.add_argument("--db", type=Path, default=None, help="database for 証券コード (overrides paths.db; read only)")
    pb.add_argument("--templates", type=Path, default=Path("docs/templates"),
                    help="Copilot 見本ブック (*.xlsx) copied to <publish-dir>/templates/ if the folder exists")
    pb.add_argument("--today", type=_iso_date, default=None, help=argparse.SUPPRESS)  # tests
    pk = pus.add_parser("check", help="success definition (spec §2-5): five files present, 社一覧/monitor of today")
    pk.add_argument("--publish-dir", type=Path, required=True)
    pk.add_argument("--today", type=_iso_date, default=None, help=argparse.SUPPRESS)  # tests
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        with forbid_network() if a.offline else contextlib.nullcontext():
            return _dispatch(a)
    except NetworkForbidden as e:
        logging.error("%s", e)
        return 3


def _dispatch(a) -> int:
    if a.cmd == "run":
        return _run(a)
    if a.cmd == "compare":
        return _compare(a)
    if a.cmd == "report":
        return _report(a)
    if a.cmd == "resolve":
        return _resolve(a)
    if a.cmd == "fetch":
        return _fetch(a)
    if a.cmd == "monitor":
        return _monitor(a)
    if a.cmd == "extract":
        return _extract(a)
    if a.cmd == "publish":
        return _publish(a)
    if a.cmd == "m1_resolve":
        return _m1_resolve(a)
    if a.cmd == "m1_segment":
        from . import m1_segment
        rows = m1_segment.run(a.m0, a.targets, a.out)
        for r in rows:
            print(f"{r['label']} {r['periodEnd']} {r['docID']}: {r['seg'].segmentation} "
                  f"items={len(r['seg'].leaves)} headings={len(r['seg'].items)}")
        return 0 if rows else 2
    if a.cmd == "m1_match":
        from . import m1_match
        from .config import ConfigError
        try:
            res = m1_match.run(a.resolve, a.segments, a.cache, a.thresholds, a.out, a.db)
        except ConfigError as e:
            logging.error("%s", e)
            return 2
        for rec in res["results"]:
            m = rec["result"]
            print(f"{rec['label']} {rec['cur_doc']} x {rec['prev_doc']}: "
                  + (", ".join(f"{k}={v}" for k, v in sorted(m.label_counts().items())) if m else rec["skip"]))
        print(f"matches rows saved={res['saved_rows']} outputs in {a.out}")
        return 0 if all(r["result"] for r in res["results"]) else 2
    if a.cmd == "init-db":
        return _init_db(a)
    if a.cmd == "codes":
        return _codes_sync(a)
    if a.cmd == "population":
        return _population_load(a)
    if a.cmd == "doclist":
        return _doclist_sync(a)
    if a.cmd == "m0_spike":
        from . import m0
        try:
            res = m0.run(a.filer, a.years, a.out, a.cache, a.filter)
        except EdinetError as e:  # messages are already key-masked
            logging.error("%s", e)
            return 1
        for m in res["metas"]:
            c = m["csv"]
            print(f"{m['filerName']} docID={m['docID']} periodEnd={m['periodEnd']} "
                  f"text_chars={m['char_count']} raw_html_chars={m['raw_html_char_count']} "
                  f"csv_len={c.get('csv_len')} csv==30000={c.get('csv_len_exactly_30000')}")
        print(f"scanned {len(res['scanned'])} day(s); network calls this run: {res['network_calls']}; "
              f"outputs in {a.out}")
        return 0 if res["metas"] else 2
    if a.cmd == "m0_truncation":
        from . import truncation
        from .client import EdinetClient
        try:
            res = truncation.run(EdinetClient(cache_dir=a.cache), a.doc_ids, a.out)
        except EdinetError as e:
            logging.error("%s", e)
            return 1
        v = res["verdict"]
        for rec, rows in res["docs"]:
            br = next((r for r in rows if r.element == "BusinessRisksTextBlock"), None)
            ntr = sum(1 for r in rows if r.truncated)
            print(f"{rec.get('docID')} {rec.get('filerName')}: BusinessRisks csv={br.csv_chars if br else '-'} "
                  f"xbrl_text={br.xbrl_text_chars if br else '-'} truncated={br.truncated if br else '-'}; "
                  f"truncated TextBlocks={ntr}")
        print(f"truncated facts={v['truncated_facts']} constant_at_limit={v['constant_at_limit']} "
              f"utf8_bytes={v['measure_ranges']['csv_utf8_bytes']} all_prefix={v['all_truncated_are_prefix']} "
              f"tags_in_csv={v['any_tags_in_csv']} max_untruncated={v['max_untruncated_csv_chars']}")
        print(f"network calls this run: {res['network_calls']}; outputs in {a.out}")
        return 0
    if a.cmd == "m0_multi":
        from . import m0_multi
        try:
            res = m0_multi.run(a.targets, a.out, a.cache, a.filter)
        except EdinetError as e:
            logging.error("%s", e)
            return 1
        for r in res["rows"]:
            print(f"{r['label']} {r['period']} docID={r['docID']} chars={r['chars']} status={r['status'][:80]}")
        print(f"network calls this run: {res['network_calls']}; outputs in {a.out}")
        return 0 if all(r["status"].startswith("OK") for r in res["rows"]) else 2
    if a.cmd == "m0_scan":
        from datetime import date
        from . import m0_scan
        try:
            res = m0_scan.run(date.fromisoformat(a.start), date.fromisoformat(a.end), a.out, a.cache,
                              a.table, fetch=not a.no_fetch)
        except EdinetError as e:
            logging.error("%s", e)
            return 1
        print(f"days ok={len(res['ok'])} failed={len(res['failures'])} docs={sum(res['crosstab'].values())} "
              f"combos={len(res['crosstab'])} table_entries={res['table_size']} unexpected={len(res['unexpected'])}")
        for fc, hs in res["hits"].items():
            print(f"formCode {fc}: {len(hs)}" + (" (zero within period)" if not hs else ""))
        print(f"network calls this run: {res['network_calls']}; outputs in {a.out}")
        return 0 if not res["failures"] else 2
    return 1


def _compare(a) -> int:
    from . import m1_compare
    from .config import ConfigError
    try:
        dbp = a.db or (a.out / "m1.sqlite" if (a.out / "m1.sqlite").exists() else None)
        res = m1_compare.run(a.resolve, a.m0, a.cache, a.thresholds, a.out, a.run_id, db_path=dbp)
    except (ConfigError, FileNotFoundError) as e:
        logging.error("%s", e)
        return 2
    for f in res["data"]["filers"]:
        ps = f["pairs"]
        print(f"{f['label']} {f['cur_doc']} x {f['prev_doc']}: " + (f["skip"] or
              f"pairs={len(ps)} changed={sum(p['change'] == 'changed' for p in ps)} "
              f"minor={sum(p['change'] == 'minor' for p in ps)} review={sum(p['review_flag'] for p in ps)}"))
    print(f"compare.json in {res['run_dir']}")
    if res["db"] is not None:
        c = res["db"]
        print(f"db={dbp}: filers={c['filers']} extracts={c['extracts']} items={c['items']} matches={c['matches']} "
              f"sentence_changes={c['sentence_changes']}" + (f" skipped={c['skipped']}" if c["skipped"] else ""))
    return 0 if all(f["skip"] is None for f in res["data"]["filers"]) else 2


def _report(a) -> int:
    from . import m1_report
    run = a.run
    if run is None:
        runs = sorted(Path("out/m1").glob("run_*/compare.json"))
        if not runs:
            logging.error("no out/m1/run_*/compare.json; run `compare` first")
            return 2
        run = runs[-1].parent
    if not (Path(run) / "compare.json").exists():
        logging.error("%s/compare.json not found; run `compare` first", run)
        return 2
    res = m1_report.run(run, a.out, gold_path=a.gold)
    rc = res["reconcile"]
    print(f"run_id={res['run_id']} xlsx={res['xlsx']} html={len(res['html'])} pages; 照合 全文一致 "
          f"{rc['fulltext_ok']}/{rc['fulltext_rows']}, 手作業 対象外 {rc['gold_missing']}/{rc['rows']}")
    return 0 if rc["fulltext_ok"] == rc["fulltext_rows"] else 2


def _run(a) -> int:
    from datetime import date
    from . import m1_run
    from .config import ConfigError, load_settings
    try:
        s = load_settings(a.config)
        inp = m1_run.Inputs(targets=a.targets, cache_dir=a.cache, filter_path=a.filter, thresholds=a.thresholds,
                            m0_dir=a.m0, out_dir=a.out, db_path=a.db, gold=a.gold,
                            as_of=date.fromisoformat(a.as_of) if a.as_of else s.run.as_of,
                            fy=a.fy or s.run.fy, window=s.window.current, resolve=s.resolve)
        res = m1_run.run(inp, offline=a.offline, run_id=a.run_id)
    except (ConfigError, FileNotFoundError, ValueError) as e:
        logging.error("%s", e)
        return 2
    filers = res["compare"]["data"]["filers"]
    for f in filers:
        ps = f["pairs"]
        print(f"{f['label']} {f['cur_doc']} x {f['prev_doc']}: " + (f["skip"] or
              f"pairs={len(ps)} changed={sum(p['change'] == 'changed' for p in ps)} "
              f"minor={sum(p['change'] == 'minor' for p in ps)} review={sum(p['review_flag'] for p in ps)}"))
    fe, rc, fp = res["fetch"], res["report"]["reconcile"], res["fingerprint"]
    print(f"fetch: cached={len(fe['cached'])} fetched={len(fe['fetched'])} missing={len(fe['missing'])}"
          + "".join(f"\n  missing {d}: {why}" for d, why in fe["missing"].items()))
    print(f"api_calls this run: documents={res['doc_api_calls']} documents.json="
          f"{res['api_calls'].get('documents.json', 0)} (network calls={res['network_calls']}"
          + (", --offline" if a.offline else "") + ")")
    print(f"report: xlsx={res['report']['xlsx']} html={len(res['report']['html'])} pages; "
          f"照合 全文一致 {rc['fulltext_ok']}/{rc['fulltext_rows']}")
    d = fp["digest"]
    print(f"fingerprint: tables={d['tables'][:12]} excel={(d['excel'] or '-')[:12]} html={d['html'][:12]} "
          f"files={d['files'][:12]}")
    prev = m1_run.previous_fingerprint(a.out, res["run_id"])
    if prev is not None:
        bad = m1_run.diff_fingerprints(prev, fp)
        print(f"idempotency vs run {prev['run_id']}: " + ("identical (tables, excel, html, files)" if not bad
                                                          else "DIFFERENT: " + ", ".join(bad)))
    print(f"run_id={res['run_id']} outputs in {res['run_dir']}")
    ok = all(f["skip"] is None for f in filers) and rc["fulltext_ok"] == rc["fulltext_rows"]
    return 0 if ok else 2


def _init_db(a) -> int:
    from .config import ConfigError, load_settings
    from .store import db, migrate
    try:
        path = a.db or load_settings(a.config).paths.db_path
    except ConfigError as e:
        logging.error("%s", e)
        return 1
    conn = db.connect(path)
    try:
        before = migrate.current_version(conn)
        applied = migrate.migrate(conn)
        after = migrate.current_version(conn)
        tables = db.tables(conn)
    except migrate.MigrationError as e:
        logging.error("%s", e)
        return 1
    finally:
        conn.close()
    print(f"db={path} schema v{before} -> v{after} (applied: {applied or 'none'}); tables={len(tables)}: "
          + ", ".join(tables))
    return 0


def _codes_sync(a) -> int:
    from .config import ConfigError, load_settings
    from .net.download import Downloader
    from .population import edinet_codes
    from .store import db
    try:
        s = load_settings(a.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    path = a.db or s.paths.db_path
    conn = db.open_db(path)
    try:
        dl = Downloader(min_interval=s.api.min_interval, max_retries=s.api.max_retries,
                        backoff_base=s.api.backoff_base, backoff_max=s.api.backoff_max, timeout=s.api.timeout,
                        audit_hook=db.api_call_recorder(conn, None))
        res = edinet_codes.sync(conn, a.cache or Path(s.paths.cache_dir), downloader=dl, refresh=a.refresh,
                                from_file=a.from_file, aggregation_file=a.aggregation_file)
    except (edinet_codes.CodesError, EdinetError) as e:
        logging.error("%s", e)
        return 2
    finally:
        conn.close()
    c, g = res["edinet_codes"], res["code_aggregation"]
    print(f"edinet_codes: {c['rows']} rows (list date {c['list_date']}, {c['origin']} {c['path']} "
          f"sha256={c['sha256'][:12]}) listed={c['listed']} unlisted={c['unlisted']} blank={c['listed_blank']} "
          f"sec_code={c['with_sec_code']}; new={c['new']} changed={c['changed']} unchanged={c['unchanged']} "
          f"stale={c['stale']}")
    print(f"code_aggregation: {g['rows']} rows ({g['origin']} {g['path']} sha256={g['sha256'][:12]}); "
          f"new={g['new']} changed={g['changed']} unchanged={g['unchanged']} stale={g['stale']}"
          + (f"; continuing code not in edinet_codes: {', '.join(g['new_code_unknown'])}"
             if g["new_code_unknown"] else ""))
    print(f"db={path} network calls this run: {res['network_calls']}")
    return 0


def _iso_date(value: str):
    from datetime import date
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date YYYY-MM-DD: {value!r}") from None


def _doclist_sync(a) -> int:
    import uuid
    from datetime import datetime
    from .config import ConfigError, api_key, load_settings
    from .ingest import doclist
    from .net.edinet_api import EdinetClient
    from .store import db
    try:
        s = load_settings(a.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    cache = a.cache or Path(s.paths.cache_dir)
    today = a.today or doclist.today_jst()
    if a.date_to is not None and a.date_from is None:
        logging.error("--to needs --from")
        return 2
    try:
        if a.date_from is not None:
            mode, dates = "range", doclist.date_range(a.date_from, a.date_to or a.date_from)
        elif a.daily:
            mode, dates = "daily", doclist.daily_dates(today)
        elif a.rescan_month:
            ws = {"current": [s.window.current], "previous": [s.window.previous],
                  "both": [s.window.current, s.window.previous]}[a.window]
            mode, dates = f"rescan-month({a.window})", doclist.window_dates(ws, today)
        else:
            mode, dates = "from-cache", sorted(f.stem for f in (cache / "doclist").glob("*.json"))
    except ValueError as e:
        logging.error("%s", e)
        return 2
    cache_only = a.from_cache or a.offline
    plan = doclist.plan_sync(cache, dates, today, refresh=a.rescan_month or a.refresh)
    span = f"{dates[0]}..{dates[-1]}" if dates else "-"
    print(f"doclist sync {mode}: days={len(plan.dates)} ({span}) cached_final={len(plan.cached_final)} "
          f"cached_not_final={len(plan.cached_stale)} future={len(plan.future)}")
    if a.dry_run:
        est, gap = (0 if cache_only else plan.calls), max(1.0, s.api.min_interval)
        print(f"dry-run: documents.json calls={est} ("
              + ("--rescan-month/--refresh: cached days are fetched again" if a.rescan_month or a.refresh
                 else "cached final days excluded")
              + f"); >= {est * gap / 60:.1f} min at {gap:g}s/call (retries not counted)"
              + (" [cache only: no calls]" if cache_only else ""))
        if not cache_only and plan.calls and doclist.quiet_now(s.api.doclist_quiet_start, s.api.doclist_quiet_end):
            print(f"note: now is within {s.api.doclist_quiet_start}-{s.api.doclist_quiet_end} JST; "
                  "documents.json would not be called")
        return 0
    client = None
    if not cache_only and plan.calls:
        try:
            key = api_key(s)
        except ConfigError as e:
            logging.error("%s", e)
            return 2
        if not key:
            logging.error("EDINET_API_KEY is not set (or use --offline / --from-cache)")
            return 2
    path = a.db or s.paths.db_path
    conn = db.open_db(path)
    run_id = "doclist-" + datetime.now(db.JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    try:
        db.start_run(conn, run_id, "doclist sync", code_version=doclist.PROCESSOR_VERSION,
                     args={"mode": mode, "from": dates[0] if dates else None, "to": dates[-1] if dates else None,
                           "cache_only": bool(cache_only), "refresh": bool(a.rescan_month or a.refresh)})
        if not cache_only and plan.calls:
            client = EdinetClient(api_key=key, cache_dir=cache, min_interval=s.api.min_interval,
                                  max_retries=s.api.max_retries, backoff_base=s.api.backoff_base, backoff_max=s.api.backoff_max,
                                  timeout=s.api.timeout, base_url=s.api.base_url,
                                  audit_hook=db.api_call_recorder(conn, run_id),
                                  doclist_quiet=(s.api.doclist_quiet_start, s.api.doclist_quiet_end))
        res = doclist.sync(conn, cache, plan, client)
        ok = not res.failed and res.stopped is None and not (res.missing and not cache_only)
        with db.transaction(conn):
            conn.execute("UPDATE runs SET meta_synced_at = (SELECT max(fetched_at) FROM doclist_raw) "
                         "WHERE run_id = ?", (run_id,))
        db.finish_run(conn, run_id, "ok" if ok else ("aborted" if res.stopped else "failed"))
        n_raw = conn.execute("SELECT count(*) FROM doclist_raw").fetchone()[0]
        n_days = conn.execute("SELECT count(DISTINCT file_date) FROM doclist_raw").fetchone()[0]
    finally:
        conn.close()
    print(f"imported day files={res.imported_days} (rows offered={res.raw_rows}); fetched={len(res.fetched)} "
          f"failed={len(res.failed)} not fetched={len(res.missing)}"
          + (" (cache only)" if cache_only and res.missing else "")
          + (f"; STOPPED ({res.stopped})" if res.stopped else ""))
    for d, why in res.failed.items():
        print(f"  failed {d}: {why}")
    print(f"db={path}: doclist_raw rows={n_raw} days={n_days} documents={res.documents}; "
          f"network calls={client.calls if client else 0} run_id={run_id}")
    if res.stopped == "auth":
        return 4
    return 0 if ok else 2


def _as_of_month(value: str) -> str:
    from .population.jpx import JpxError, check_as_of_month
    try:
        return check_as_of_month(value)
    except JpxError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _population_load(a) -> int:
    from .config import ConfigError, load_settings
    from .population import jpx
    from .store import db
    try:
        path = a.db or load_settings(a.config).paths.db_path
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    conn = db.open_db(path)
    try:
        res = jpx.load(conn, a.jpx, a.as_of_month)
    except jpx.JpxError as e:
        logging.error("%s", e)
        return 2
    finally:
        conn.close()
    print(f"population {res['as_of_month']}: {res['population']} rows ({jpx.PRIME_DOMESTIC}) of {res['file_rows']} "
          f"in {res['path']} (日付 {res['list_date']}, sha256={res['sha256'][:12]}); replaced={res['replaced']}")
    print("match_method: " + ", ".join(f"{k}={v}" for k, v in res["match_method"].items()))
    if res["excluded_class_shares"]:
        print(f"excluded {len(res['excluded_class_shares'])} 優先株・種類株式 (5-character コード): "
              + ", ".join(c for c, _ in res["excluded_class_shares"]))
    for code, cands in res["multi_candidates"].items():
        print(f"  multiple EDINET codes for {code}: {', '.join(cands)}")
    for code, name in res["unmatched"]:
        print(f"  unmatched {code} {name}")
    for code, ecode in res["matched_unlisted"]:
        print(f"  matched to a code not marked 上場: {code} -> {ecode}")
    print(f"db={path}")
    return 0


def _resolve(a) -> int:
    import uuid
    from . import m1_resolve
    from .config import ConfigError, load_settings
    from .m0_multi import load_targets
    from .resolve import runner
    from .resolve.entity import EntityError, load_successor_map
    from .resolve.reasons import Target
    from .resolve.versions import FilingFilter
    from .store import db
    try:
        s = load_settings(a.config)
        path = a.db or s.paths.db_path
        fy = a.fy or s.run.fy
        window = s.window.current if fy == s.run.fy else runner.fy_window(fy)
    except (ConfigError, ValueError) as e:
        logging.error("%s", e)
        return 2
    as_of = a.as_of or s.run.as_of
    conn = db.open_db(path)
    try:
        if a.successor_map and a.successor_map.exists():
            sm = load_successor_map(conn, a.successor_map)
            print(f"successor_map: {sm['rows']} rows from {sm['path']}")
        targets_path = Path("config/transport7.toml") if a.transport7 else a.targets
        if a.entity:
            scope, targets = "entity", runner.code_targets(conn, a.entity)
        elif targets_path:
            filers, _ = load_targets(targets_path)
            scope = f"targets:{targets_path.name}"
            targets = [Target(f["label"], f["edinetCode"], f["label"], scope) for f in filers]
        else:
            month, targets = runner.population_targets(conn, a.as_of_month)
            scope = f"population:{month}"
        run_id = a.run_id or f"resolve-{db.now_iso()[:19].replace('-', '').replace(':', '')}-{uuid.uuid4().hex[:4]}"
        runner.start(conn, run_id, "resolve", fy=fy, as_of=as_of,
                     args={"scope": scope, "filter": str(a.filter), "cache": str(a.cache)})
        texts = m1_resolve.TextSource(a.cache, fetch=False)
        out = runner.resolve_targets(conn, targets, run_id=run_id, as_of=as_of, window=window, cfg=s.resolve,
                                     flt=FilingFilter.from_toml(a.filter), cache_dir=a.cache, text_of=texts,
                                     available=lambda d: texts.zip_path(d).exists())
        db.finish_run(conn, run_id, "ok")
    except (EntityError, ValueError, FileNotFoundError) as e:
        logging.error("%s", e)
        return 2
    finally:
        conn.close()
    summ = runner.summarize(out)
    meta = {"run_id": run_id, "scope": scope, "fy": fy, "as_of": as_of.isoformat(), "db": str(path),
            "window": [window.period_end_from.isoformat(), window.period_end_to.isoformat()]}
    paths = runner.write_outputs(a.out / f"resolve_{run_id}", summ, meta)
    if targets_path:   # same shape as m1_resolve's resolve_transport7.json (m1_match / compare input)
        rows = [m1_resolve._row({"label": t.key, "edinetCode": t.edinet_code}, r) for t, r in out.results]
        (paths["json"].parent / "resolve_transport7.json").write_text(json.dumps(
            {"as_of": as_of.isoformat(), "rows": rows, "text_missing": texts.missing},
            ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"resolve {scope} FY {fy} T={as_of} window {meta['window'][0]}..{meta['window'][1]} run_id={run_id}")
    print(f"targets={summ['targets']} entities={summ['entities']} duplicates={len(summ['duplicates'])} "
          f"matched(当期採用版あり)={summ['matched']} with_prev={summ['with_prev']} unknown={summ['unknown']}")
    print("突合の理由: " + (", ".join(f"{k}={v}" for k, v in summ["cur_reasons"].items()) or "なし"))
    print("前期なしの理由: " + (", ".join(f"{k}={v}" for k, v in summ["no_prev_reasons"].items()) or "なし"))
    if summ["unknown"]:
        print("unknown_cause: " + ", ".join(f"{k}={v}" for k, v in summ["unknown_causes"].items()))
    print("flags: " + ", ".join(f"{k}={v}" for k, v in summ["flags"].items())
          + f" (訂正の本文未取得で一部未判定: {summ['texts_pending']}社)")
    print(f"edinetCode なしでスキップした書類: {summ['skipped_no_edinet_code']}; "
          f"documents.entity_id を読み替えたコード: {summ['documents_remapped']}")
    for n in summ["entity_notes"]:
        print(f"  entity note: {json.dumps(n, ensure_ascii=False)}")
    print(f"outputs: {paths['md'].parent}")
    return 0


def _publish(a) -> int:
    from . import peer_publish as publish
    from .config import ConfigError, api_key, load_settings
    from .store import db
    if a.publish_cmd == "check":
        bad = publish.check(a.publish_dir, a.today)
        for b in bad:
            print(f"  {b}")
        print(f"publish check {a.publish_dir}: " + ("ok (5 files, 社一覧/monitor of today)" if not bad
                                                     else f"NOT OK ({len(bad)} problem(s)); do not sync"))
        return 0 if not bad else 2
    try:
        s = load_settings(a.config)
        if a.publish_cmd == "config":
            try:
                key_source = "env" if os.environ.get("EDINET_API_KEY", "").strip() else (
                    "file" if s.paths.api_key_file and api_key(s) else "none")
            except ConfigError:
                key_source = "unreadable_file"
            p = s.publish
            print(json.dumps({"dest_dir": p.dest_dir, "work_dir": p.work_dir, "out_dir": p.out_dir,
                              "targets": p.targets, "doclist_days": p.doclist_days, "db": str(s.paths.db_path),
                              "cache_dir": s.paths.cache_dir, "settings": s.source, "api_key_source": key_source}))
            return 0
        path = a.db or s.paths.db_path
        if a.publish_cmd == "targets":
            conn = db.connect(path) if Path(path).exists() else None
            try:
                filers, periods = publish.merge_targets(a.targets or [Path(t) for t in s.publish.targets], conn)
            finally:
                if conn is not None:
                    conn.close()
            publish.write_targets(a.out, filers, periods)
            print(f"publish targets: {len(filers)} filer(s) -> {a.out}"
                  + "".join(f"\n  {f.get('secCode', '-')} {f['edinetCode']} {f['label']}" for f in filers))
            return 0
        res = publish.build(a.publish_dir, a.out_root, a.resolve_run, a.targets, compare_run=a.compare_run,
                            db_path=path, today=a.today, templates=a.templates)
    except (ConfigError, publish.PublishError) as e:
        logging.error("%s", e)
        return 2
    print(json.dumps({"publish_dir": str(res["publish_dir"]), "companies": res["companies"], "md": res["md"],
                      "templates": res["templates"],
                      "monitor_dir": str(res["monitor_dir"]),
                      "copies": {k: str(v) for k, v in res["copies"].items()}}))
    return 0


def _extract(a) -> int:
    from .config import ConfigError, load_settings
    from .extract import EXTRACTOR_VER
    from .ingest import extract_bulk
    from .store import db
    try:
        s = load_settings(a.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    path = a.db or s.paths.db_path
    if not Path(path).exists():
        logging.error("database not found: %s", path)
        return 2
    conn = db.open_db(path)
    try:
        docs = None
        if a.entity:
            q = ", ".join("?" for _ in a.entity)
            docs = {r[0] for r in conn.execute(f"SELECT doc_id FROM documents WHERE edinet_code IN ({q})", a.entity)}
        res = extract_bulk.run(conn, doc_ids=docs, force=a.force)
        total = conn.execute("SELECT COUNT(*) FROM extracts WHERE extractor_ver = ?", (EXTRACTOR_VER,)).fetchone()[0]
    finally:
        conn.close()
    print(f"extract {EXTRACTOR_VER}: extracted={res.extracted} kept={res.kept} empty={len(res.empty)} "
          f"multi_hit={len(res.multi)} missing_file={len(res.missing_file)}; extracts rows={total}")
    for k, ds in (("empty", res.empty), ("multi_hit", res.multi), ("missing_file", res.missing_file)):
        for d in ds:
            print(f"  {k} {d}")
    return 0


def _fetch(a) -> int:
    import uuid
    from datetime import datetime
    from .config import ConfigError, api_key, load_settings
    from .ingest import fetch
    from .m0_multi import load_targets
    from .net.edinet_api import EdinetClient
    from .resolve.entity import EntityMap
    from .store import db
    try:
        s = load_settings(a.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    cache = a.cache or Path(s.paths.cache_dir)
    path = a.db or s.paths.db_path
    max_attempts = a.max_attempts or s.api.fetch_max_attempts
    targets_path = Path("config/transport7.toml") if a.transport7 else a.targets
    narrowed = bool(a.entity or targets_path)
    if not (narrowed or a.all or a.dry_run or a.offline):
        logging.error("fetch without --entity / --targets / --transport7 would request every body of the resolve "
                      "run; pass --all to do that on purpose (or --dry-run to see the count first)")
        return 2
    if not Path(path).exists():
        logging.error("database not found: %s", path)
        return 2
    # --dry-run must not write: plain connect (no migration), reads only
    conn = db.connect(path) if a.dry_run else db.open_db(path)
    try:
        resolve_run = a.run_id or fetch.latest_resolve_run(conn)
        if resolve_run is None or conn.execute("SELECT 1 FROM filing_selection WHERE run_id = ? LIMIT 1",
                                               (resolve_run,)).fetchone() is None:
            logging.error("no filing_selection rows%s; run `resolve` first",
                          f" for run {resolve_run}" if resolve_run else "")
            return 2
        entities, scope = None, "all"
        if narrowed:
            codes = a.entity or [f["edinetCode"] for f in load_targets(targets_path)[0]]
            emap = EntityMap.from_db(conn)
            entities = {emap(c) for c in codes}
            scope = "entity" if a.entity else f"targets:{targets_path.name}"
            present = {r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM filing_selection WHERE run_id = ?",
                                                  (resolve_run,))}
            for e in sorted(entities - present):
                print(f"  not in resolve run {resolve_run}: {e}")
        targets = fetch.selection_targets(conn, resolve_run, entities, include_pending=a.include_pending)
        plan = fetch.plan_fetch(conn, targets, cache, retry_failed=a.retry_failed)
        cnt = plan.count()
        print(f"fetch {scope} from resolve run {resolve_run}: entities={len({t.entity_id for t in targets})} "
              f"docs={len(targets)} (cur/prev doc_ids{' + texts_pending' if a.include_pending else ''}) "
              + " ".join(f"{k}={cnt.get(k, 0)}" for k in fetch.ACTIONS))
        if a.dry_run:
            gap = max(1.0, s.api.min_interval)
            print(f"dry-run: documents API calls={plan.calls}; >= {plan.calls * gap / 60:.1f} min at {gap:g}s/call "
                  f"(backoff not counted); already have={cnt.get('have', 0)}, from cache without a call="
                  f"{cnt.get('adopt', 0)}" + (" [--offline: no calls would be made]" if a.offline else ""))
            for i in plan.items:
                if i.action in ("fetch", "failed"):
                    print(f"  {i.action} {i.target.doc_id} {i.target.entity_id} {'/'.join(i.target.roles)}: {i.why}")
            return 0
        key = ""
        if plan.calls and not a.offline:
            try:
                key = api_key(s)
            except ConfigError as e:
                logging.error("%s", e)
                return 2
            if not key:
                logging.error("EDINET_API_KEY is not set (or use --offline / --dry-run)")
                return 2
        run_id = "fetch-" + datetime.now(db.JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
        db.start_run(conn, run_id, "fetch", code_version=fetch.PROCESSOR_VERSION,
                     args={"scope": scope, "resolve_run": resolve_run, "retry_failed": bool(a.retry_failed),
                           "include_pending": bool(a.include_pending), "offline": bool(a.offline), "max_attempts": max_attempts})
        client = None
        if plan.calls and not a.offline:
            client = EdinetClient(api_key=key, cache_dir=cache, min_interval=s.api.min_interval,
                                  max_retries=s.api.max_retries, backoff_base=s.api.backoff_base,
                                  backoff_max=s.api.backoff_max, timeout=s.api.timeout, base_url=s.api.base_url,
                                  audit_hook=db.api_call_recorder(conn, run_id))
        res = fetch.execute(conn, plan, cache, run_id=run_id, client=client, key=key, max_attempts=max_attempts)
        ok = not res.failed and not res.retry and res.stopped is None and not (res.not_fetched and not a.offline)
        db.finish_run(conn, run_id, "ok" if ok else ("aborted" if res.stopped else "failed"))
        queue = fetch.queue_counts(conn, [t.doc_id for t in targets])
    finally:
        conn.close()
    print(f"fetched={len(res.fetched)} adopted_from_cache={len(res.adopted)} failed={len(res.failed)} "
          f"retry={len(res.retry)} not_fetched={len(res.not_fetched)}"
          + (" (--offline: left pending)" if a.offline and res.not_fetched else "")
          + (f"; STOPPED (401/403): {res.stop_error}" if res.stopped else ""))
    for d, why in {**res.failed, **res.retry}.items():
        print(f"  {'failed' if d in res.failed else 'retry'} {d}: {why}")
    print("fetches (these docs): " + (", ".join(f"{k}={v}" for k, v in queue.items()) or "none"))
    print(f"db={path} documents API calls={client.calls if client else 0} run_id={run_id}")
    if res.stopped == "auth":
        return 4
    return 0 if ok else 2


def _monitor(a) -> int:
    import uuid
    from datetime import datetime
    from .config import ConfigError, api_key, load_settings
    from .ingest.fetch import latest_resolve_run, scrub
    from .monitor import metrics, output
    from .resolve import runner
    from .resolve.versions import FilingFilter
    from .store import db
    try:
        s = load_settings(a.config)
        mpath = a.thresholds or (Path(s.paths.monitor) if Path(s.paths.monitor).exists() else None)
        cfg = metrics.load_config(mpath)
        flt = FilingFilter.from_toml(a.filter) if a.filter.exists() else FilingFilter()
        try:
            key = api_key(s)        # only to scrub it from the outputs; never printed
        except ConfigError:
            key = ""
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    path = a.db or s.paths.db_path
    if not Path(path).exists():
        logging.error("database not found: %s", path)
        return 2
    with forbid_network():          # the database is the only input
        conn = db.open_db(path)
        try:
            resolve_run = a.run or latest_resolve_run(conn)
            if resolve_run is None:
                logging.error("no ok resolve run with filing_selection rows; run `resolve` first")
                return 2
            fy = conn.execute("SELECT fy FROM runs WHERE run_id = ?", (resolve_run,)).fetchone()
            fy = fy[0] if fy else None
            window = None if not fy else (s.window.current if fy == s.run.fy else runner.fy_window(fy))
            run_id = "monitor-" + datetime.now(db.JST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
            previous = metrics.previous_monitor_run(conn)
            rep = metrics.compute(conn, resolve_run, cfg=cfg, flt=flt, window=window, since=a.since, run_id=run_id,
                                  previous=previous)
            db.start_run(conn, run_id, "monitor", code_version=metrics.PROCESSOR_VERSION, fy=fy,
                         as_of=rep.meta.get("as_of"),
                         args={"resolve_run": resolve_run, "config_version": cfg.version, "since": a.since,
                               "previous_monitor_run": previous})
            metrics.save(conn, rep)
            db.finish_run(conn, run_id, "ok")
        except ValueError as e:
            logging.error("%s", e)
            return 2
        finally:
            conn.close()
    paths = output.write(rep, a.out / f"monitor_{run_id}", key=key)
    print(f"monitor resolve run {resolve_run} ({rep.meta.get('scope')}, FY {rep.meta.get('fy')}, "
          f"T={rep.meta.get('as_of')}) thresholds {cfg.version} run_id={run_id}")
    shown = ("match_rate", "adopted_rate", "unmatched", "unknown", "form_out_of_scope", "fetch_failed", "fetch_pending",
             "empty_extract", "extract_missing", "multi_hit", "api_429_5xx")
    for m in rep.metrics:
        if m.metric in shown or m.breached:
            v = "-" if m.value is None else (f"{m.value:.4f}" if not float(m.value).is_integer() else f"{m.value:g}")
            print(f"  {'BREACH ' if m.breached else ''}{m.metric}={v}"
                  + (f" (threshold {m.threshold:g})" if m.threshold is not None else "")
                  + (f" {scrub(m.note, key)}" if m.note else ""))
    print(f"details={len(rep.details)} rows; breached={len(rep.breached)}: "
          + (", ".join(m.metric for m in rep.breached) or "none"))
    print(f"outputs: {paths['xlsx']} {paths['summary_csv']} {paths['details_csv']}")
    return 2 if a.fail_on_threshold and rep.breached else 0


def _m1_resolve(a) -> int:
    from . import m1_resolve
    from .config import ConfigError, load_settings
    try:
        s = load_settings(a.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    res = m1_resolve.run(a.targets, a.cache, a.filter, a.out, a.db, as_of=s.run.as_of,
                         window=s.window.current, cfg=s.resolve, fetch=a.fetch)
    for r in res["rows"]:
        print(f"{r['label']}: cur={r['cur_doc_id']}({r['cur_kind']}) prev={r['prev_doc_id']}({r['prev_kind']}) "
              f"flags={r['cur_flags'] + r['prev_flags']} no_prev={r['no_prev_reason']}")
    if a.correction_note:
        print("correction:", m1_resolve.write_correction_note(a.correction_note, res))
    print(f"run_id={res['run_id']} outputs in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
