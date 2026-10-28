# M2 進捗（プライム拡大と監視）— 第1バッチ

作成日: 2026-10-28。対象: 第1バッチ Step A〜D（母集団・コード・書類一覧の土台づくり）。
参照: `/workspace/edinet-risk-review/implementation-plan.md` §3-2・§4 M2・§5・§6-1・§6-4・§6-5。M1 の成果は `docs/m1-progress.md`。

**この文書は途中経過の記録です。M2 の合格は宣言しません。** 受け入れ条件 1〜13 の大半は、次バッチ以降の作業と人手の確認が終わるまで判定できません（§4）。

## 1. 実装したもの（Step A〜D）

| Step | 内容 | 主なファイル |
|---|---|---|
| A | スキーマ v2。`population` テーブルを追加（`as_of_month`・`sec_code4` が主キー。`sec_code4` は常に TEXT で、英字入りコード（例 `130A`）も扱えます）。§5 のうち v1 に無かった M2 用テーブルは `population` だけで、既存テーブルの列追加はありません。`llm_*` は M3 に回します。 | `store/migrations/0002_m2_population.sql`、`store/migrate.py` |
| B | `codes sync`。EDINETコード一覧（`Edinetcode.zip`）と集約一覧（`ESE140190.csv`）を取得し、`edinet_codes`・`code_aggregation` に upsert します。`cache/codes/` を再利用し、`--refresh` で再取得します。ローカルファイルからも取り込めます（`--from-file`・`--aggregation-file`）。 | `population/edinet_codes.py` |
| C | `population load`。JPX 東証上場銘柄一覧（`data_j.xlsx`）からプライム（内国株式）だけを取り込み、`sec_code5 == sec_code4 || '0'` で `edinet_codes` と結合します。結合方法は `match_method`（`sec_code`／`sec_code_listed`／`ambiguous`／`unmatched`）に記録します。同じ基準月を再投入すると置き換わります（冪等）。 | `population/jpx.py`、`docs/jpx-population.md` |
| D | `doclist sync` の拡張。`--from/--to`・`--daily`・`--rescan-month`（`--window current|previous|both`）・`--from-cache`・`--refresh`・`--dry-run`・`--offline` に対応しました。`doclist_raw` は追記のみで、`documents` へのマージは版選択の `merge_state` を使います。基準日時点の状態は `state_as_of` で再構成します。確定日（取得日 > 対象日）の判定、静穏時間帯（00:00〜01:00）の停止、1秒以上の間隔、429 のときのバックオフ、`api_calls` への記録（キーは含めません）も入れました。 | `ingest/doclist.py`、`net/edinet_api.py`、`resolve/versions.py` |

テストを追加・拡張したファイル: `tests/test_store.py`、`tests/test_codes.py`、`tests/test_population.py`、`tests/test_doclist_sync.py`。

## 2. 実データの件数

DB は既定パスの `~/.local/share/edinet-risk/edinet_risk.sqlite`（`config/settings.toml` は未作成）、スキーマは `user_version=2` です。2026-10-28 時点の件数です。

### 2-1. コード一覧（`codes sync`、取得 2026-10-28 14:44 JST）

| 項目 | 件数 |
|---|---|
| `edinet_codes` 全体 | 11,433 |
| うち上場（`listed=1`） | 3,812（証券コードあり 3,809、空 **3**） |
| うち非上場（`listed=0`） | 1,300 |
| うち上場区分が空 | 6,321 |
| `code_aggregation` | 3（E37984→E40962、E38680→E38709、E40481→E26533） |

### 2-2. 母集団（`population load --as-of-month 2026-09`、`ref/jpx/data_j_202609.xlsx`）

| 項目 | 件数 |
|---|---|
| JPX 一覧の全行 | 4,447 |
| プライム（内国株式）の行 | 1,550 |
| うち5桁コード（優先株・種類株式）で除外 | 7 |
| `population`（2026-09） | **1,543** |
| `match_method=sec_code`（1対1で結合） | 1,543 |
| 結合できなかったもの・候補が複数のもの | 0 |

### 2-3. 書類一覧（`doclist sync`）

| 項目 | 値 |
|---|---|
| `doclist_raw` | 99,946 行 |
| 対象日の範囲 | 2025-06-02〜2026-10-27（264日分。キャッシュから 2025-06-01〜2026-06-30 を取り込み、API で 2026-10-26・27 を取得） |
| `documents`（docID ごとのマージ結果） | 99,440 件 |
| `documents` のうち docTypeCode 120／130／150 | 13,846／936／150 |
| 有報（120・010・030000）で periodEnd が 2025-04-01〜2026-03-31 のもの | 4,126 件（4,122社） |
| 上記の有報がある母集団の会社（参考値） | 1,531／1,543 |
| 母集団の会社の訂正（130） | 388 件 |
| `withdrawal_status` 0／1／2 | 99,028／71／341 |
| `runs`／`api_calls` | 4／4（codes 2件、documents.json 2件） |

