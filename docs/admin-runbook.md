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
   - `dest_dir` … SharePoint の同期フォルダ（OneDrive クライアントで同期した公開フォルダのローカルパス。例: `C:\Users\<user>\<社名>\EDINETリスク - 公開`）。**必須**: 空のまま（または `settings.toml` がないまま）本実行すると、何もせず終了コード 1 で止まる。`publish\` を作るだけなら `-NoSync` を付ける。
   - **同期フォルダを使えない環境（当面の社内運用）**: `dest_dir` は空のままにし、実行には常に `-NoSync` を付ける。`publish\` の中身は管理者が SharePoint へ手動でアップロードする（§12）。
   - `targets` … 既定は `["config/transport7.toml"]`。依頼社は §3 の `config/requests.toml` を配列に足す。
   - `out_dir`（CLI 出力、既定 `out/publish`）・`work_dir`（正規名の `publish/`、既定 `publish`）・`doclist_days`（書類一覧の最低同期日数、既定 8）・`doclist_max_days`（休んだ週を取り戻すときのさかのぼり上限、既定 62）は既定のままで可。
   - `as_of` … 基準日T。既定 `"today"`（実行日。週次はこのまま）。`"2026-09-30"` のように固定すると毎週同じ T で解決する（再現・検証用。週次公開では使わない）。
3. **API キー**（§2）を設定する。
4. DB と初期データ（初回だけ。書類一覧の全期間同期は時間がかかる）:
   ```powershell
   .venv\Scripts\python -m edinet_risk init-db
   .venv\Scripts\python -m edinet_risk codes sync
   .venv\Scripts\python -m edinet_risk doclist sync --rescan-month --window both
   ```
5. 公開フォルダに利用者グループの **閲覧権限** を付ける（Copilot は利用者本人の権限で読む）。
6. `docs/templates/yoY_report_template.xlsx` がリポにあれば、`publish build` が `publish\templates\` へコピーする（Copilot の見本ブック。成功判定の5種には含めない）。
7. `-DryRun`（§4）で、実行するコマンドとコピー先を確認する。続けて手動で1回本実行し、§6 の確認をする（同期フォルダなしなら `-NoSync` で実行し、§12 の手順で初回アップロードする）。
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

1. `config\requests.example.toml` を `config\requests.toml` にコピーし、`[[filer]]` を足す（`secCode`（証券コード4桁）か `edinetCode` のどちらか。`label` は社別 md の名前になる）。`社一覧.xlsx` の「区分」は、`targets` の1つ目（transport7）の社が `ピア`、2つ目以降（依頼社リスト）の社が `依頼`。個別に変えるときは `[[filer]]` に `group = "ピア"` か `group = "依頼"` を書く。Copilot は「対象:」が空欄なら区分=ピアの社だけを使う。
2. `settings.toml` の `[publish]` を `targets = ["config/transport7.toml", "config/requests.toml"]` にする。
3. `-DryRun` で確認し、次回の定期実行を待つか手動で実行する。`secCode` が見つからないときは `codes sync` を流し直す。

## 4. 手動実行・DryRun

```powershell
cd C:\edinet-risk
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_peer.ps1 -DryRun   # コマンドとコピー先を表示するだけ
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_peer.ps1           # 本実行
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_peer.ps1 -NoSync   # 同期フォルダなし: publish\ の作成と点検まで（アップロードは §12）
```

オプション: `-Config <toml>`（既定 `config\settings.toml`）、`-Dest <フォルダ>`（`publish.dest_dir` の上書き）、`-Python <python.exe>`（既定 `.venv\Scripts\python.exe`）、`-SkipDoclist`（書類一覧の同期を省く）、`-NoSync`（`publish\` の作成と点検まで。同期フォルダへコピーしない。`dest_dir` が空でもよい）、`-FailOnThreshold`（monitor の閾値超過があれば、公開したうえで終了コード 3。既定オフ）。`dest_dir` を `work_dir`（`publish\`）と同じ・その中・その親にすると終了コード 1。`-DryRun`・`-NoSync` なしで `settings.toml` がない、または `dest_dir`（`-Dest`）が空なら終了コード 1。

`publish_peer.ps1` が行うこと（既存 CLI をこの順に実行）:

1. `publish targets`: transport7 ＋依頼社リストを `out\publish\targets.toml` にまとめる
2. `doclist sync --from <doclist_from> --to <今日>`（`doclist_from` は `publish config` が決める: 前回の同期で確定した最後の日の翌日。休んだ週があれば自動でさかのぼる。最低 `doclist_days`・最大 `doclist_max_days` 日）
3. `resolve --as-of <基準日T>`（取得対象を決める）→ `fetch --include-pending` → `extract` → `resolve --as-of <基準日T>`（本文取得後。公開に使う）。基準日T は `publish.as_of`（既定 = 実行日）。`社一覧.xlsx` の「基準日T」列と `週次要約.md` の見出しに出る
4. `compare`（本文は `cache\docs\` の type=1 ZIP から読む）→ `report`（`risk_diff_transport7.xlsx`）→ `monitor`
5. `publish build`: `社一覧.xlsx`・`社別\<証券コード>_<社名>.md`・`週次要約.md` を `publish\` に生成し、`docs\templates\*.xlsx` を `publish\templates\` にコピー。`risk_diff_transport7.xlsx` → `risk_diff.xlsx`、`monitor_<run-id>\monitor.xlsx` → `monitor.xlsx` にリネームしてコピー
6. `publish check`: 5種がそろい、`社一覧.xlsx` と `monitor.xlsx` が当日付か。だめなら **同期しない**（前回分が残る）
7. 成功時のみ `dest_dir` へ同じ相対パスで **上書きコピー**（削除→新規作成だとリンクが変わる）。`社一覧.xlsx` を **最後** にコピーする（完了の印）。Excel で開かれているなどロック中のファイルは 10 秒おきに最大5回やり直す。同期フォルダの `社別\*.md` のうち今回の対象にないもの（対象外にした社・`label` を変えた社の古い名前）だけは消す。`-NoSync` のときはこの手順を省き、管理者が §12 の手順で同じ規則どおりに手動アップロードする

## 5. タスクスケジューラ登録

毎週1回（例: 月曜 7:30）。書類一覧 API を呼ばない 0:00〜1:00 は避ける。OneDrive がアップロードするのはユーザーがサインインしている間なので、そのユーザーで「ユーザーがログオンしているときのみ実行する」にするのが簡単。

**同期フォルダなし（手動アップロード運用）**: 下の引数の末尾に `-NoSync` を付ける（付けないと `dest_dir` が空なので終了コード 1 で止まる）。タスクは `publish\` の作成と点検までで、アップロードは同じ日に管理者が §12 の手順で行う（`publish check` は当日付かを見るため、日をまたぐ前に上げる）。

GUI（タスクスケジューラ →「タスクの作成」）:

| タブ | 設定 |
|---|---|
| 全般 | 名前 `edinet-risk publish_peer`、実行ユーザー = §2 のキーを設定したユーザー、「ユーザーがログオンしているときのみ実行する」 |
| トリガー | 毎週・月曜・7:30 |
| 操作 | プログラム `powershell.exe`、引数 `-NoProfile -ExecutionPolicy Bypass -File "C:\edinet-risk\scripts\publish_peer.ps1"`（同期フォルダなしは末尾に ` -NoSync`）、開始 `C:\edinet-risk` |
| 設定 | **「スケジュールされた時刻にタスクを開始できなかった場合、すぐにタスクを実行する」にチェック**（StartWhenAvailable。PC が止まっていた週も、次にログオンしたときに実行される）、「タスクを停止するまでの時間」= 4時間、「既に実行中の場合」= 新しいインスタンスを開始しない |

コマンドで登録する場合（キーは引数に入れない。`schtasks` では StartWhenAvailable を付けられないので PowerShell で登録する）:

```powershell
$act = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory "C:\edinet-risk" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\edinet-risk\scripts\publish_peer.ps1"   # 同期フォルダなしは末尾に -NoSync を足す
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 07:30
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "edinet-risk publish_peer" -Action $act -Trigger $trg -Settings $set -Force   # 実行中のユーザー・ログオン時のみ
Start-ScheduledTask -TaskName "edinet-risk publish_peer"     # 登録直後に1回試す
```

### 休んだ週の取りこぼし（リスクと対処）

- **リスク**: 書類一覧（doclist）はその日に提出された書類の一覧で、取得しなかった日の提出は DB に入らない。PC の停止・失敗で1週休むと、その週の提出（訂正報告書を含む）を取りこぼし、次の週の公開に出ないおそれがある。
- **自動の対処**: `publish config` がキャッシュ（`cache\doclist\<日付>.json`）を見て、確定していない日（未取得、または当日に取得した日）を直近の確定日までさかのぼって同期範囲に入れる（上限 `doclist_max_days`、既定 62 日）。確定済みの日は再取得しないので、休んだ日数分の呼び出しだけ増える。StartWhenAvailable と組み合わせれば、PC が戻った日に取り戻せる。
- **上限を超えたとき**: ログに「書類一覧は <日付> から同期します（publish.doclist_max_days=…）」の警告が出る。それより前の抜けは手動で埋める: `.venv\Scripts\python -m edinet_risk doclist sync --from <抜けの初日> --to <上限の前日>`。長く止めていたときは `doclist sync --rescan-month --window current` でも可（時間がかかる）。
- 毎週、ログの「doclist sync range: days=… (<from>..<to>)」の範囲が前回実行日以前から始まっていることを見る（§6）。

## 6. 結果の確認（毎週）

- 終了コード 0 = 成功（タスクスケジューラの「前回の実行結果」が 0x0）、1 = 失敗（同期していない。コピー途中の失敗なら一部だけ更新され、`社一覧.xlsx` は前回のまま）、3 = `-FailOnThreshold` 指定時の閾値超過（公開は済み）。
- ログ: `out\publish\logs\publish_peer_<日時>.log`（キーは出ない）。
- 「完了（警告 n 件…）」は続行できた警告（終了コード 2: fetch の一部失敗（新規取得が1件以上ある場合）、compare の前期なしで比較できない社、report の照合で全文一致しない社。monitor の閾値超過 `monitor breached=n`）。`monitor.xlsx` の要確認を見る。
- CLI の **終了コード 2 の意味**: `compare`・`report` は「一部の社だけ比較できない／照合が全文一致しない」と「入力がない」のどちらも 2。ps1 は集計行の有無で分け、ログに「終了コード 2 = 一部の社が比較できない…（続行）」か「集計行がありません（入力なし）」（失敗、終了コード 1）と出す。
- doclist sync の失敗（終了コード 2 を含む）と、fetch の「新規取得 0 件でエラーあり」は失敗（終了コード 1、同期しない）。古いデータを当日分として公開しないため。
- 公開フォルダの `社一覧.xlsx`・`monitor.xlsx` の更新日が当日、`社一覧.xlsx` の「基準日T」が実行日になっていること（手動アップロード運用では、§12 のアップロード後に SharePoint の「更新日時」で見る）。
- `-NoSync` のときは、ログに「同期フォルダへのコピーは省略」と出て公開フォルダは変わらない。最後の行が「完了。」か「完了（警告…）」なら §12 の手順でアップロードする。
- 「警告: 基準日T … が当期の提出期間の終わり … を過ぎています」が出たら、年度の切り替え（`settings.toml` の `[run]` `fy`・`[window]`）を検討する。続行はする。
- 「settings が見つかりません」「publish.dest_dir が空です」は設定の誤り（終了コード 1、何も実行していない）。§1 の 2. を見直す。

### 失敗に気づく（通知システムは作っていない）

- **タスクの終了コード**: タスクスケジューラの「前回の実行結果」が 0x0 以外（0x1 = 失敗、0x3 = `-FailOnThreshold` の閾値超過）なら見る。週1回、実行日に確認する。
- **ログ**: `out\publish\logs\` の最新ログの最後の行が「完了。」か「完了（警告…）」か「失敗: …」か。`publish config`／`publish build` の失敗理由は `stderr:` の行に出る。
- **更新日が古い**: 公開フォルダの `社一覧.xlsx` の更新日が1週間より前なら、タスクが止まっているか失敗が続いている（PC 停止・サインイン切れを含む）。手動アップロード運用では、アップロードし忘れも疑う。
- **アップロード途中**: 手動アップロード運用で `社一覧.xlsx` だけ更新日が古く、ほかのファイルが当日なら、アップロードが途中で止まっている（新旧混在）。§12 の手順を最後までやり直す。
- **コピー途中の失敗**: ログに「コピー済み n/m 件」「新旧が混在しています」が出たら、ロックしている人に閉じてもらい再実行する。
- （任意）Teams／メールに知らせたいときは、タスクスケジューラのイベント（タスクの失敗）や Power Automate で共有チャネル宛てに通知する（個人宛てにしない）。本リポには通知の仕組みは入れていない。

## 7. 失敗したとき

| 症状 | 見ること | 対処 |
|---|---|---|
| 「API キーがありません」 | §2 の環境変数／`api_key_file` | 実行ユーザーの環境変数を確認、タスクスケジューラを開き直す。Git に載せない |
| fetch／doclist が終了コード 4 | キーの有効性（401/403） | キーを再発行して差し替え |
| 「doclist が終了コード 2 で失敗」 | ログの `failed <日付>` / `not fetched=` | 一時障害なら時間をおいて再実行（取得済みの日はキャッシュから読むので速い） |
| 「fetch: 新規取得 0 件でエラーがあります」 | ログの `failed` / `retry` の書類 | 再実行。続くなら EDINET 側の障害か書類の取下げを確認 |
| `publish check` NOT OK | ログの `missing:` / `not updated today:` | 欠けた CLI 出力を確認。同期は走っていない（前回分が残る） |
| 「compare: 終了コード 2 で集計行がありません」 | resolve の出力（`resolve_<run-id>\resolve_transport7.json`） | resolve が対象社を出していない。ログの resolve の行を確認 |
| 「コピーできません」「新旧が混在しています」 | ログの「コピー済み」一覧 | 同期フォルダのファイルを開いている人に閉じてもらい再実行（`社一覧.xlsx` が前回のままなら未完了） |
| 「dest_dir と work_dir が同じか、一方が他方の中」 | `[publish]` の `dest_dir`・`work_dir` | 別のフォルダにする |
| SharePoint に古いまま | `dest_dir`・OneDrive の同期状態 | パス誤り or 同期停止（サインイン切れ）。手動アップロード運用ならアップロード漏れ（§12 をやり直す） |
| 「publish.dest_dir が空です」（手動アップロード運用） | タスクの引数 | `-NoSync` が付いていない。§5 の引数に足す |
| 手動アップロード後にリンクが切れた／同名ファイルが2つある | SharePoint のファイル名（`risk_diff(1).xlsx` など） | 「両方保持」や削除→上げ直しをした。余分なファイルを消し、正規名のファイルを「置き換え」で上げ直す。リンクが変わっていたら統合プロンプトのリンクを差し替えてピン留めを更新（§12） |
| 利用者「ファイル開けない」 | 閲覧権限・リンク切れ | 権限付与、ピン留め更新。正規名を変えていないか |

## 8. 制限（MVP）

- 年度は `settings.toml` の `[run]`／`[window.*]`（既定 FY 2026-03、3月決算）。年度が替わったら更新する。
- 対象から外した社・`label` を変えた社の古い `社別\*.md` は、同期フォルダから自動で消す（`社別\*.md` だけ。ほかのファイルは消さない）。手動アップロード運用では自動で消えないので、管理者が SharePoint で消す（§12）。
- `publish\` の正規名ファイルは毎回作り直す（それ以外のファイルには触れない）。

## 9. 離任前チェックリスト

- [ ] 後任に管理用 Windows・タスク・`dest_dir`・依頼窓口を共有（手動アップロード運用なら、`-NoSync` 付きのタスクと §12 の毎週の手順・SharePoint 公開フォルダの場所を一緒に1回やって引き継ぐ）
- [ ] 手動アップロード運用なら、後任が公開フォルダの編集権限を持っている
- [ ] キーを後任へ移し（§2）、自分のコピーを消す
- [ ] SharePoint 管理者権限が後任にある
- [ ] ピン留めプロンプトが最新（テンプレリンク含む）
- [ ] 本 Runbook と `docs/publish_peer.spec.md` の場所を伝える

## 10. 関連

- 正規名: `docs/publish_peer.spec.md`
- 利用者プロンプト（Excel 体裁固定）: `docs/copilot-runbook.md` / `docs/templates/yoY_report_template.xlsx`
- 全体像: `docs/overview.md`

## 11. 確認状況（2026-10-29 時点）

済み（社外の開発環境、ネットワークなし `--offline`、DB はコピーを使用）:
- ps1 と同じ順・同じ引数で CLI を実行（`publish targets` → `resolve` → `fetch --dry-run` → `extract` → `resolve` → `compare` → `report` → `monitor` → `publish build` → リネームコピー → `publish check`）。輸送7社で `publish check` が ok、`publish\templates\yoY_report_template.xlsx`（`差分詳細`・`分析` シート込み）のコピーを確認。
- ps1 の静的テスト（`tests/test_peer_publish.py`）: UTF-8 BOM・PowerShell 5.1 で使えない構文なし・各サブコマンドに渡すオプション（`--config` を含む）が CLI に実在・ps1 が読む JSON キーが `publish config`／`publish build` の出力に実在。

未（この環境ではできないため実施していない）:
- [ ] 社内の管理用 Windows・PowerShell 5.1 での `publish_peer.ps1` の実行（`-DryRun` を含め、ps1 そのものはまだ一度も実行していない）
- [ ] タスクスケジューラ登録と定期実行（§5）
- [ ] SharePoint の実 URL の確定と、手動アップロード（§12、`-NoSync`）での公開。「置き換え」後も統合プロンプトの同じリンクで新しいファイルが読めるか（同期フォルダ（`dest_dir`）へのコピーは、同期できる環境になったときに確認）
- [ ] 統合プロンプトのリンクを実 URL に差し替えて M365 Copilot 実機で読めるかの確認（`copilot-runbook.md`「パイロット第1確認」）
- [ ] 本番キーでの `doclist sync`／`fetch`（週次差分の取得）

## 12. 手動アップロード（同期フォルダなし）

社内で SharePoint をローカル同期できないため、当面はこの運用にする。スクリプトは変えず、ps1 が同期フォルダへ行うこと（§4 の 7.）を管理者が SharePoint の画面で同じ規則どおりに行う: **上書き（置き換え）・古い社別 md だけ削除・`社一覧.xlsx` は最後**。

### 設定
- `settings.toml` の `[publish]` `dest_dir` は **空のまま**。
- 実行は **常に `-NoSync`**（手動実行も §5 のタスクも）。`publish\`（例: `C:\edinet-risk\publish`）の作成と `publish check` までを行い、公開フォルダには触れない。
- アップロード元は `publish\` の **中身**。アップロード先は SharePoint の公開フォルダ（統合プロンプトのリンク先。公開フォルダの中に `社別`・`templates` フォルダがある）。

### 毎週の手順
1. `-NoSync` で実行する（タスク、または §4 のコマンド）。
2. ログ（`out\publish\logs\` の最新）の最後の行が「完了。」か「完了（警告…）」であることを確認する。「失敗: …」なら §7 で直して再実行し、アップロードしない（公開フォルダは前回分のまま）。
3. ログの `publish check … ok` を確認する。念のため、アップロードの直前に手で流してもよい:
   ```powershell
   .venv\Scripts\python -m edinet_risk publish check --publish-dir publish
   ```
   `NOT OK` ならアップロードしない。`publish check` は当日付かを見るので、実行と同じ日にアップロードする。
4. `社一覧.xlsx` **以外** をアップロードし、同名ファイルがあると聞かれたら **「置き換え」** を選ぶ:
   - 公開フォルダ直下へ `risk_diff.xlsx`・`monitor.xlsx`・`週次要約.md`
   - `社別` フォルダへ `publish\社別\*.md`（すべて）
   - `templates` フォルダへ `publish\templates\*.xlsx`（あるとき）
5. 公開フォルダの `社別` にあって `publish\社別` にない md（対象から外した社・`label` を変えた社の古い名前）を削除する。`社別\*.md` 以外は消さない。
6. 最後に `社一覧.xlsx` を公開フォルダ直下へ「置き換え」でアップロードする（完了の印）。
7. SharePoint の「更新日時」列で、上げたファイルがすべて当日になっていることを確認する。`社一覧.xlsx` だけ古いなら途中で止まっている（4.〜6. をやり直す）。

### やってはいけない
- 同名ファイルの確認で **「両方保持」** を選ぶ（`risk_diff(1).xlsx` のような別ファイルができ、Copilot が古い方を読む）。
- 古いファイルを **削除してから上げ直す**（リンクが変わり、ピン留めした統合プロンプトのリンクが切れる）。
- 公開フォルダのファイルの **名前変更・移動**（同じくリンクが切れる。正規名は `publish_peer.spec.md` §1）。
- `out\publish\` の CLI 出力（`risk_diff_transport7.xlsx` など）を上げる。上げるのは `publish\` の正規名ファイルだけ。
- `publish` **フォルダごと** 上げる（公開フォルダの中に `publish` フォルダができ、リンク先が更新されない）。
- API キー（キーファイル）・`settings.toml`・ログを上げる。

### チェックリスト（毎週）
- [ ] `-NoSync` で実行し、ログの最後の行が「完了。」か「完了（警告…）」
- [ ] `publish check` が ok（実行と同じ日にアップロードしている）
- [ ] `risk_diff.xlsx`・`monitor.xlsx`・`週次要約.md`・`社別\*.md`（・`templates\*.xlsx`）を「置き換え」で上げた
- [ ] 公開フォルダの `社別` から、`publish\社別` にない古い md だけを消した
- [ ] 最後に `社一覧.xlsx` を「置き換え」で上げた
- [ ] 公開フォルダの「更新日時」がすべて当日で、`(1)` などの付いた重複ファイルがない
- [ ] キー・`settings.toml`・`out\publish\`・`publish` フォルダそのものを上げていない
