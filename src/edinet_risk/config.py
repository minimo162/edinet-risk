"""Settings loader (plan §3 config.py): TOML (tomllib) + environment overrides.

Precedence (low -> high): built-in defaults < settings TOML < environment variables.

- Settings file: ``--config PATH``, else ``$EDINET_RISK_CONFIG``, else ``config/settings.toml``
  if it exists (copy ``config/settings.example.toml``). No file -> defaults only.
- Env overrides: ``EDINET_RISK__<SECTION>__<KEY>=value`` (e.g. ``EDINET_RISK__API__MIN_INTERVAL=2``),
  coerced to the type of the default. ``<SECTION>`` may be dotted path parts joined by ``__``.
- Unknown keys are errors (catches typos).
- API keys are never read from the settings file (an ``api_key`` entry is rejected); see ``api_key()``.
- Relative paths are resolved against the current directory.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .net.http import MIN_INTERVAL_FLOOR

ENV_PREFIX = "EDINET_RISK__"
DEFAULT_SETTINGS = Path("config/settings.toml")
DEFAULT_THRESHOLDS = Path("config/thresholds.toml")


class ConfigError(Exception):
    pass


def default_data_dir() -> Path:
    """%LOCALAPPDATA%\\edinet-risk on Windows (plan §3), XDG data dir elsewhere."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "edinet-risk"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "edinet-risk"


# ---------------------------------------------------------------- settings schema
@dataclass
class Paths:
    data_dir: str = ""          # "" -> default_data_dir()
    db: str = ""                # "" -> {data_dir}/edinet_risk.sqlite
    raw_dir: str = ""           # "" -> {data_dir}/raw
    out_dir: str = ""           # "" -> {data_dir}/out
    log_dir: str = ""           # "" -> {data_dir}/logs
    cache_dir: str = "cache"    # M0 response cache (doclist JSON / ZIP), reused offline
    filing_filter: str = "config/filing_filter.toml"
    thresholds: str = "config/thresholds.toml"
    monitor: str = "config/monitor.toml"     # monitor thresholds (§8-3)
    api_key_file: str = ""      # optional; must live outside the distribution directory (§6-8)

    def _under(self, value: str, default: str) -> Path:
        return Path(value) if value else self.data_path / default

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    @property
    def db_path(self) -> Path:
        return self._under(self.db, "edinet_risk.sqlite")

    @property
    def raw_path(self) -> Path:
        return self._under(self.raw_dir, "raw")

    @property
    def out_path(self) -> Path:
        return self._under(self.out_dir, "out")

    @property
    def log_path(self) -> Path:
        return self._under(self.log_dir, "logs")


@dataclass
class Api:
    base_url: str = "https://api.edinet-fsa.go.jp/api/v2"
    min_interval: float = 1.0           # seconds; floor fixed at 1.0 (§6-4)
    max_retries: int = 5
    backoff_base: float = 60.0          # 60 -> 120 -> 240 ...
    backoff_max: float = 3600.0         # cap of one backoff wait (seconds)
    fetch_max_attempts: int = 3         # fetch: transient failures are retried by later runs up to this many attempts
    timeout: float = 120.0
    doclist_quiet_start: str = "00:00"  # no documents.json calls during the nightly swap (§6-4)
    doclist_quiet_end: str = "01:00"


@dataclass
class Window:
    period_end_from: date = date(2025, 4, 1)
    period_end_to: date = date(2026, 3, 31)
    submit_from: date = date(2025, 4, 1)
    submit_to: date = date(2026, 9, 30)


def _prev_window() -> Window:
    return Window(date(2024, 4, 1), date(2025, 3, 31), date(2024, 4, 1), date(2025, 9, 30))


@dataclass
class Windows:
    current: Window = field(default_factory=Window)
    previous: Window = field(default_factory=_prev_window)


@dataclass
class Run:
    fy: str = "2026-03"
    as_of: date = date(2026, 9, 30)     # base date T (§6-5)


@dataclass
class Resolve:
    prev_gap_ok_days: int = 1           # gap == 1 -> normal
    prev_gap_max_days: int = 31         # 2..31 -> prev_gap flag; > 31 -> prev_missing_year
    period_min_days: int = 365          # outside [min, max] -> irregular
    period_max_days: int = 366
    partial_ratio: float = 0.5          # correction text < ratio * original -> correction_partial