「1,531／1,543」は `documents` を単純に結合した参考値です。取得・抽出を経た突合率（M2-1）ではありません。当期の提出日窓（2025-04-01〜2026-09-30）のうち、2025-04-01〜05-31 と 2026-07-01〜10-25 はまだ走査していません。

## 3. CLI 一覧（現時点）

| コマンド | 状態 |
|---|---|
| `init-db [--config] [--db]` | M1。v2 マイグレーションを含む |
| `codes sync [--refresh] [--from-file] [--aggregation-file] [--cache] [--db]` | **M2 第1バッチ** |
| `population load --jpx <xlsx> --as-of-month YYYY-MM [--db]` | **M2 第1バッチ** |
| `doclist sync (--from D [--to D] \| --daily \| --rescan-month [--window ...] \| --from-cache) [--refresh] [--dry-run] [--offline] [--db] [--cache]` | **M2 第1バッチで拡張** |
| `m1_resolve`・`m1_segment`・`m1_match`・`compare`・`report`・`run` | M1（7社用）。全社化は次バッチ以降 |
| `m0_spike`・`m0_truncation`・`m0_multi`・`m0_scan` | M0 |
| `resolve --fy YYYY-MM --as-of D [--entity CODE… \| --targets TOML \| --transport7] [--as-of-month] [--successor-map] [--db] [--cache] [--out]` | **M2 第2バッチ Step F**（既定は母集団全社。オフライン） |
| `fetch [--entity … \| --targets \| --transport7 \| --all] [--run-id] [--retry-failed] [--max-attempts] [--dry-run] [--offline]` | **M2 第2バッチ Step G** |
| `monitor`・`gold eval`・`selftest` | 未実装 |

共通オプション: `--offline`（ネットワークを禁止。違反時は終了コード 3）、`--log-level`。

## 4. 受け入れ条件（計画 §4 M2）との対応

| # | 条件（要約） | 状況 | メモ |
|---|---|---|---|
| 1 | 突合率 ≥ 99%、残りに理由、`unknown` は人が全件確認 | 一部 | 母集団 1,543社とコードの結合（全件 `sec_code`）まで。取得・抽出と理由の付与は未着手 |
| 2 | 取得失敗・空抽出が0件、または理由付きで監視に載る | 未着手 | fetch（全社）・再試行キューは次バッチ |
| 3 | 前期なしの理由別件数、変則・`prev_gap` の一覧 | 未着手 | resolve の全社化は次バッチ |
| 4 | 合成データのテスト（取下げ・不開示・再登場・T 後の訂正・取下げ・`correction_unparsed`・702001・`correction_partial`・`prev_missing_year`） | 一部 | 再登場の採用・T 時点の状態・取下げは `test_doclist_sync.py` で確認済み。M2-4 の全項目をそろえた合成テストは次バッチ |
| 5 | 層別サンプル評価用で適合率・再現率・境界F1、閾値は調整用だけで | 人手待ち | ラベル付け担当者の確保（B7） |
| 6 | 間隔 ≥ 1秒・並列1、429/5xx で60秒からの指数バックオフ、ログにキーなし | 一部 | 書類一覧では、間隔・429 のバックオフ・`api_calls` にキーを残さないことをテストで確認済み。書類取得（全社）と、全ログを grep する確認は未着手 |
| 7 | 強制終了後の再実行で再取得せずに完了し、結果が一致 | 未着手 | fetch（全社）の後 |
| 8 | 監視レポート（Excel・CSV）と `monitor --fail-on-threshold` | 未着手 | |
| 9 | 初回が12時間以内、日次差分が30分以内 | 未着手 | 全社実行の後に計測 |
| 10 | 訂正の実例10件が人の判断と一致（10/10） | 人手待ち | 候補の抽出は resolve の全社化の後 |
| 11 | 120 があるのに採用版が無い会社が0件、または理由付きで人が確認 | 未着手 | `filing_filter.toml` の整備と resolve の全社化の後 |
| 12 | 理由ごとに無作為5件を人が確認 | 人手待ち | 理由の付与（1・3）の後 |
| 13 | 3万字超の3社で末尾段落が閲覧画面と一致（全社実行の前） | 人手待ち | 対象3社の選定と閲覧画面・PDF との照合 |

## 5. 人手待ち

