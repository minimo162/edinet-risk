# edinet-risk

東証プライム「事業等のリスク」の前年比較を、EDINET API v2 で自動化する Python ツールです。

## セットアップ（Windows / PowerShell）

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy config\settings.example.toml config\settings.toml
```

API キーは次のどちらかに置く（`settings.toml` にキーそのものは書かない。`api_key` の行はエラーで拒否される）:

- ユーザー環境変数 `EDINET_API_KEY`（スタートメニュー →「環境変数を編集」→ ユーザー環境変数。設定後は PowerShell を開き直す）
- キーファイル: リポの外にキーだけを1行で保存し、`settings.toml` の `[paths]` に `api_key_file = 'C:\Users\<user>\.edinet\api_key.txt'`（パスのみ）

両方あるときは環境変数が優先。詳細と週次公開の運用は [`docs/admin-runbook.md`](docs/admin-runbook.md)。

## 主なコマンド

```powershell
python -m edinet_risk --help
python -m edinet_risk init-db
python -m edinet_risk codes sync
python -m edinet_risk population load --jpx ref/jpx/data_j_202609.xlsx --as-of-month 2026-09
python -m edinet_risk run --offline
```

週次公開（SharePoint・M365 Copilot 向け）は `scripts\publish_peer.ps1`（[`docs/admin-runbook.md`](docs/admin-runbook.md)）。進捗は `docs/m1-progress.md` / `docs/m2-progress.md`、スパイク結果は `docs/spike-findings.md` を参照。API キーをリポジトリに置かないこと。
