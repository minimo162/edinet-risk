# Microsoft 365 Copilot 連携アーキテクチャ

> 対象は **Microsoft 365 Copilot**。旧版の GitHub Copilot（VS Code Agent が CLI を叩く）前提は破棄。仕様は `docs/copilot-chat-specs.md`。

## 推奨: 「CLI で作る → M365 Copilot で読む」二段構成
1. **取得・抽出（管理用 Windows の CLI バッチ）**: `python -m edinet_risk` で対象社（指定社 or マツダ既定ピア `config/transport7.toml`）を取得・抽出・resolve・monitor。全社はバッチ専用。
2. **受け渡し**: `publish_peer.ps1` が CLI 出力を正規名（`risk_diff.xlsx`・`monitor.xlsx`・`社一覧.xlsx`・社別リスク本文の前期/当期 md・`週次要約.md`）にリネームし、SharePoint の公開フォルダに置く。正規名とリネーム表は `docs/publish_peer.spec.md`（CLI 自体は run 固有の名前で出す）。利用者グループには閲覧権限だけ付け、利用者自身はフォルダを開かない。
3. **分析（M365 Copilot Chat）**: 利用者は `docs/copilot-runbook.md` の統合プロンプト1本（公開ファイルのリンク入り、Teams にピン留め）を貼るだけ。Copilot がリンク先を読み、コードインタープリタで再計算して変化表・マツダ比較・要確認リストを1回で返す。

## 任意: Copilot Studio エージェント
- 指示＝runbook の統合プロンプト、知識＝上記 SharePoint フォルダ。
- EDINET 書類一覧 API を REST API ツール（API key 認証 or Key Vault 環境変数）で呼び、提出状況確認程度に限定。ZIP/XBRL 処理は CLI のまま。

## 採らない案
- M365 Copilot に Python を貼って取得から全部やらせる（サンドボックスは通信不可で EDINET に届かない）
- M365 Copilot が社内 Windows で CLI を実行する前提（その機能はない）

## 推奨運用（2026-10-29 追記）
非エンジニア向けの標準は「共有ライブラリ＋統合プロンプト1本」方式（利用者は SharePoint を開かない）。詳細は `docs/easiest-path.md` を参照。