- **M2-5 層別サンプル**: 40〜60社のラベル付け（業務側）と、調整用・評価用の分割。担当者の確保（B7）が前提です。
- **M2-10 訂正の実例10件**: 年度窓で訂正（130・702001）があるプライムの会社から10件以上を選び、採用 docID とフラグを人が判断します。
- **M2-12 理由のサンプル確認**: 突合・前期なしの理由ごとに無作為5件を確認します。
- **M2-13 末尾段落**: 3万字超の3社（体裁の異なるもの）で、抽出本文の末尾段落と EDINET 閲覧画面（または PDF）の末尾段落を照合します。全社実行の前に行います。

## 6. 既知の注意点

- **5桁コードの除外**: JPX 一覧のプライムのうち5桁コードの7行（優先株・社債型種類株式）は、母集団が会社単位のため除外しています。除外した分は `population load` の出力に一覧で出ます。
- **上場中で証券コードが空の3社**: `edinet_codes` で `listed=1` なのに `sec_code5` が空の会社が3社あります。今回の母集団の結合には影響しません（不一致0件）。全社化の際に突合理由の扱いを確認します。
- **集約先 E38709 がコード一覧に無い**: 集約一覧の E38680→E38709（2023-10-12）について、集約先が `edinet_codes` にありません。承継・集約の entity_id を作るときに扱いを決めます。
- **entity_id は未読み替え**: `documents.entity_id` は現在 `edinet_code` をそのまま入れています（95,472件。edinetCode の無い 3,968件は NULL）。集約一覧と `successor_map`（現在0行。`config/successor_map.csv` は未取込み）による読み替えは次バッチで行います。
- **`state_as_of` の制約**: `doclist_raw` の全行が同じ `fetched_at`（2026-10-28T00:01、応答の `processDateTime`）です。つまり、基準日 T=2026-09-30 より前に撮ったスナップショットはありません。T 時点の状態は、opeDateTime による除外と取下書の提出日時から再構成しています。opeDateTime が T より後のレコードしか無い書類（現時点の `documents` で2件）は、T 時点の状態から外れます。取下書が走査範囲の外にある場合は `withdrawal_time_unknown` になります。
- **rescan で行が増える**: `doclist_raw` は追記のみです（主キーは file_date・seqNumber・fetched_at）。`processDateTime` が変わった後に `--rescan-month` や `--refresh` を実行すると、窓全体（今回なら約10万行）がそのたびに追記されます。容量の見積りと、古いスナップショットの扱いは運用開始前に決めます。

## 7. 次バッチ

1. **resolve の全社化**: 母集団 × 年度窓で版の選択と前期の特定を行い、突合・前期なしの理由を付けます。集約一覧と承継マップによる entity_id もここで作ります（E38709 の扱いを含みます）。
2. **fetch（全社）と再試行キュー**: レート制御、取得済みのスキップ、中断後の再開（M2-2・M2-7）。
3. **monitor**: §8-3 の指標を Excel シートと CSV に出し、`--fail-on-threshold` で終了コード2を返します（M2-8）。
4. **合成テスト M2-4**: 条件4の全項目をそろえます。
5. **`config/filing_filter.toml`**: M2 向けに版を上げます（`candidates` の 130・030001・702001 を有効にし、対象外の様式を判定します。M2-11）。

## 8. テスト結果

```
.venv/bin/pytest -q -m "not live"
276 passed, 1 skipped, 2 deselected in 15.49s
```

- M1 最終時点の 229 passed から 47件増えました（主に Step A〜D で追加・拡張したテストです）。
- skipped 1: `tests/test_match.py`。scipy が未導入のため、ハンガリアン法のオラクル比較を skip しています（M1 と同じ）。
- deselected 2: live テストです（ネットワークを使うため除外）。

---

# 第2バッチ Step F: resolve の全社化（2026-10-28）

## F-1. 実装

| 内容 | ファイル |
|---|---|
| entity_id: `code_aggregation` → `successor_map` の順に固定点まで読み替え（連鎖可、循環は停止、1対多の承継は読み替えない）。`config/successor_map.csv` を実行ごとに `successor_map` へ全置換で取り込み（ヘッダ・コード形式・日付を検査、`#` 行は注記）。`documents.entity_id` も書き換え、`merge_documents` の既定もこの対応表 | `resolve/entity.py`、`ingest/doclist.py` |
| 突合・前期なしの理由と判定根拠（`reason_basis_json.reason_basis`） | `resolve/reasons.py` |
| 対象（母集団／`--entity`／targets ファイル）ごとの版選択・前期特定・理由付け、`filing_selection` へ会社単位のトランザクションで保存、集計・出力（json/csv/md） | `resolve/runner.py` |
| `select_version` に `available`（本文がオフラインで取れるか）を追加。訂正が複数版あり本文が未取得なら 3a/4 を判定せず `texts_pending` に記録（未取得を `correction_partial` と誤判定しない） | `resolve/versions.py`、`resolve/period.py` |
| CLI `resolve`。`m1_resolve` は `runner` 経由に統合（出力 `resolve_transport7.json` は M1 と同一を確認）。`--transport7`／`--targets` では同形式の `resolve_transport7.json` も出力 | `__main__.py`、`m1_resolve.py` |
| 決算日（`Edinetcode.zip` の「決算日」）の読取り。DB には保存せず、`cache/codes/` から読む | `population/edinet_codes.py` |

