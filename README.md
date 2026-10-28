# edinet-risk

東証プライム「事業等のリスク」の前年比較を、EDINET API v2 で自動化する Python ツールです。

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp config/settings.example.toml config/settings.toml
export EDINET_API_KEY=...   # キーは環境変数のみ。settings.toml に書かない
```

## 主なコマンド

```bash
python -m edinet_risk --help
python -m edinet_risk init-db
python -m edinet_risk codes sync
python -m edinet_risk population load --jpx ref/jpx/data_j_202609.xlsx --as-of-month 2026-09
python -m edinet_risk run --offline
```

進捗は `docs/m1-progress.md` / `docs/m2-progress.md`、スパイク結果は `docs/spike-findings.md` を参照。API キーをリポジトリに置かないこと。
