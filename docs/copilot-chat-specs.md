# Microsoft 365 Copilot 仕様調査（edinet-risk 向け）

> 注: 以前の版は **GitHub Copilot** を前提にしていたため破棄した。本書は **Microsoft 365 Copilot (M365 Copilot)** が対象。
> 調査日: 2026-10-29 / 出典は Microsoft Learn（末尾）。Claude Code は自動レビューの分類エラーで2回起動できず、本書は直接調査で作成。

## 1. 製品の区分
| 面 | できること | edinet-risk での位置づけ |
|---|---|---|
| M365 Copilot Chat | アップロード／OneDrive・SharePoint ファイルや Web を根拠に回答。コードインタープリタ（Python）で表・グラフ作成 | **主な利用面**：CLI が出した CSV/xlsx/md を読ませて解釈・比較 |
| Agent Builder / Copilot Studio エージェント | 指示＋知識ソース＋ツール（REST API・コネクタ・MCP・Power Automate フロー・プロンプト（コードインタープリタ）） | 任意：EDINET API をカスタムコネクタで呼ぶエージェント |
| Copilot in Excel（Python in Excel 含む） | ブック内データの分析・Python セル | monitor の xlsx を開いて深掘り |
| Copilot in Word | 文書の要約・起草 | 報告書の下書き |

## 2. 実行環境（最重要）
- **M365 Copilot は利用者や管理者の PC に SSH／シェル接続して `python -m edinet_risk` を実行しない。** ローカル端末を操作する機能はない。
- コードインタープリタの Python は **Microsoft Azure 上の隔離 VM** で動く。毎回新しい VM・終了で破棄・**インバウンド／アウトバウンド通信不可**・CPU/メモリ/時間/ディスクに上限。
  → サンドボックス内から EDINET API は叩けない。pip で任意パッケージを入れる・長時間バッチも不可。
- 扱えるのは「アップロード／参照したファイル」の解析（Excel, CSV, Word, PDF, PowerPoint 等）。

## 3. 外部データ取得の手段
- Copilot Studio の **REST API ツール（OpenAPI 定義）** や **カスタムコネクタ**、**MCP サーバー**、**Power Automate フロー** でエージェントから外部 API を呼べる。
- **Graph コネクタ（Copilot コネクタ）** で外部データを M365 にインデックスし根拠にできる（要管理者）。
- ただし EDINET の書類 ZIP 取得→XBRL 抽出→年度比較を数百社規模でやるのは、応答時間・トークン・ファイル処理の制約で不向き。少数社のメタデータ取得程度が現実的。

## 4. API キー／シークレット
- **プロンプト・チャット・アップロードファイルにキーを書かない。**
- REST API ツールの「API key」認証：実行時に利用者にキー入力を求め、接続（connection）として保持。ヘッダー／クエリで送る。
- 組織管理なら Power Platform の **シークレット型環境変数 → Azure Key Vault 参照**（Copilot Studio サービスに Key Vault Secrets User、`AllowedEnvironments`/`AllowedAgents` タグ）。注意：エージェント編集者はメッセージノードで値を出せてしまう。
- コードインタープリタにはキーを渡せず、渡しても通信不可なので意味がない。
- 既存 CLI 側（管理用 Windows のバッチ）は従来どおり `EDINET_API_KEY` か `paths.api_key_file`。

## 5. プロンプトパック運用での制約
- 貼るのは統合プロンプト1本（手順・出力形式・公開ファイルのリンク）。Python 全文を貼っても実行は隔離 VM 内のみで、EDINET 取得はできない。
- ファイルサイズ・1回に参照できるファイル数・応答長に上限あり → 対象は指定社（最大10）か既定ピア（マツダ同業7社）に絞り、CLI 側で要約済み CSV/md を用意する。
- 回答は根拠ファイルを明示させ、数値は CSV からの再計算を指示する（幻覚対策）。
- テナント設定（コードインタープリタ・Web 検索・エージェント公開）は管理者次第。

## 出典
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/code-interpreter-security
- https://learn.microsoft.com/en-us/microsoft-copilot-studio/code-interpreter-for-prompts
- https://learn.microsoft.com/en-us/microsoft-copilot-studio/agent-extend-action-rest-api
- https://learn.microsoft.com/en-us/microsoft-copilot-studio/authoring-variables
- https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/kit-configure-secrets