スキーマ変更なし（v2 のまま）。当期側の突合理由は `filing_selection` の cur 行の `reason_basis_json.reason`、前期なしの理由は prev 行の `no_prev_reason` と `reason_basis_json.reason` に入れます。

## F-2. 判定ルール（§6-5 の表を具体化）

- 突合（当期）: `code_mismatch`（母集団の edinet_code が空・コード一覧にない）> `form_out_of_scope`（窓内に絞り込み外の 120/130）> `not_filed`／`late`（T までに対象の 120 なし。期待 periodEnd は前年の有報＋12か月、無ければ決算日。期限＝periodEnd＋3か月）> `xbrl_missing` > `fetch_failed`（`fetches.status=failed` または空抽出）。
- 前期なし: `prev_missing_year`（gap>31日）> `prev_not_fetched` > `successor_suspected`（前期相当の 120 が別 entity に secCode・法人番号・社名（「株式会社」「ホールディングス」等を除いて）一致で存在、または当社の「有価証券届出書（組織再編成・上場／組織再編成）」027000/026000）> `new_listing`（「有価証券届出書（新規公開時）」024000。参照方式 023000 は上場会社の株式報酬等なので根拠にしない）。
- `unknown` は必ず `unknown_cause` を持つ: `doclist_not_scanned`（不在を示すべき提出日が未走査。`would_be` に本来の理由）、`no_annual_report_history`（有報の履歴も決算日以前の書類もなく期限を決められない）、`no_basis_matched`。
- **E38709 の扱い（決定）**: 集約一覧 E38680→E38709 は、集約先がコード一覧に無くても**読み替える**（集約一覧は EDINET 自身の「同一提出者」の記録で、コード一覧は現存コードだけのため）。`entity note: aggregation_target_not_in_codes` として毎回出力します。両コードとも母集団・書類一覧に現れないため、今回の突合には影響しません。

## F-3. 実DBでの結果（母集団 2026-09、FY 2026-03、T=2026-09-30、API 呼び出しなし）

run_id `resolve-20261028T151259-ac31`、出力 `out/m2/resolve_resolve-20261028T151259-ac31/`。

| 項目 | 件数 |
|---|---|
| 対象（母集団） | 1,543（entity 1,543、重複 0） |
| 当期の採用版あり（突合） | **1,531**（99.2%。取得・抽出は未実施なので M2-1 の突合率ではない） |
| 前期あり | 1,087 |
| 突合の理由 | `late` 6（アサヒGHD、エア・ウォーター、コタ、ニデック、イリソ電子工業、燦HD）、`unknown` 6（2026年の持株会社化 6社: `no_annual_report_history`） |
| 前期なしの理由 | `unknown` 439、`new_listing` 3（オリオンビール、テクセンドフォトマスク、NSグループ）、`successor_suspected` 2（ブルーゾーンHD、ARCHION） |
| unknown 計 | **445**（`doclist_not_scanned` 439、`no_annual_report_history` 6） |
| flags | `irregular` 4（テイクアンドギヴ・ニーズ、ソースネクスト、ヤーマン、ARCHION）、他は 0 |
| 訂正の本文未取得で `correction_partial`／`risk_changed_by_correction` 未判定 | 158社 |
| edinetCode なしでスキップした書類 | 3,968 |

- `doclist_not_scanned` 439件の大半は非3月決算（12月 183、2月 75 …）。前期の有報の提出日（2025-04-01〜05-31 など）が**未走査**なためで、`doclist sync --from 2025-04-01 --to 2025-05-31`（と前年度窓）を実行すれば多くは解消する見込みです（API を使うので今回は未実行）。
- `prev_gap`・`correction_*` が 0 なのは、本文（type=1）を未取得で判定できないためです。全社の fetch の後に再実行します。
- 輸送7社（`resolve --transport7`）は M1 の `resolve_transport7.json` と採用版・前期・flags・理由が一致しました。

