# 管理者 Runbook（週次公開）

対象: 管理用 **Windows** PC の担当者。利用者は M365 Copilot Chat に統合プロンプトを貼るだけ（SharePoint は開かない）。製品は Microsoft 365 Copilot。

週次公開は `scripts/publish_peer.ps1`（PowerShell 5.1 で動く）1本で回す。公開ファイルの正規名とリネーム表は `docs/publish_peer.spec.md` が唯一の定義。

## 1. 一度だけ（セットアップ）

1. 社内の管理用 Windows に Python 3.12+ と本リポ（`minimo162/edinet-risk`）を置く（例: `C:\edinet-risk`）。
   ```powershell
   cd C:\edinet-risk
   python -m venv .venv
   .venv\Scripts\pip install -e .
   ```
2. `config\settings.example.toml` を `config\settings.toml` にコピーし、`[publish]` を埋める:
   - `dest_dir` … SharePoint の同期フォルダ（OneDrive クライアントで同期した公開フォルダのローカルパス。例: `C:\Users\<user>\<社名>\EDINETリスク - 公開`）。空なら `publish\` を作るだけでコピーしない。
   - `targets` … 既定は `["config/transport7.toml"]`。依頼社は §3 の `config/requests.toml` を配列に足す。
   - `out_dir`（CLI 出力、既定 `out/publish`）・`work_dir`（正規名の `publish/`、既定 `publish`）・`doclist_days`（書類一覧の同期日数、既定 8）は既定のままで可。
3. **API キー**（§2）を設定する。
4. DB と初期データ（初回だけ。書類一覧の全期間同期は時間がかかる）:
   ```powershell
   .venv\Scripts\python -m edinet_risk init-db
   .venv\Scripts\python -m edinet_risk codes sync
   .venv\Scripts\python -m edinet_risk doclist sync --rescan-month --window both
   ```
5. 公開フォルダに利用者グループの **閲覧権限** を付ける（Copilot は利用者本人の権限で読む）。
6. `docs/templates/yoY_report_template.xlsx` がリポにあれば、`publish build` が `publish\templates\` へコピーする（Copilot の見本ブック。成功判定の5種には含めない）。
7. `-DryRun`（§4）で、実行するコマンドとコピー先を確認する。続けて手動で1回本実行し、§6 の確認をする。
8. タスクスケジューラに登録する（§5）。
9. `docs/copilot-runbook.md` の統合プロンプトのプレースホルダを実リンクに差し替え、共有 Teams チャネルに **ピン留め**。古い文面は残さない。

## 2. API キー（2通りだけ）

キーは次のどちらかにだけ置く。`settings.toml`（`api_key` の行はエラーで拒否される）・`publish_peer.ps1`・タスクの引数・公開フォルダ・プロンプト・Teams・Git には書かない。

- **A. 環境変数 `EDINET_API_KEY`**: タスクを実行するユーザーの「ユーザー環境変数」に設定する（スタートメニュー →「環境変数を編集」→ ユーザー環境変数 → 新規）。コマンドラインに打つと履歴に残るので、GUI で入れる。設定後はタスクスケジューラ・PowerShell を開き直す。
- **B. キーファイル `paths.api_key_file`**: リポの外（例: `C:\Users\<user>\.edinet\api_key.txt`）にキーだけを1行で保存し、`settings.toml` の `[paths]` に `api_key_file = 'C:\Users\<user>\.edinet\api_key.txt'` と書く（パスのみ）。ファイルはそのユーザーだけが読めるようにする:
  ```powershell
  icacls C:\Users\<user>\.edinet\api_key.txt /inheritance:r /grant:r "${env:USERNAME}:R"
  ```

両方あるときは環境変数が優先。`publish_peer.ps1` はキーを読まず、`python -m edinet_risk publish config` が返す「どちらから読めるか」（`env` / `file` / `none`）だけを見る。`none` なら何もせずに止まる。

## 3. 依頼社の追加

1. `config\requests.example.toml` を `config\requests.toml` にコピーし、`[[filer]]` を足す（`secCode`（証券コード4桁）か `edinetCode` のどちらか。`label` は社別 md の名前になる）。
2. `settings.toml` の `[publish]` を `targets = ["config/transport7.toml", "config/requests.toml"]` にする。
3. `-DryRun` で確認し、次回の定期実行を待つか手動で実行する。`secCode` が見つからないときは `codes sync` を流し直す。

## 4. 手動実行・DryRun

```powershell
cd C:\edinet-risk
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_peer.ps1 -DryRun   # コマンドとコピー先を表示するだけ
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_peer.ps1           # 本実行
```

オプション: `-Config <toml>`（既定 `config\settings.toml`）、`-Dest <フォルダ>`（`publish.dest_dir` の上書き）、`-Python <python.exe>`（既定 `.venv\Scripts\python.exe`）、`-SkipDoclist`（書類一覧の同期を省く）、`-NoSync`（`publish\` の作成と点検まで。同期フォルダへコピーしない）。

`publish_peer.ps1` が行うこと（既存 CLI をこの順に実行）:

1. `publish targets`: transport7 ＋依頼社リストを `out\publish\targets.toml` にまとめる
2. `doclist sync --from <今日-7日> --to <今日>`
3. `resolve`（取得対象を決める）→ `fetch --include-pending` → `extract` → `resolve`（本文取得後。公開に使う）
4. `compare` → `report`（`risk_diff_transport7.xlsx`）→ `monitor`
5. `publish build`: `社一覧.xlsx`・`社別\<証券コード>_<社名>.md`・`週次要約.md` を `publish\` に生成。`risk_diff_transport7.xlsx` → `risk_diff.xlsx`、`monitor_<run-id>\monitor.xlsx` → `monitor.xlsx` にリネームしてコピー
6. `publish check`: 5種がそろい、`社一覧.xlsx` と `monitor.xlsx` が当日付か。だめなら **同期しない**（前回分が残る）
7. 成功時のみ `dest_dir` へ同じ相対パスで **上書きコピー**（削除しない。削除→新規作成だとリンクが変わる）

## 5. タスクスケジューラ登録

毎週1回（例: 月曜 7:30）。書類一覧 API を呼ばない 0:00〜1:00 は避ける。OneDrive がアップロードするのはユーザーがサインインしている間なので、そのユーザーで「ユーザーがログオンしているときのみ実行する」にするのが簡単。

GUI（タスクスケジューラ →「タスクの作成」）:

| タブ | 設定 |
|---|---|
| 全般 | 名前 `edinet-risk publish_peer`、実行ユーザー = §2 のキーを設定したユーザー、「ユーザーがログオンしているときのみ実行する」 |
| トリガー | 毎週・月曜・7:30 |
| 操作 | プログラム `powershell.exe`、引数 `-NoProfile -ExecutionPolicy Bypass -File "C:\edinet-risk\scripts\publish_peer.ps1"`、開始 `C:\edinet-risk` |
| 設定 | 「タスクを停止するまでの時間」= 4時間、「既に実行中の場合」= 新しいインスタンスを開始しない |

コマンドで登録する場合（キーは引数に入れない）:

```powershell
schtasks /Create /TN "edinet-risk publish_peer" /SC WEEKLY /D MON /ST 07:30 /IT /F `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\edinet-risk\scripts\publish_peer.ps1"
schtasks /Run /TN "edinet-risk publish_peer"     # 登録直後に1回試す
```

