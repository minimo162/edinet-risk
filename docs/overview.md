# 全体像（edinet-risk × Microsoft 365 Copilot）

更新: 2026-10-29。図で見たい場合は `overview.html`。詳細は `easiest-path.md`（運用方式）／`copilot-runbook.md`（統合プロンプト）／`copilot-chat-specs.md`（M365 の仕様）／`copilot-architecture.md`（構成）／`publish_peer.spec.md`（公開ファイルの正規名）／`m1-progress.md`・`m2-progress.md`（開発記録）。

## 1. 目的（ひとことで）

> Copilot の出力はチャット表ではなく **Excel 1冊・6シート固定**（対象確認・変化表・差分詳細・分析・マツダ比較・要確認。だめなら Word）。体裁は `docs/templates/yoY_report_template.xlsx` に固定（色・列・フォント・見出し行のオートフィルタ）。

有価証券報告書「事業等のリスク」が前期から当期でどう変わったかを、社内の誰でも **M365 Copilot Chat に統合プロンプト1本を貼るだけ** で読めるようにする。1回の回答で6シートの Excel（対象確認・変化表・差分詳細・分析・マツダ比較・要確認）が返る。

> 利用者が触るのは **M365 Copilot Chat だけ**（Teams の Copilot か microsoft365.com のチャット）。SharePoint は管理者が結果ファイルを置く裏側の置き場で、利用者は開かない。Copilot はプロンプト内のリンクからファイルを読む。

- 対象は利用者が指定する社（最大10社）。指定がなければマツダ同業ピア（`config/transport7.toml`）。
- プライム全社の抽出はバッチ専用（チャットでは扱わない）。

## 2. 誰が何をするか

| 誰 | どこで | 何をする |
|---|---|---|
| 一般ユーザ | 自分の Windows PC ＋ M365 Copilot Chat | 共有 Teams チャネルのピン留め投稿から統合プロンプト1本をコピーし、Copilot Chat に貼って送る。SharePoint は開かない。キーも Python も不要（手順は次節） |
| 管理者 | 社内の管理用 Windows PC ＋ SharePoint | 週次バッチ（取得→抽出→対応付け→監視）を回し、結果を SharePoint に置く。利用者に閲覧権限を付け、リンク入りプロンプトを Teams にピン留めする。APIキーを保管する |
| M365 Copilot | Microsoft 365 側 | プロンプト内のリンクから SharePoint のファイルを（利用者の権限で）読み、外部通信できない隔離実行環境で Excel にまとめて返す（数値は公開ファイルの値を使い、再計算しない）。EDINET には届かない |
| 開発 | 開発環境（このボットのコンピュータ・社外） | CLI を作り、GitHub に push する。社内の機械ではない |

## 3. 一般ユーザの操作（具体）

> **注意: 設計上の想定手順です。** 公開フォルダ・リンク入り統合プロンプト・Teams のピン留め投稿はまだ作っていません（§5「これから」）。

1. **共有 Teams チャネルのピン留め投稿「EDINET リスク分析プロンプト」を全文コピーする。**
2. **M365 Copilot Chat を開く。** Teams の左側の「Copilot」か、ブラウザで microsoft365.com を開いてチャットを選ぶ。
3. **貼り付けて送信する。** 社を指定したいときだけ1行目の `対象:` に証券コードか社名を書く（空欄ならマツダ同業ピア）。
4. **6シートの Excel（対象確認・変化表・差分詳細・分析・マツダ比較・要確認）が1回で返ってくる。** 対象外の社は `対象確認` シートに出る。

対象外と出た社が必要なときは、共有 Teams チャネルで証券コードを書いて依頼する（次回バッチで追加）。プロンプト本文は `copilot-runbook.md`。

## 4. データの流れ（週次・MVP 完成後）