## F-4. テスト

`tests/test_resolve_population.py`（18件）: 3月・12月・変則（9か月）決算、承継（successor_map なし→`successor_suspected`、CSV 取込み後→前期として連結）、集約（旧コードの母集団行、集約先がコード一覧にない注記）、循環・1対多、新規上場（024000 と 023000 の区別）、組織再編成、`prev_gap`・`prev_missing_year`、未走査→`unknown`、`prev_not_fetched`、`form_out_of_scope`、`not_filed`・`late`、`code_mismatch`・`xbrl_missing`・`fetch_failed`、`correction_unparsed`・`texts_pending`、edinetCode なしのスキップ件数、CLI（母集団・`--entity`）。

```
.venv/bin/pytest -q -m "not live"
294 passed, 1 skipped, 2 deselected
```

---

# 第2バッチ Step G: fetch と再試行キュー（2026-10-28）

## G-1. 実装

| 内容 | ファイル |
|---|---|
| CLI `fetch [--entity CODE… \| --targets TOML \| --transport7 \| --all] [--run-id] [--retry-failed] [--max-attempts N] [--dry-run] [--offline]`。対象は resolve run（既定は最新の ok）の `filing_selection` の cur/prev の doc_id だけ。**範囲指定なしで API を呼ぶ実行は `--all` が無いと拒否**（終了コード 2。`--dry-run`・`--offline` は可） | `__main__.py` |
| 計画（`have`／`adopt`／`fetch`／`failed`／`no_xbrl`）と実行。ok かつファイルの sha256 一致なら再取得しない。キャッシュに有効な ZIP があれば3点検査のうえ API を呼ばずに ok として記録（M0/M1 の取得分・中断時の救済）。docID ごとにコミット | `ingest/fetch.py` |
| 401/403（HTTP または本文 JSON）で即停止（終了コード 4、run は `aborted`、その書類は pending のまま・試行回数に数えない）。429/5xx は 60秒からの指数バックオフ（`api.backoff_max` で上限、`api.max_retries` で回数）。使い切ったら `retry`、`api.fetch_max_attempts`（既定3）に達したら `failed`。400/404・不正な本体は `failed`（`--retry-failed` でのみ再要求） | `ingest/fetch.py`、`net/edinet_api.py`、`config.py` |
| HTTP 401/403 で本文が status 欄の無い JSON の場合も認証エラーとして扱うよう修正 | `net/edinet_api.py` |
| スキーマ v3: `fetches` を作り直し（status に `retry`・`no_xbrl` を追加、`error_kind`・`last_http_status`・`last_attempt_at`・`run_id` 列を追加、既存行は保持） | `store/migrations/0003_m2_fetch_queue.sql` |
| resolve の `fetch_failed` は `failed` に加えて `retry` も対象 | `resolve/reasons.py` |

本体の置き場所は M0/M1 と同じ `cache/docs/{docID}_type1.zip`（resolve・compare が読む場所）。計画 §3 の `raw/` への移行は未実施です。

## G-2. 実DBでの確認（ネットワークなし）

- `fetch --transport7 --dry-run --offline`: 7社 14 docID、API 呼び出し 0（14件すべてキャッシュ済み）。
- `fetch --transport7 --offline` を2回: 1回目 `adopted_from_cache=14`、2回目 `have=14`。どちらも **API 呼び出し 0**（`api_calls` は 4行のまま）。DB は v3 に移行（移行前の複製 `edinet_risk.sqlite.bak-before-stepG`）。
- 参考（`fetch --dry-run`、母集団全体）: 1,531 entity・2,618 docID のうち、取得が必要なもの 2,595（1秒間隔で約43分以上）。**実行はしていません。**

## G-3. テスト

`tests/test_fetch.py`（21件）: 401/403 の即停止（HTTP 401・403、HTTP 401＋status なしの JSON、HTTP 200＋`StatusCode` 401、HTTP 200＋`metadata.status` 403）、バックオフ秒列 [60,120,240,480]、上限 [60,100,100]、retry→retry→failed、通信エラーの扱いとマスク、間隔1秒、再取得なし（sha256 不一致だけ再取得）、キャッシュからの記録と壊れたファイルの再取得、404→failed と `--retry-failed`、不正な本体、xbrlFlag=0、entity による絞り込み、CLI（dry-run で DB 無変更、offline、範囲指定なしの拒否、401 停止時に DB・stdout・ログ・`api_calls.jsonl` にキーが出ないこと）。`test_store.py` は v3 に更新。

```
.venv/bin/pytest -q -m "not live"
314 passed, 1 skipped, 2 deselected
```
