# publish_peer 仕様（リネーム表）

`scripts/publish_peer.ps1`（管理用 Windows・週次）の仕様。スクリプト本体は実装済み（社内 Windows での実機確認は未。確認状況は `admin-runbook.md` §11）。本書の §1 が公開ファイルの正規名の唯一の定義で、`overview.md`・`easiest-path.md`・`copilot-runbook.md`・`copilot-architecture.md` はここを参照する。

## 方針
- CLI（`python -m edinet_risk`）の出力名は変えない。CLI は run ごとのフォルダに run 固有の名前で出す（例: `risk_diff_transport7.xlsx`、`monitor_<run-id>/monitor.xlsx`）。
- `publish_peer.ps1` が CLI 出力を `publish/` に **正規名でコピー（リネーム）** し、`publish/` を SharePoint の公開フォルダへ同期する。
- 公開フォルダの名前は固定。統合プロンプトのリンクは正規名を指すので、名前を変えるとリンクが切れる。上書き保存で更新する（削除→新規作成にしない。リンクが変わるため）。

## 1. 正規名（公開フォルダ）とリネーム表

| 正規名（公開フォルダ） | 元（CLI 出力） | 生成元コマンド | 状態 |
|---|---|---|---|
| `risk_diff.xlsx` | `<out>/m1/run_<run-id>/risk_diff_transport7.xlsx` | `compare` → `report` | CLI あり（リネームのみ） |
| `monitor.xlsx` | `<out>/monitor_<run-id>/monitor.xlsx` | `monitor` | CLI あり（リネームのみ） |
| `社一覧.xlsx` | `<out>/resolve_<run-id>/resolve.json`（対象社の行） | `resolve` | `publish build` が生成（列: 証券コード, 社名, 区分, EDINETコード, 当期書類, 当期期末, 前期書類, 前期期末, 基準日T, 更新日。区分 = `ピア`（`publish.targets` の1つ目＝transport7）／`依頼`（2つ目以降＝依頼社リスト）。`[[filer]]` の `group = "ピア"/"依頼"` で個別に上書き可。基準日T = resolve の as_of） |
| `社別/<証券コード>_<社名>.md` | `<out>/m1/run_<run-id>/compare.json`（前期/当期の項目本文） | `compare` | `publish build` が生成 |
| `週次要約.md` | `monitor.md` ＋ `reconcile.md` ＋ 対象社一覧 | `monitor` / `report` | `publish build` が連結して生成。xlsx をリンクで読めない場合の代替（`copilot-runbook.md`「パイロット第1確認」） |

任意（成功判定の5種に含めない）: `templates/*.xlsx` ← `docs/templates/*.xlsx`（Copilot の見本ブック。`差分詳細`・`分析` シートを含む）を `publish build` がコピー。

参考（公開しない）: `html/index.html`・`html/diff_NN_<社名>.html`（`report` の全文差分）、`reconcile.csv`/`.md`、`monitor_summary.csv`/`monitor_details.csv`/`monitor.json`。管理者の点検用。

## 2. 処理順（`publish_peer.ps1` の実装）
1. キーを `EDINET_API_KEY` か `paths.api_key_file` から読む（スクリプトに書かない）。
   設定ゲート: `-DryRun`・`-NoSync` のどちらでもないのに settings が見つからない、または `publish.dest_dir`（`-Dest`）が空なら、何も実行せず終了コード 1。
2. 対象社 = `config/transport7.toml` ＋ 依頼社リスト（`publish targets`）。
3. `doclist sync` → `resolve` → `fetch` → `extract` → `resolve` → `compare` → `report` → `monitor`。
   基準日T: 2回の `resolve` に `--as-of <publish config の as_of>` を渡す。`publish.as_of` 既定 `"today"` = 実行日（JST）。固定日 `"YYYY-MM-DD"` も可（再現用）。
   T が `window.current.submit_to` を過ぎる、または `run.as_of` から92日超のときは警告（続行）。
   書類一覧の範囲: `publish config` の `doclist_from`〜`doclist_to`。`当日-(doclist_days-1)` から前へ、キャッシュで確定していない日（未取得・当日取得）をさかのぼり、直近の確定日（＝前回の成功同期）の翌日から当日まで。上限 `publish.doclist_max_days`（既定 62）日。上限に当たったら警告。確定済みの日は再取得しない。
   取得ゲート: `doclist sync` が 0 以外（2 を含む）で終わったら、その週は公開しない。`fetch` が終了コード 2（一部失敗）のとき、
   新規取得（`fetched` + `adopted_from_cache`）が 0 件なら公開しない。どちらも ps1 は終了コード 1 で終わり、同期フォルダは前回分のまま。
   `fetch` が 0 で新規 0 件（取得対象がすべて取得済み）は正常として続行する。
4. `publish build` が §1 の3種を生成し、ps1 が `risk_diff.xlsx`・`monitor.xlsx` を正規名でコピーする（`publish check` で点検）。
5. 成功の定義: `publish/` の5種がそろい、`社一覧.xlsx` と `monitor.xlsx` の更新日が当日。1つでも欠けたら同期しない（前回分を残す）。
6. `publish/` を SharePoint 同期フォルダへコピー。