1. 管理用 Windows のタスクスケジューラが `publish_peer.ps1` を起動する（キーは環境変数かキーファイルから読む）。
2. EDINET API から書類一覧と本文を取得する（1秒間隔・並列1）。**書類一覧が取れない週、または本文の新規取得が0件でエラーがある週は公開しない**（終了コード 1。公開フォルダは前回分のまま）。
3. 抽出 → 対応付け → 監視を行い、SQLite に保存する。
4. `publish_peer.ps1` が CLI 出力（例: `risk_diff_transport7.xlsx`）を正規名 `risk_diff.xlsx`・`monitor.xlsx` にリネームし、`社一覧.xlsx`・社別 md・`週次要約.md` を生成する（`publish build`）。5種がそろい `社一覧.xlsx`・`monitor.xlsx` が当日付のときだけ（`publish check`）SharePoint の同期フォルダへ上書きコピーする（管理者のみが触る）。正規名とリネーム表は `publish_peer.spec.md` が唯一の定義。
5. 利用者が M365 Copilot Chat に統合プロンプト1本を貼る（§3）。
6. Copilot がリンク先のファイルを読み、6シートの Excel を1回で返す。
7. 対象外の社は共有 Teams チャネルで依頼 → 次回バッチで追加。

## 5. 進捗（いまどこか）

**できている**
- M0 調査: API・CSV/XBRL・様式コードを確認（API 456回）。
- M1 7社の再現: 版選択→分割→対応付け→差分→SQLite→Excel/HTML。`run --offline` は何度回しても同じ結果。
- 開発環境での取得・GitHub への push（main、2026-10-29 時点）。
- 週次公開の仕組み: `publish_peer.ps1`（PowerShell 5.1）と `publish config/targets/build/check`。`社一覧.xlsx`・社別 md・`週次要約.md` を生成し、取得ゲート（書類一覧の失敗・新規取得0件でエラー → 公開しない）と成功判定を持つ。管理者 Runbook（`admin-runbook.md`）。
- Copilot 用: 統合プロンプト（`copilot-runbook.md`、6シート・CLI の8分類→追加/削除/改訂の対応表）と見本ブック（`docs/templates/yoY_report_template.xlsx`）。

**途中**
- M2 プライム拡大: 1,543社、取得・抽出 3,358件、突合率 99.55%、monitor あり、pytest 365 passed。第3バッチまで。合格宣言はまだ（人手確認待ち: unknown 6社・late 1社 ほか）。
- `publish_peer.ps1`: 開発環境では CLI 引数・JSON キー・ゲートを静的テストで照合済み。社内 Windows ではまだ実行していない。

**これから**
- 社内 Windows 上での `publish_peer.ps1` と CLI の動作確認（これまでは社外の開発環境だけで検証）、タスクスケジューラ登録。
- SharePoint 公開フォルダと閲覧権限、リンク入り統合プロンプトの Teams ピン留め、Teams 依頼窓口。
- パイロット第1確認（Copilot がリンクだけでファイルを読めるか。`copilot-runbook.md`）。

## 6. 守る制約

- **社内 PC は Windows のみ**（利用者も管理者も）。「開発環境」は社外の開発用、「隔離実行環境」は M365 Copilot 側の機能で、どちらも社内の機械ではない。
- **APIキーは `EDINET_API_KEY` か `paths.api_key_file` だけで渡す**。設定ファイル・プロンプト・チャット・SharePoint・Teams・Git には置かない。
- 製品は **Microsoft 365 Copilot**（GitHub Copilot ではない）。Copilot は EDINET に届かないので、取得は必ず管理用 Windows のバッチで行う。

## 7. 次の MVP（この順番で）

1. `publish_peer.ps1` を社内 Windows で動かす（作成済み。`admin-runbook.md` の手順で初回実行）。
2. タスクスケジューラ＋SharePoint 同期で週次公開する。
3. 統合プロンプトに実リンクを入れ、共有 Teams チャネルにピン留めする（利用者はこれをコピーして貼るだけ）。
4. `docs/admin-runbook.md`（キー配置手順・離任前チェックリストを含む。作成済み）を実機の手順に合わせて直す。
5. 非エンジニア1名でパイロットし、つまずきを反映する。最初に「Copilot がリンクだけでファイルを読めるか」を確認する（`copilot-runbook.md`「パイロット第1確認」）。
6. （任意）Copilot Studio エージェント化。

**決めること**: 管理用 Windows の機械、SharePoint の場所と利用者グループの閲覧権限、更新頻度、依頼窓口（Teams かフォームか）、Studio 化の有無。