## 6. 結果の確認（毎週）

- 終了コード 0 = 成功（タスクスケジューラの「前回の実行結果」が 0x0）、1 = 失敗（同期していない）。
- ログ: `out\publish\logs\publish_peer_<日時>.log`（キーは出ない）。
- 「完了（警告 n 件…）」は続行できた警告（取得の一時失敗、前期なしで比較できない社など）。`monitor.xlsx` の要確認を見る。
- 公開フォルダの `社一覧.xlsx`・`monitor.xlsx` の更新日が当日になっていること。

## 7. 失敗したとき

| 症状 | 見ること | 対処 |
|---|---|---|
| 「API キーがありません」 | §2 の環境変数／`api_key_file` | 実行ユーザーの環境変数を確認、タスクスケジューラを開き直す。Git に載せない |
| fetch／doclist が終了コード 4 | キーの有効性（401/403） | キーを再発行して差し替え |
| `publish check` NOT OK | ログの `missing:` / `not updated today:` | 欠けた CLI 出力を確認。同期は走っていない（前回分が残る） |
| SharePoint に古いまま | `dest_dir`・OneDrive の同期状態 | パス誤り or 同期停止（サインイン切れ） |
| 利用者「ファイル開けない」 | 閲覧権限・リンク切れ | 権限付与、ピン留め更新。正規名を変えていないか |

## 8. 制限（MVP）

- 年度は `settings.toml` の `[run]`／`[window.*]`（既定 FY 2026-03、3月決算）。年度が替わったら更新する。
- 対象から外した社の `社別\*.md` は同期フォルダに残る（上書きのみで削除しないため）。不要なら手で消す。
- `publish\` の正規名ファイルは毎回作り直す（それ以外のファイルには触れない）。

## 9. 離任前チェックリスト

- [ ] 後任に管理用 Windows・タスク・`dest_dir`・依頼窓口を共有
- [ ] キーを後任へ移し（§2）、自分のコピーを消す
- [ ] SharePoint 管理者権限が後任にある
- [ ] ピン留めプロンプトが最新（テンプレリンク含む）
- [ ] 本 Runbook と `docs/publish_peer.spec.md` の場所を伝える

## 10. 関連

- 正規名: `docs/publish_peer.spec.md`
- 利用者プロンプト（Excel 体裁固定）: `docs/copilot-runbook.md` / `docs/templates/yoY_report_template.xlsx`
- 全体像: `docs/overview.md`
