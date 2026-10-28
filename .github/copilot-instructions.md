# 注記: このファイルについて

本プロジェクトの AI 連携対象は **Microsoft 365 Copilot** であり、本ファイル（GitHub Copilot 用 instructions）は M365 Copilot には読み込まれない。
M365 向けの指示は `docs/copilot-runbook.md`（統合プロンプト1本）と `docs/copilot-architecture.md` を参照。

リポを編集する開発者向けの最低ルール:
- EDINET API キーをコード・ドキュメント・Issue・チャットに書かない（`EDINET_API_KEY` または `paths.api_key_file`）。
- プライム全社取得はバッチ専用。
