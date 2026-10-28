from datetime import date
from pathlib import Path

import pytest

from edinet_risk.config import ConfigError, Thresholds, api_key, load_settings, load_thresholds

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text, name="s.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config/settings.toml here
    s = load_settings(env={})
    assert s.source == "" and s.api.min_interval == 1.0 and s.run.as_of == date(2026, 9, 30)
    assert s.paths.db_path == s.paths.data_path / "edinet_risk.sqlite"


def test_example_file_loads_and_matches_defaults():
    s = load_settings(ROOT / "config/settings.example.toml", env={})
    defaults = s.to_dict()
    defaults.pop("source")
    from edinet_risk.config import Settings
    expected = Settings().to_dict()
    expected.pop("source")
    assert defaults == expected  # the example documents the real defaults


def test_file_then_env_precedence(tmp_path):
    p = write(tmp_path, '[api]\nmin_interval = 3.0\nmax_retries = 2\n[paths]\ndata_dir = "d"\n')
    s = load_settings(p, env={"EDINET_RISK__API__MIN_INTERVAL": "5", "EDINET_RISK__WINDOW__CURRENT__SUBMIT_TO": "2026-10-31"})
    assert s.api.min_interval == 5.0 and s.api.max_retries == 2
    assert s.window.current.submit_to == date(2026, 10, 31)
    assert s.paths.db_path == Path("d/edinet_risk.sqlite") and s.paths.raw_path == Path("d/raw")


def test_config_path_from_env(tmp_path):
    p = write(tmp_path, '[logging]\nlevel = "DEBUG"\n')
    assert load_settings(env={"EDINET_RISK_CONFIG": str(p)}).logging.level == "DEBUG"


@pytest.mark.parametrize("text,match", [
    ('[api]\nmin_intervl = 2\n', "unknown key"),
    ('[nope]\na = 1\n', "unknown key"),
    ('[api]\nmin_interval = 0.5\n', ">= 1.0"),
    ('[api]\nmax_retries = "many"\n', "expected int"),
    ('[api]\napi_key = "SECRET"\n', "must not be written"),
    ('edinet_api_key = "SECRET"\n', "must not be written"),
    ('[resolve]\nprev_gap_max_days = 0\n', "prev_gap"),
    ('[window.current]\nsubmit_from = 2026-10-01\n', "from must be <= to"),
    ('[llm]\nadapter = "gpt"\n', "adapter"),
    ('[api\n', "invalid TOML"),
])
def test_invalid_settings(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match) as ei:
        load_settings(write(tmp_path, text), env={})
    assert "SECRET" not in str(ei.value)


def test_missing_file_and_bad_env(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", env={})
    with pytest.raises(ConfigError, match="SECTION"):
        load_settings(write(tmp_path, ""), env={"EDINET_RISK__LEVEL": "x"})
    with pytest.raises(ConfigError, match="expected float"):
        load_settings(write(tmp_path, ""), env={"EDINET_RISK__API__TIMEOUT": "soon"})


def test_api_key_env_then_file(tmp_path):
    kf = write(tmp_path, "FILEKEY\n", "key.txt")
    s = load_settings(write(tmp_path, f'[paths]\napi_key_file = "{kf.as_posix()}"\n'), env={})
    assert api_key(s, env={"EDINET_API_KEY": "ENVKEY"}) == "ENVKEY"
    assert api_key(s, env={}) == "FILEKEY"
    assert api_key(None, env={}) == ""
    assert "FILEKEY" not in repr(s) and "FILEKEY" not in str(s.to_dict())


def test_thresholds_file_matches_plan():
    t = load_thresholds(ROOT / "config/thresholds.toml")
    assert t == Thresholds()  # §6-6 initial values
    assert (t.anchor_body_sim_min, t.weight_body, t.weight_head) == (0.3, 0.7, 0.3)
    assert (t.accept_min, t.review_min, t.contain_min) == (0.55, 0.35, 0.5)
    assert (t.ngram_min, t.ngram_max) == (2, 3)


def test_thresholds_fingerprint_changes_with_values():
    assert Thresholds().fingerprint() == Thresholds().fingerprint()
    assert Thresholds(accept_min=0.6).fingerprint() != Thresholds().fingerprint()


@pytest.mark.parametrize("text,match", [
    ("weight_body = 0.8\n", "must be 1"),
    ("review_min = 0.6\n", "review_min"),
    ("accept_min = 1.5\n", r"\[0, 1\]"),
    ("ngram_min = 4\n", "ngram"),
    ("foo = 1\n", "unknown key"),
    ("[a]\naccept_min = 0.5\n[b]\naccept_min = 0.5\n", "duplicate"),
])
def test_invalid_thresholds(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        load_thresholds(write(tmp_path, text))