@dataclass
class Llm:
    adapter: str = "none"               # none | replay | http (M3)
    provider: str = ""
    model: str = ""
    api_key_env: str = "LLM_API_KEY"    # name of the env var only; never the key itself


@dataclass
class Logging:
    level: str = "INFO"


@dataclass
class Publish:
    """scripts/publish_peer.ps1 (docs/publish_peer.spec.md): weekly peer outputs under canonical names."""
    dest_dir: str = ""                  # SharePoint sync folder ("" -> build publish/ only, never copied)
    work_dir: str = "publish"           # local publish/ (canonical names)
    out_dir: str = "out/publish"        # CLI outputs of the weekly run (run folders)
    targets: list[str] = field(default_factory=lambda: ["config/transport7.toml"])  # + 依頼社リスト
    doclist_days: int = 8               # doclist sync covers at least today and the previous N-1 days ...
    doclist_max_days: int = 62          # ... and back to the oldest not-final cached day, at most N days
    as_of: str = "today"                # base date T of the weekly resolve: "today" (JST) or "YYYY-MM-DD"


@dataclass
class Settings:
    paths: Paths = field(default_factory=Paths)
    api: Api = field(default_factory=Api)
    run: Run = field(default_factory=Run)
    window: Windows = field(default_factory=Windows)
    resolve: Resolve = field(default_factory=Resolve)
    llm: Llm = field(default_factory=Llm)
    logging: Logging = field(default_factory=Logging)
    publish: Publish = field(default_factory=Publish)
    source: str = ""                    # file the settings were loaded from ("" = defaults)

    def to_dict(self) -> dict:
        return _jsonable(dataclasses.asdict(self))


# ---------------------------------------------------------------- generic merge / coercion
def _coerce(value: Any, default: Any, where: str) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            return value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                pass
    elif isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    elif isinstance(default, date):
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
    elif isinstance(default, str):
        if isinstance(value, str):
            return value
    elif isinstance(default, list):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return list(value)
        if isinstance(value, str):      # env override: "a.toml;b.toml"
            return [v.strip() for v in value.split(";") if v.strip()]
    raise ConfigError(f"{where}: expected {type(default).__name__}, got {value!r}")


def _merge(obj: Any, data: Mapping, where: str) -> None:
    fields = {f.name: f for f in dataclasses.fields(obj)}
    for k, v in data.items():
        if "api_key" == k.lower() or k.lower().endswith("_api_key") or k.lower() == "subscription_key":
            raise ConfigError(f"{where}.{k}: API keys must not be written in settings files "
                              "(use the EDINET_API_KEY environment variable)")
        if k not in fields or k == "source":
            raise ConfigError(f"{where}: unknown key {k!r}")
        cur = getattr(obj, k)
        if dataclasses.is_dataclass(cur):
            if not isinstance(v, Mapping):
                raise ConfigError(f"{where}.{k}: expected a table")
            _merge(cur, v, f"{where}.{k}")
        else:
            setattr(obj, k, _coerce(v, cur, f"{where}.{k}"))


def _env_overrides(env: Mapping[str, str]) -> dict:
    out: dict = {}
    for name, value in env.items():
        if not name.upper().startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX):].split("__") if p]
        if len(parts) < 2:
            raise ConfigError(f"{name}: expected {ENV_PREFIX}<SECTION>__<KEY>")
        d = out
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = value
    return out


def _validate(s: Settings) -> None:
    if s.api.min_interval < MIN_INTERVAL_FLOOR:
        raise ConfigError(f"api.min_interval must be >= {MIN_INTERVAL_FLOOR}s (got {s.api.min_interval})")
    if s.api.max_retries < 0 or s.api.backoff_base < 0 or s.api.timeout <= 0:
        raise ConfigError("api.max_retries/backoff_base must be >= 0 and api.timeout > 0")
    if s.api.backoff_max < s.api.backoff_base or s.api.fetch_max_attempts < 1:
        raise ConfigError("api.backoff_max must be >= api.backoff_base and api.fetch_max_attempts >= 1")
    for name in ("current", "previous"):
        w: Window = getattr(s.window, name)
        if w.period_end_from > w.period_end_to or w.submit_from > w.submit_to:
            raise ConfigError(f"window.{name}: from must be <= to")
    r = s.resolve
    if not (1 <= r.prev_gap_ok_days <= r.prev_gap_max_days):
        raise ConfigError("resolve: need 1 <= prev_gap_ok_days <= prev_gap_max_days")
    if not (0 < r.period_min_days <= r.period_max_days):
        raise ConfigError("resolve: need 0 < period_min_days <= period_max_days")
    if not (0.0 < r.partial_ratio <= 1.0):
        raise ConfigError("resolve.partial_ratio must be in (0, 1]")
    if s.llm.adapter not in ("none", "replay", "http"):
        raise ConfigError(f"llm.adapter must be none|replay|http (got {s.llm.adapter!r})")
    if not s.publish.targets or s.publish.doclist_days < 1:
        raise ConfigError("publish: need at least one targets file and doclist_days >= 1")
    if s.publish.doclist_max_days < s.publish.doclist_days:
        raise ConfigError("publish: need doclist_max_days >= doclist_days")
    if s.publish.as_of != "today":
        try:
            date.fromisoformat(s.publish.as_of)
        except ValueError:
            raise ConfigError(f'publish.as_of must be "today" or "YYYY-MM-DD" (got {s.publish.as_of!r})') from None


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None


