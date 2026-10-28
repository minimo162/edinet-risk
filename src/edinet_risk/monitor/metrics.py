"""`monitor`: the plan §8-3 metrics of one resolve run, threshold checks, ``metrics`` rows (M2-8).

Input is the database only (offline): ``filing_selection`` of a resolve run (default: the latest ok one),
``fetches`` / ``extracts`` / ``items`` / ``matches`` of its cur/prev doc_ids, ``documents`` (formCodes
excluded by the filing filter), ``api_calls`` (429 / 5xx) and ``population`` (denominator).

Metrics come in two kinds:

- run-wide (``Metric``): one value per run. A metric breaches when a limit is configured for it
  (``config/monitor.toml`` ``[thresholds]``; ``match_rate`` is a lower bound, every other one an upper
  bound) and the value is past it. Metrics with no limit are 一覧化 only (never breach).
  ``excluded_form.*`` breach on a sudden change against the previous monitor run (``[excluded_form]``).
- per entity / document (``Detail``): the lists behind the counts (unmatched with reason, flags,
  form_out_of_scope, fetch failures, M1 anomalies). The M1 per-company checks use ``[entity]``.

M1 metrics are computed from whatever extracts / items / matches exist (the latest processor version
per doc); docs not processed yet are counted as not extracted, not as anomalies.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Iterable, Mapping

from ..config import ConfigError, Window, _read_toml
from ..resolve.reasons import CUR_REASONS, PREV_REASONS
from ..resolve.runner import FLAGS
from ..resolve.versions import FilingFilter
from ..store import db

PROCESSOR_VERSION = "monitor-1"
DEFAULT_CONFIG = Path("config/monitor.toml")
FETCH_STATUSES = ("ok", "pending", "retry", "failed", "no_xbrl", "none")   # none = no fetches row yet
LOWER_BOUND = ("match_rate",)


# ---------------------------------------------------------------- config (config/monitor.toml)
@dataclass(frozen=True)
class EntityLimits:
    items_min: int = 1                  # items <= this -> anomaly
    items_max: int = 60                 # items >= this -> anomaly
    items_prev_change: float = 0.5      # |cur/prev - 1| > this -> anomaly
    chars_prev_change: float = 0.4
    image_text_max_chars: int = 2000    # img_count > 0 and char_len < this -> anomaly
    review_ratio_max: float = 0.30      # review_flag pairs / pairs > this -> anomaly


@dataclass(frozen=True)
class MonitorConfig:
    version: str = "m2-initial"
    thresholds: Mapping[str, float] = field(default_factory=lambda: {
        "match_rate": 0.99, "unknown": 0, "fetch_failed": 0, "empty_extract": 0, "multi_hit": 0,
        "api_429_5xx": 0})
    excluded_form_change_ratio: float = 0.5
    excluded_form_change_min: int = 10
    entity: EntityLimits = field(default_factory=EntityLimits)
    source: str = ""


# Metric ids a [thresholds] entry may name (prefix.* = any suffix).
_STATIC = ("match_rate", "adopted_rate", "unmatched", "unknown", "form_out_of_scope", "fetch_failed", "no_xbrl",
           "fetch_pending", "empty_extract", "extract_missing", "multi_hit", "items_out_of_range",
           "items_prev_change", "chars_prev_change", "image_low_text", "segment_fallback", "review_ratio_high",
           "api_429", "api_5xx", "api_429_5xx", "excluded_form_total")
_PREFIXES = ("cur_reason.", "no_prev_reason.", "flag.", "fetches.")


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {v!r}")
    return float(v)


def load_config(path: str | Path | None = None) -> MonitorConfig:
    """``path`` (default config/monitor.toml); a missing default file means built-in defaults."""
    p = Path(path) if path else DEFAULT_CONFIG
    if path is None and not p.exists():
        return MonitorConfig()
    data = dict(_read_toml(p))
    base = MonitorConfig()
    unknown = set(data) - {"version", "thresholds", "excluded_form", "entity"}
    if unknown:
        raise ConfigError(f"{p}: unknown key(s) {sorted(unknown)}")
    th = dict(base.thresholds)
    for k, v in data.get("thresholds", {}).items():
        if k not in _STATIC and not k.startswith(_PREFIXES):
            raise ConfigError(f"{p}: [thresholds] unknown metric {k!r}")
        th[k] = _num(v, f"{p}: thresholds.{k}")
    if not 0.0 <= th.get("match_rate", 0.0) <= 1.0:
        raise ConfigError(f"{p}: thresholds.match_rate must be in [0, 1]")
    ef = data.get("excluded_form", {})
    if set(ef) - {"change_ratio", "change_min"}:
        raise ConfigError(f"{p}: [excluded_form] unknown key(s) {sorted(set(ef) - {'change_ratio', 'change_min'})}")
    ent_vals = {}
    ent_fields = {f.name: f for f in fields(EntityLimits)}
    for k, v in data.get("entity", {}).items():
        if k not in ent_fields:
            raise ConfigError(f"{p}: [entity] unknown key {k!r}")
        x = _num(v, f"{p}: entity.{k}")
        ent_vals[k] = int(x) if ent_fields[k].type == "int" else x
    return MonitorConfig(version=str(data.get("version", base.version)), thresholds=th,
                         excluded_form_change_ratio=_num(ef.get("change_ratio", base.excluded_form_change_ratio),
                                                         f"{p}: excluded_form.change_ratio"),
                         excluded_form_change_min=int(_num(ef.get("change_min", base.excluded_form_change_min),
                                                           f"{p}: excluded_form.change_min")),
                         entity=EntityLimits(**ent_vals), source=str(p))


# ---------------------------------------------------------------- result types
@dataclass
class Metric:
    metric: str
    label: str
    phase: str                     # 初出フェーズ (§8-3)
    value: float | None
    threshold: float | None = None
    breached: bool = False
    note: str = ""


@dataclass
class Detail:
    category: str                  # unmatched | no_prev | flag | form_out_of_scope | fetch | m1 | api | excluded_form
    metric: str
    entity_id: str = ""
    key: str = ""
    name: str = ""
    role: str = ""
    doc_id: str = ""
    value: float | None = None
    threshold: float | None = None
    detail: str = ""


@dataclass
class Report:
    run_id: str
    resolve_run: str
    meta: dict
    metrics: list[Metric] = field(default_factory=list)
    details: list[Detail] = field(default_factory=list)

    @property
    def breached(self) -> list[Metric]:
        return [m for m in self.metrics if m.breached]


# ---------------------------------------------------------------- data loading
def _latest(rows: Iterable[sqlite3.Row], ver_col: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        if d["doc_id"] not in out or str(d[ver_col]) >= str(out[d["doc_id"]][ver_col]):
            out[d["doc_id"]] = d
    return out


def _load_selection(conn: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    ents: dict[str, dict] = defaultdict(dict)
    for r in conn.execute("SELECT entity_id, role, doc_id, period_end, flags_json, no_prev_reason, reason_basis_json "
                          "FROM filing_selection WHERE run_id = ? ORDER BY entity_id, role", (run_id,)):
        d = dict(r)
        d["flags"] = json.loads(d.pop("flags_json") or "[]")
        d["basis"] = json.loads(d.pop("reason_basis_json") or "{}")
        ents[d["entity_id"]][d["role"]] = d
    return dict(ents)


def _population_size(conn: sqlite3.Connection, scope: str) -> int | None:
    if not scope.startswith("population:"):
        return None
    return conn.execute("SELECT count(*) FROM population WHERE as_of_month = ?", (scope.split(":", 1)[1],)).fetchone()[0]


def _names(conn: sqlite3.Connection, scope: str) -> tuple[dict[str, str], dict[str, str]]:
    by_code = {r[0]: r[1] for r in conn.execute("SELECT edinet_code, name FROM edinet_codes")}
    by_key = {}
    if scope.startswith("population:"):
        by_key = {r[0]: r[1] for r in conn.execute("SELECT sec_code4, name FROM population WHERE as_of_month = ?",
                                                   (scope.split(":", 1)[1],))}
    return by_code, by_key


def previous_monitor_run(conn: sqlite3.Connection, before: str | None = None) -> str | None:
    sql = "SELECT run_id FROM runs WHERE cmd = 'monitor' AND status = 'ok'"
    args: list = []
    if before:
        sql += " AND run_id != ?"
        args.append(before)
    r = conn.execute(sql + " ORDER BY started_at DESC, run_id DESC LIMIT 1", args).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- compute
def compute(conn: sqlite3.Connection, resolve_run: str, *, cfg: MonitorConfig | None = None,
            flt: FilingFilter | None = None, window: Window | None = None, since: str | None = None,
            run_id: str = "", previous: str | None = None) -> Report:
    """All §8-3 metrics for ``resolve_run``. ``since``: only api_calls at or after it (ISO, JST)."""
    cfg, flt = cfg or MonitorConfig(), flt or FilingFilter()
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (resolve_run,)).fetchone()
    if run is None:
        raise ValueError(f"resolve run not found: {resolve_run}")
    scope = json.loads(run["args_json"] or "{}").get("scope", "")
    sel = _load_selection(conn, resolve_run)
    if not sel:
        raise ValueError(f"no filing_selection rows for run {resolve_run}")
    rep = Report(run_id, resolve_run, {"resolve_run": resolve_run, "scope": scope, "fy": run["fy"],
                                       "as_of": run["as_of"], "meta_synced_at": run["meta_synced_at"],
                                       "config_version": cfg.version, "config": cfg.source or "(defaults)",
                                       "since": since, "previous_monitor_run": previous})
    by_code, by_key = _names(conn, scope)

    def who(ent: str) -> tuple[str, str]:
        e = sel.get(ent, {})
        t = (e.get("cur") or e.get("prev") or {}).get("basis", {}).get("reason_basis", {}).get("target", {})
        key = t.get("key", "")
        return key, by_key.get(key) or by_code.get(t.get("edinet_code") or ent, "") or ""

    def detail(category, metric, ent="", role="", doc="", value=None, threshold=None, text=""):
        key, name = who(ent) if ent else ("", "")
        rep.details.append(Detail(category, metric, ent, key, name, role, doc or "", value, threshold, text))

    def metric(mid, label, phase, value, note=""):
        lim = cfg.thresholds.get(mid)
        hit = (lim is not None and value is not None
               and (value < lim if mid in LOWER_BOUND else value > lim))
        rep.metrics.append(Metric(mid, label, phase, value, lim, hit, note))

    docs = {(e, role): r["doc_id"] for e, rr in sel.items() for role, r in rr.items() if r.get("doc_id")}
    doc_ids = sorted(set(docs.values()))
    q = ",".join("?" for _ in doc_ids)
    fetch_rows = {r["doc_id"]: dict(r) for r in conn.execute(
        f"SELECT * FROM fetches WHERE type = 1 AND doc_id IN ({q})", doc_ids)} if doc_ids else {}
    ext = _latest(conn.execute(f"SELECT doc_id, extractor_ver, char_len, img_count, hit_count FROM extracts "
                               f"WHERE doc_id IN ({q})", doc_ids), "extractor_ver") if doc_ids else {}
    item_rows = conn.execute(f"SELECT doc_id, segmenter_ver, count(*) AS n, sum(segmentation = 'fallback') AS fb "
                             f"FROM items WHERE doc_id IN ({q}) GROUP BY doc_id, segmenter_ver", doc_ids) if doc_ids else []
    items = _latest(item_rows, "segmenter_ver")
    xflag = {r[0]: r[1] for r in conn.execute(f"SELECT doc_id, xbrl_flag FROM documents WHERE doc_id IN ({q})",
                                              doc_ids)} if doc_ids else {}

    def fetch_status(doc: str) -> str:
        r = fetch_rows.get(doc)
        if r is None:
            return "no_xbrl" if xflag.get(doc) == "0" else "none"
        return r["status"]

    def extracted(doc: str) -> bool:
        e = ext.get(doc)
        return e is not None and e["char_len"] > 0 and e["hit_count"] > 0

    # ---- M2: 突合率 (M2-1) and the reason of every unmatched entity
    pop = _population_size(conn, scope)
    denom = pop if pop is not None else len(sel)
    adopted = [e for e, rr in sel.items() if rr.get("cur", {}).get("doc_id")]
    matched = {e for e in adopted if fetch_status(docs[(e, "cur")]) == "ok" and extracted(docs[(e, "cur")])}
    metric("match_rate", "突合率（当期の有報を取得・抽出済み ÷ 母集団）", "M2", len(matched) / denom if denom else None,
           f"{len(matched)}/{denom}" + ("（母集団）" if pop is not None else "（resolve の対象 entity）"))
    metric("adopted_rate", "参考: 当期の採用版がある率（取得・抽出前）", "M2", len(adopted) / denom if denom else None,
           f"{len(adopted)}/{denom}")
    unmatched = 0
    for e in sorted(sel):
        if e in matched:
            continue
        unmatched += 1
        cur = sel[e].get("cur", {})
        doc = cur.get("doc_id")
        if not doc:
            reason = cur.get("basis", {}).get("reason") or "unknown"
            rb = cur.get("basis", {}).get("reason_basis", {})
            text = reason + (f" ({rb['unknown_cause']})" if rb.get("unknown_cause") else "")
        else:
            st = fetch_status(doc)
            reason = {"ok": "extract_missing" if doc not in ext else "empty_extract", "no_xbrl": "xbrl_missing",
                      "failed": "fetch_failed", "retry": "fetch_failed"}.get(st, "fetch_pending")
            text = f"{reason} (fetches.status={st})"
        detail("unmatched", f"unmatched.{reason}", e, "cur", doc, text=text)
    metric("unmatched", "突合外の会社（理由は詳細シート）", "M2", unmatched)

    # ---- M2: 突合・前期なしの理由別件数, unknown (M2-1, M2-3)
    cur_reasons = Counter(rr.get("cur", {}).get("basis", {}).get("reason") for rr in sel.values())
    for r in CUR_REASONS:
        metric(f"cur_reason.{r}", f"突合の理由: {r}", "M2", cur_reasons.get(r, 0))
    prev_reasons = Counter(rr.get("prev", {}).get("no_prev_reason") for e, rr in sel.items() if e in adopted)
    for r in PREV_REASONS:
        metric(f"no_prev_reason.{r}", f"前期なしの理由: {r}", "M2", prev_reasons.get(r, 0))
    n_unknown = 0
    for e in sorted(sel):
        for role in ("cur", "prev"):
            r = sel[e].get(role, {})
            reason = r.get("basis", {}).get("reason") if role == "cur" else r.get("no_prev_reason")
            if role == "prev" and e not in adopted:
                continue
            if role == "prev" and reason:
                cause = r.get("basis", {}).get("reason_basis", {}).get("unknown_cause")
                detail("no_prev", f"no_prev_reason.{reason}", e, "prev", r.get("doc_id"),
                       text=reason + (f" ({cause})" if cause else ""))
            if reason == "unknown":
                n_unknown += 1
    metric("unknown", "unknown（突合＋前期なし。全件を人が確認）", "M2", n_unknown)

    # ---- M2: flags, form_out_of_scope
    for f in FLAGS:
        ents = [e for e, rr in sel.items() if any(f in r.get("flags", []) for r in rr.values())]
        metric(f"flag.{f}", f"フラグ: {f}", "M2", len(ents))
        for e in sorted(ents):
            roles = [role for role, r in sel[e].items() if f in r.get("flags", [])]
            for role in roles:
                r = sel[e][role]
                extra = r.get("basis", {}).get("pair", {}).get("gap_days") if f == "prev_gap" else None
                detail("flag", f"flag.{f}", e, role, r.get("doc_id"), value=extra,
                       text=f"period_end={r.get('period_end')}" + (f" gap_days={extra}" if extra is not None else ""))
    oos = 0
    for e in sorted(sel):
        rb = sel[e].get("cur", {}).get("basis", {}).get("reason_basis", {})
        docs_oos = rb.get("out_of_scope_docs") or []
        if sel[e].get("cur", {}).get("basis", {}).get("reason") == "form_out_of_scope" or docs_oos:
            oos += 1
            text = "; ".join(f"{d.get('doc_id')} {d.get('docTypeCode')}/{d.get('ordinanceCode')}/{d.get('formCode')}"
                             if isinstance(d, dict) else str(d) for d in docs_oos)
            detail("form_out_of_scope", "form_out_of_scope", e, "cur", sel[e]["cur"].get("doc_id"), text=text)
    metric("form_out_of_scope", "form_out_of_scope の会社（全件を人が確認）", "M2", oos)

    # ---- fetches by status (cur/prev docs of the run, and the whole table)
    st_count = Counter(fetch_status(d) for d in doc_ids)
    for s in FETCH_STATUSES:
        metric(f"fetches.{s}", f"取得状態（この run の docID）: {s}", "M2", st_count.get(s, 0))
    for s, n in conn.execute("SELECT status, count(*) FROM fetches GROUP BY status ORDER BY status"):
        metric(f"fetches.all.{s}", f"取得状態（fetches 全体）: {s}", "M2", n)
    roles_of = defaultdict(list)
    for (e, role), d in docs.items():
        roles_of[d].append((e, role))
    n_fail = 0
    for d in doc_ids:
        st = fetch_status(d)
        if st in ("failed", "retry", "no_xbrl"):
            n_fail += st != "no_xbrl"
            r = fetch_rows.get(d, {})
            for e, role in roles_of[d]:
                detail("fetch", "no_xbrl" if st == "no_xbrl" else "fetch_failed", e, role, d,
                       value=r.get("last_http_status"),
                       text=f"status={st} error_kind={r.get('error_kind')} attempts={r.get('attempts')} "
                            f"last_error={r.get('last_error')}" if r else "xbrlFlag=0")
    metric("fetch_failed", "取得失敗（再試行の後: failed＋retry）", "M1", n_fail,
           "Content-Type エラー・ZIP破損・404 など")
    metric("no_xbrl", "xbrlFlag=0（取得しない）", "M2", st_count.get("no_xbrl", 0))
    metric("fetch_pending", "未取得（pending・fetches 行なし）", "M2", st_count.get("pending", 0) + st_count.get("none", 0))

    # ---- M1 metrics (extracts / items / matches)
    lim = cfg.entity
    ok_docs = [d for d in doc_ids if fetch_status(d) == "ok"]
    n_empty = n_missing = n_multi = n_img = 0
    for d in ok_docs:
        e = ext.get(d)
        for ent, role in roles_of[d]:
            if e is None:
                detail("m1", "extract_missing", ent, role, d, text="取得済み・未抽出")
            elif e["char_len"] == 0 or e["hit_count"] == 0:
                detail("m1", "empty_extract", ent, role, d, value=e["char_len"],
                       text=f"hit_count={e['hit_count']} char_len={e['char_len']}")
            if e is not None and e["hit_count"] > 1:
                detail("m1", "multi_hit", ent, role, d, value=e["hit_count"], text=f"hit_count={e['hit_count']}")
            if e is not None and e["img_count"] > 0 and e["char_len"] < lim.image_text_max_chars:
                detail("m1", "image_low_text", ent, role, d, value=e["char_len"], threshold=lim.image_text_max_chars,
                       text=f"img_count={e['img_count']} char_len={e['char_len']}")
        if e is None:
            n_missing += 1
        else:
            n_empty += e["char_len"] == 0 or e["hit_count"] == 0
            n_multi += e["hit_count"] > 1
            n_img += e["img_count"] > 0 and e["char_len"] < lim.image_text_max_chars
    metric("extract_missing", "取得済みで未抽出の書類", "M1", n_missing)
    metric("empty_extract", "空抽出（TextBlock なし・空文字）", "M1", n_empty)
    metric("multi_hit", "TextBlock の複数ヒット", "M1", n_multi)
    metric("image_low_text", f"画像あり かつ 文字数 < {lim.image_text_max_chars}", "M1", n_img)

    n_range = n_items_chg = n_chars_chg = n_fb = n_review = 0
    for e in sorted(sel):
        cur_d, prev_d = docs.get((e, "cur")), docs.get((e, "prev"))
        ci, pi = items.get(cur_d), items.get(prev_d)
        bad_range = False
        for role, d, it in (("cur", cur_d, ci), ("prev", prev_d, pi)):
            if it is None:
                continue
            if it["n"] <= lim.items_min or it["n"] >= lim.items_max:
                bad_range = True
                detail("m1", "items_out_of_range", e, role, d, value=it["n"],
                       text=f"items={it['n']} (目安: {lim.items_min} 以下・{lim.items_max} 以上)")
            if it["fb"]:
                detail("m1", "segment_fallback", e, role, d, value=it["n"], text="見出し検出なし（段落フォールバック）")
        n_range += bad_range
        n_fb += any(it is not None and it["fb"] for it in (ci, pi))
        if ci and pi and pi["n"] and abs(ci["n"] / pi["n"] - 1) > lim.items_prev_change:
            n_items_chg += 1
            detail("m1", "items_prev_change", e, "cur", cur_d, value=round(ci["n"] / pi["n"] - 1, 4),
                   threshold=lim.items_prev_change, text=f"items {pi['n']} -> {ci['n']}")
        ce, pe = ext.get(cur_d), ext.get(prev_d)
        if ce and pe and pe["char_len"] and abs(ce["char_len"] / pe["char_len"] - 1) > lim.chars_prev_change:
            n_chars_chg += 1
            detail("m1", "chars_prev_change", e, "cur", cur_d, value=round(ce["char_len"] / pe["char_len"] - 1, 4),
                   threshold=lim.chars_prev_change, text=f"chars {pe['char_len']} -> {ce['char_len']}")
        if cur_d and prev_d:
            m = conn.execute("SELECT matcher_ver, count(*) AS n, sum(review_flag) AS rv FROM matches "
                             "WHERE cur_doc = ? AND prev_doc = ? GROUP BY matcher_ver ORDER BY matcher_ver DESC "
                             "LIMIT 1", (cur_d, prev_d)).fetchone()
            if m and m["n"] and m["rv"] / m["n"] > lim.review_ratio_max:
                n_review += 1
                detail("m1", "review_ratio_high", e, "cur", cur_d, value=round(m["rv"] / m["n"], 4),
                       threshold=lim.review_ratio_max, text=f"要確認 {m['rv']}/{m['n']} ペア")
    metric("items_out_of_range", f"項目数 {lim.items_min} 以下・{lim.items_max} 以上の会社", "M1", n_range)
    metric("items_prev_change", f"項目数の前期比 ±{lim.items_prev_change:.0%} 超の会社", "M1", n_items_chg)
    metric("chars_prev_change", f"文字数の前期比 ±{lim.chars_prev_change:.0%} 超の会社", "M1", n_chars_chg)
    metric("segment_fallback", "段落フォールバック（見出し検出なし）の会社", "M1", n_fb)
    metric("review_ratio_high", f"対応付けの要確認比率 {lim.review_ratio_max:.0%} 超の会社", "M1", n_review)

    # ---- M2: formCodes excluded by the filing filter (120/130 in the periodEnd window, not withdrawn)
    prev_vals = {}
    if previous:
        prev_vals = {r[0]: r[1] for r in conn.execute(
            "SELECT metric, value FROM metrics WHERE run_id = ? AND entity_id = '' AND metric GLOB 'excluded_form.*'",
            (previous,))}
    sql = ("SELECT doc_type_code, ordinance_code, form_code, count(*) FROM documents "
           "WHERE doc_type_code IN ('120', '130') AND coalesce(withdrawal_status, '0') = '0'")
    args: list = []
    if window is not None:
        sql += " AND period_end BETWEEN ? AND ?"
        args += [window.period_end_from.isoformat(), window.period_end_to.isoformat()]
    excluded = {}
    for dt, od, fc, n in conn.execute(sql + " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", args):
        if not flt.is_version({"docTypeCode": dt, "ordinanceCode": od, "formCode": fc}):
            excluded[f"excluded_form.{dt}/{od}/{fc}"] = n
    for mid in sorted(set(excluded) | set(prev_vals)):
        n, before = excluded.get(mid, 0), prev_vals.get(mid)
        hit, note = False, "前回の monitor なし" if not previous else ""
        if before is not None:
            delta = n - before
            ratio = abs(delta) / max(before, 1)
            hit = abs(delta) >= cfg.excluded_form_change_min and ratio > cfg.excluded_form_change_ratio
            note = f"前回 {before:g} → {n}（{delta:+g}）"
        elif previous:
            hit = n >= cfg.excluded_form_change_min
            note = f"前回なし → {n}"
        rep.metrics.append(Metric(mid, f"絞り込みで除外した様式 {mid.split('.', 1)[1]}（docType/ordinance/form）",
                                  "M2", n, cfg.excluded_form_change_ratio, hit, note))
        if hit:
            detail("excluded_form", mid, value=n, threshold=cfg.excluded_form_change_ratio, text=note)
    metric("excluded_form_total", "絞り込みで除外した 120/130 の合計", "M2", sum(excluded.values()))

    # ---- M2: 429 / 5xx
    sql, args = "SELECT run_id, endpoint, http_status, count(*) FROM api_calls WHERE (http_status = 429 OR " \
                "http_status >= 500)", []
    if since:
        sql += " AND at >= ?"
        args.append(since)
    n429 = n5xx = 0
    for rid, ep, st, n in conn.execute(sql + " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", args):
        n429 += n if st == 429 else 0
        n5xx += n if st >= 500 else 0
        detail("api", "api_429" if st == 429 else "api_5xx", value=n, text=f"run_id={rid} endpoint={ep} http={st}")
    total = conn.execute("SELECT count(*) FROM api_calls" + (" WHERE at >= ?" if since else ""),
                         [since] if since else []).fetchone()[0]
    scope_note = f"api_calls {total} 件中" + (f"（{since} 以降）" if since else "（全期間）")
    metric("api_429", "HTTP 429 の回数", "M2", n429, scope_note)
    metric("api_5xx", "HTTP 5xx の回数", "M2", n5xx, scope_note)
    metric("api_429_5xx", "429・5xx の発生回数（0件以外は確認）", "M2", n429 + n5xx, scope_note)

    rep.metrics.append(Metric("taxonomy_business_risks", "年次: タクソノミに BusinessRisksTextBlock があるか", "M2",
                              None, note="年1回の確認。monitor では未計算"))
    rep.metrics.append(Metric("llm_reject_rate", "LLM の棄却率", "M3", None, note="M3 で追加"))
    return rep


# ---------------------------------------------------------------- persistence
def save(conn: sqlite3.Connection, rep: Report) -> None:
    """``metrics`` rows: every run-wide metric (entity_id '') + one row per entity-level detail."""
    with db.transaction(conn):
        for m in rep.metrics:
            db.upsert(conn, "metrics", {"run_id": rep.run_id, "metric": m.metric, "entity_id": "", "value": m.value,
                                        "threshold": m.threshold, "breached": int(m.breached)},
                      key=["run_id", "metric", "entity_id"])
        for d in rep.details:
            if not d.entity_id:
                continue
            db.upsert(conn, "metrics", {"run_id": rep.run_id, "metric": f"{d.metric}.{d.role}" if d.role else d.metric,
                                        "entity_id": d.entity_id, "value": 1.0 if d.value is None else d.value,
                                        "threshold": d.threshold, "breached": int(d.category in ("m1", "fetch"))},
                      key=["run_id", "metric", "entity_id"])