def load_settings(path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    if path is None and env.get("EDINET_RISK_CONFIG"):
        path = env["EDINET_RISK_CONFIG"]
    if path is None and DEFAULT_SETTINGS.exists():
        path = DEFAULT_SETTINGS
    if path is not None:
        _merge(s, _read_toml(Path(path)), "settings")
        s.source = str(path)
    _merge(s, _env_overrides(env), "env")
    _validate(s)
    return s


def api_key(settings: Settings | None = None, *, env: Mapping[str, str] | None = None) -> str:
    """EDINET API key from ``$EDINET_API_KEY``, else ``paths.api_key_file``. "" if neither.

    Callers must not log the return value (net.http.mask_url masks it in URLs).
    """
    env = os.environ if env is None else env
    key = env.get("EDINET_API_KEY", "").strip()
    if key or settings is None or not settings.paths.api_key_file:
        return key
    try:
        return Path(settings.paths.api_key_file).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"cannot read paths.api_key_file: {e.strerror}") from None


# ---------------------------------------------------------------- matching thresholds (§6-6)
@dataclass(frozen=True)
class Thresholds:
    version: str = "m1-initial"
    ngram_min: int = 2
    ngram_max: int = 3
    anchor_body_sim_min: float = 0.30   # anchor = exact normalised heading (1:1) AND body_sim >= this
    weight_body: float = 0.7            # S = weight_body * body_sim + weight_head * head_sim
    weight_head: float = 0.3
    accept_min: float = 0.55            # S >= accept -> matched
    review_min: float = 0.35            # review <= S < accept -> matched + review flag; below -> unmatched
    contain_min: float = 0.50           # containment ratio for split / merge detection

    def fingerprint(self) -> str:
        """Short hash of all values; part of the matcher's input hash (§6-3)."""
        blob = json.dumps(dataclasses.asdict(self), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def load_thresholds(path: str | Path = DEFAULT_THRESHOLDS) -> Thresholds:
    data = _read_toml(Path(path))
    flat: dict = {}
    for k, v in data.items():  # allow grouping in [tables]; keys must stay unique
        for kk, vv in (v.items() if isinstance(v, Mapping) else [(k, v)]):
            if kk in flat:
                raise ConfigError(f"{path}: duplicate key {kk!r}")
            flat[kk] = vv
    defaults = Thresholds()
    known = {f.name for f in dataclasses.fields(Thresholds)}
    values = {}
    for k, v in flat.items():
        if k not in known:
            raise ConfigError(f"{path}: unknown key {k!r}")
        values[k] = _coerce(v, getattr(defaults, k), f"thresholds.{k}")
    t = Thresholds(**values)
    for k in ("anchor_body_sim_min", "weight_body", "weight_head", "accept_min", "review_min", "contain_min"):
        if not 0.0 <= getattr(t, k) <= 1.0:
            raise ConfigError(f"thresholds.{k} must be in [0, 1]")
    if abs(t.weight_body + t.weight_head - 1.0) > 1e-9:
        raise ConfigError("thresholds: weight_body + weight_head must be 1")
    if not t.review_min <= t.accept_min:
        raise ConfigError("thresholds: review_min must be <= accept_min")
    if not 1 <= t.ngram_min <= t.ngram_max:
        raise ConfigError("thresholds: need 1 <= ngram_min <= ngram_max")
    return t


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, date):
        return obj.isoformat()
    return obj
