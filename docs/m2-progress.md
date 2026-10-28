# M2 進捗（プライム拡大と監視）— 第1・第2バッチ

作成日: 2026-10-28（第2バッチ Step F〜I を反映して更新）。対象: 第1バッチ Step A〜D（母集団・コード・書類一覧の土台）、第2バッチ Step F〜I（resolve の全社化・fetch・monitor・合成テスト）。
参照: `/workspace/edinet-risk-review/implementation-plan.md` §3-2・§4 M2・§5・§6-1・§6-4・§6-5。M1 の成果は `docs/m1-progress.md`。

**この文書は途中経過の記録です。M2 の合格は宣言しません。** 受け入れ条件 1〜13 の大半は、次バッチ以降の作業と人手の確認が終わるまで判定できません（§5）。

## 1. 第1バッチで実装したもの（Step A〜D）

| Step | 内容 | 主なファイル |
|---|---|---|
| A | スキーマ v2。`population` テーブルを追加（`as_of_month`・`sec_code4` が主キー。`sec_code4` は常に TEXT で、英字入りコード（例 `130A`）も扱えます）。§5 のうち v1 に無かった M2 用テーブルは `population` だけで、既存テーブルの列追加はありません。`llm_*` は M3 に回します。 | `store/migrations/0002_m2_population.sql`、`store/migrate.py` |
| B | `codes sync`。EDINETコード一覧（`Edinetcode.zip`）と集約一覧（`ESE140190.csv`）を取得し、`edinet_codes`・`code_aggregation` に upsert します。`cache/codes/` を再利用し、`--refresh` で再取得します。ローカルファイルからも取り込めます（`--from-file`・`--aggregation-file`）。 | `population/edinet_codes.py` |
| C | `population load`。JPX 東証上場銘柄一覧（`data_j.xlsx`）からプライム（内国株式）だけを取り込み、`sec_code5 == sec_code4 || '0'` で `edinet_codes` と結合します。結合方法は `match_method`（`sec_code`／`sec_code_listed`／`ambiguous`／`unmatched`）に記録します。同じ基準月を再投入すると置き換わります（冪等）。 | `population/jpx.py`、`docs/jpx-population.md` |
| D | `doclist sync` の拡張。`--from/--to`・`--daily`・`--rescan-month`（`--window current|previous|both`）・`--from-cache`・`--refresh`・`--dry-run`・`--offline` に対応しました。`doclist_raw` は追記のみで、`documents` へのマージは版選択の `merge_state` を使います。基準日時点の状態は `state_as_of` で再構成します。確定日（取得日 > 対象日）の判定、静穏時間帯（00:00〜01:00）の停止、1秒以上の間隔、429 のときのバックオフ、`api_calls` への記録（キーは含めません）も入れました。 | `ingest/doclist.py`、`net/edinet_api.py`、`resolve/versions.py` |

テストを追加・拡張したファイル: `tests/test_store.py`、`tests/test_codes.py`、`tests/test_population.py`、`tests/test_doclist_sync.py`。

## 2. 実データの件数（第1バッチ時点）

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

## 3. 第2バッチで実装したもの（Step F〜I）

### Step F: resolve の全社化

#### F-1. 実装

| 内容 | ファイル |
|---|---|
| entity_id: `code_aggregation` → `successor_map` の順に固定点まで読み替え（連鎖可、循環は停止、1対多の承継は読み替えない）。`config/successor_map.csv` を実行ごとに `successor_map` へ全置換で取り込み（ヘッダ・コード形式・日付を検査、`#` 行は注記）。`documents.entity_id` も書き換え、`merge_documents` の既定もこの対応表 | `resolve/entity.py`、`ingest/doclist.py` |
| 突合・前期なしの理由と判定根拠（`reason_basis_json.reason_basis`） | `resolve/reasons.py` |
| 対象（母集団／`--entity`／targets ファイル）ごとの版選択・前期特定・理由付け、`filing_selection` へ会社単位のトランザクションで保存、集計・出力（json/csv/md） | `resolve/runner.py` |
| `select_version` に `available`（本文がオフラインで取れるか）を追加。訂正が複数版あり本文が未取得なら 3a/4 を判定せず `texts_pending` に記録（未取得を `correction_partial` と誤判定しない） | `resolve/versions.py`、`resolve/period.py` |
| CLI `resolve`。`m1_resolve` は `runner` 経由に統合（出力 `resolve_transport7.json` は M1 と同一を確認）。`--transport7`／`--targets` では同形式の `resolve_transport7.json` も出力 | `__main__.py`、`m1_resolve.py` |
| 決算日（`Edinetcode.zip` の「決算日」）の読取り。DB には保存せず、`cache/codes/` から読む | `population/edinet_codes.py` |

スキーマ変更なし（v2 のまま）。当期側の突合理由は `filing_selection` の cur 行の `reason_basis_json.reason`、前期なしの理由は prev 行の `no_prev_reason` と `reason_basis_json.reason` に入れます。

#### F-2. 判定ルール（§6-5 の表を具体化）

- 突合（当期）: `code_mismatch`（母集団の edinet_code が空・コード一覧にない）> `form_out_of_scope`（窓内に絞り込み外の 120/130）> `not_filed`／`late`（T までに対象の 120 なし。期待 periodEnd は前年の有報＋12か月、無ければ決算日。期限＝periodEnd＋3か月）> `xbrl_missing` > `fetch_failed`（`fetches.status=failed` または空抽出）。
- 前期なし: `prev_missing_year`（gap>31日）> `prev_not_fetched` > `successor_suspected`（前期相当の 120 が別 entity に secCode・法人番号・社名（「株式会社」「ホールディングス」等を除いて）一致で存在、または当社の「有価証券届出書（組織再編成・上場／組織再編成）」027000/026000）> `new_listing`（「有価証券届出書（新規公開時）」024000。参照方式 023000 は上場会社の株式報酬等なので根拠にしない）。
- `unknown` は必ず `unknown_cause` を持つ: `doclist_not_scanned`（不在を示すべき提出日が未走査。`would_be` に本来の理由）、`no_annual_report_history`（有報の履歴も決算日以前の書類もなく期限を決められない）、`no_basis_matched`。
- **E38709 の扱い（決定）**: 集約一覧 E38680→E38709 は、集約先がコード一覧に無くても**読み替える**（集約一覧は EDINET 自身の「同一提出者」の記録で、コード一覧は現存コードだけのため）。`entity note: aggregation_target_not_in_codes` として毎回出力します。両コードとも母集団・書類一覧に現れないため、今回の突合には影響しません。

#### F-3. 実DBでの結果（母集団 2026-09、FY 2026-03、T=2026-09-30、API 呼び出しなし）

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

- `doclist_not_scanned` 439件の大半は非3月決算（12月 183、2月 75 …）。前期の有報の提出日（2025-04-01〜05-31 など）が**未走査**なためで、未走査窓の `doclist sync` で多くは解消する見込みです（§6。API を使うので今回は未実行）。
- `correction_partial`・`risk_changed_by_correction` が 0 なのは、本文（type=1）が未取得で判定していないためです（158社が `texts_pending`）。全社の fetch の後に再実行します。`prev_gap` は期間だけで判定するもので、前期を特定できた 1,087社では 0件でした（未走査で前期が見つかっていない 439社は未判定）。
- 輸送7社（`resolve --transport7`）は M1 の `resolve_transport7.json` と採用版・前期・flags・理由が一致しました。

#### F-4. テスト

`tests/test_resolve_population.py`（18件）: 3月・12月・変則（9か月）決算、承継（successor_map なし→`successor_suspected`、CSV 取込み後→前期として連結）、集約（旧コードの母集団行、集約先がコード一覧にない注記）、循環・1対多、新規上場（024000 と 023000 の区別）、組織再編成、`prev_gap`・`prev_missing_year`、未走査→`unknown`、`prev_not_fetched`、`form_out_of_scope`、`not_filed`・`late`、`code_mismatch`・`xbrl_missing`・`fetch_failed`、`correction_unparsed`・`texts_pending`、edinetCode なしのスキップ件数、CLI（母集団・`--entity`）。


### Step G: fetch と再試行キュー

#### G-1. 実装

| 内容 | ファイル |
|---|---|
| CLI `fetch [--entity CODE… \| --targets TOML \| --transport7 \| --all] [--run-id] [--retry-failed] [--max-attempts N] [--dry-run] [--offline]`。対象は resolve run（既定は最新の ok）の `filing_selection` の cur/prev の doc_id だけ。**範囲指定なしで API を呼ぶ実行は `--all` が無いと拒否**（終了コード 2。`--dry-run`・`--offline` は可） | `__main__.py` |
| 計画（`have`／`adopt`／`fetch`／`failed`／`no_xbrl`）と実行。ok かつファイルの sha256 一致なら再取得しない。キャッシュに有効な ZIP があれば3点検査のうえ API を呼ばずに ok として記録（M0/M1 の取得分・中断時の救済）。docID ごとにコミット | `ingest/fetch.py` |
| 401/403（HTTP または本文 JSON）で即停止（終了コード 4、run は `aborted`、その書類は pending のまま・試行回数に数えない）。429/5xx は 60秒からの指数バックオフ（`api.backoff_max` で上限、`api.max_retries` で回数）。使い切ったら `retry`、`api.fetch_max_attempts`（既定3）に達したら `failed`。400/404・不正な本体は `failed`（`--retry-failed` でのみ再要求） | `ingest/fetch.py`、`net/edinet_api.py`、`config.py` |
| HTTP 401/403 で本文が status 欄の無い JSON の場合も認証エラーとして扱うよう修正 | `net/edinet_api.py` |
| スキーマ v3: `fetches` を作り直し（status に `retry`・`no_xbrl` を追加、`error_kind`・`last_http_status`・`last_attempt_at`・`run_id` 列を追加、既存行は保持） | `store/migrations/0003_m2_fetch_queue.sql` |
| resolve の `fetch_failed` は `failed` に加えて `retry` も対象 | `resolve/reasons.py` |

本体の置き場所は M0/M1 と同じ `cache/docs/{docID}_type1.zip`（resolve・compare が読む場所）。計画 §3 の `raw/` への移行は未実施です。

#### G-2. 実DBでの確認（ネットワークなし）

- `fetch --transport7 --dry-run --offline`: 7社 14 docID、API 呼び出し 0（14件すべてキャッシュ済み）。
- `fetch --transport7 --offline` を2回: 1回目 `adopted_from_cache=14`、2回目 `have=14`。どちらも **API 呼び出し 0**（`api_calls` は 4行のまま）。DB は v3 に移行（移行前の複製 `edinet_risk.sqlite.bak-before-stepG`）。
- 参考（`fetch --dry-run`、母集団全体）: 1,531 entity・2,618 docID のうち、取得が必要なもの 2,595（1秒間隔で約43分以上）。**実行はしていません。**

#### G-3. テスト

`tests/test_fetch.py`（21件）: 401/403 の即停止（HTTP 401・403、HTTP 401＋status なしの JSON、HTTP 200＋`StatusCode` 401、HTTP 200＋`metadata.status` 403）、バックオフ秒列 [60,120,240,480]、上限 [60,100,100]、retry→retry→failed、通信エラーの扱いとマスク、間隔1秒、再取得なし（sha256 不一致だけ再取得）、キャッシュからの記録と壊れたファイルの再取得、404→failed と `--retry-failed`、不正な本体、xbrlFlag=0、entity による絞り込み、CLI（dry-run で DB 無変更、offline、範囲指定なしの拒否、401 停止時に DB・stdout・ログ・`api_calls.jsonl` にキーが出ないこと）。`test_store.py` は v3 に更新。


### Step H: monitor

#### H-1. 実装

| 内容 | ファイル |
|---|---|
| §8-3 の指標の計算と閾値判定。入力は DB だけ（resolve run の `filing_selection`、その cur/prev docID の `fetches`・`extracts`・`items`・`matches`、`documents`、`api_calls`、`population`）。`metrics` に全体の指標（entity_id ''）と会社単位の行を保存、`runs` に `monitor` run を記録 | `monitor/metrics.py` |
| 出力: `out/m2/monitor_<run_id>/` に `monitor.xlsx`（シート「監視_概要」「監視_詳細」）、`monitor_summary.csv`、`monitor_details.csv`、`monitor.json`、`monitor.md`。全文字列をキーのマスク（URL パラメータ＋キー文字列そのもの）を通して書く | `monitor/output.py` |
| 閾値: `config/monitor.toml`（`paths.monitor`、`--thresholds` で上書き）。`[thresholds]` に書いた指標だけが超過の対象（`match_rate` は下限、他は上限）。既定は match_rate 0.99・unknown 0・fetch_failed 0・empty_extract 0・multi_hit 0・api_429_5xx 0。`[excluded_form]` は前回の monitor 比の急変（比 0.5 超かつ差 10 以上）、`[entity]` は M1 の会社単位の目安 | `config/monitor.toml`、`config.py` |
| CLI `monitor`。`--fail-on-threshold` で超過時に終了コード 2（無しなら 0）。入力エラーも 2 | `__main__.py` |

指標: 突合率（取得 ok かつ抽出済み ÷ 母集団）と参考の採用版率、突合外の全社と理由（resolve の理由、または `fetch_pending`／`extract_missing`／`empty_extract`／`fetch_failed`）、突合・前期なしの理由別件数、unknown、flags（irregular・prev_gap・correction_* ・risk_changed_by_correction）の一覧、form_out_of_scope、fetches の状態別（run の docID／テーブル全体）、取得失敗・no_xbrl、429・5xx（`--since` で期間を絞る。既定は全期間）、除外 formCode 別件数と前回比、M1 の空抽出・複数ヒット・項目数・文字数の前期比・画像あり低文字数・段落フォールバック・要確認比率。タクソノミの年次確認（L2）と LLM 棄却率（M3）は値なしの行として出します。

#### H-2. 実DBでの実行（API 呼び出しなし）

`monitor --fail-on-threshold` → **終了コード 2**。run_id `monitor-20261028T152945-487a`、出力 `out/m2/monitor_monitor-20261028T152945-487a/`。

| 指標 | 値 | 判定 |
|---|---|---|
| match_rate | 0／1,543（0%） | **超過**（全社 fetch 未実行のため。採用版率は 1,531／1,543＝99.2%） |
| unknown | 445（前期なし 439＋突合 6） | **超過**（Step F と同じ内訳） |
| 突合外 1,543 の理由 | fetch_pending 1,524、extract_missing 7、late 6、unknown 6 | |
| fetches（run の docID 2,618） | ok 14、未登録 2,604 | |
| extract_missing | 14（輸送7社の抽出は `out/m1/m1.sqlite` 側にあり、本 DB の `extracts` は 0） | |
| fetch_failed・empty_extract・multi_hit・api_429_5xx | 0 | 閾値内 |
| flag.irregular | 4（他の flag は 0） | 一覧 |
| 除外した 120/130（periodEnd 窓） | 6,471 | 初回のため前回比なし |

#### H-3. テスト

`tests/test_monitor.py`（14件）: 閾値内で 0、超過で 2（`--fail-on-threshold` なしは 0）、既定の各閾値（unknown・fetch_failed・empty_extract・multi_hit・match_rate 98%）の超過、`--since`、config による閾値の追加と未知キーの拒否、出力（xlsx・CSV・JSON・MD）と stdout に API キーが出ないこと、一覧と M1 指標、理由別件数と突合外の理由、除外 formCode の前回比、resolve run なしの場合。

### Step I: 合成テスト M2-4（受け入れ条件4）

`tests/test_m2_synthetic.py`（15件）。合成の書類一覧を `doclist_raw` に取り込み（スナップショットの追記）、`documents` へマージし、`runner.resolve_targets`（T 時点の状態、版グループ、前期の特定、理由付け）を通して、**保存された `filing_selection` の行**で確かめます。同じ規則の単体テストは `test_resolve.py`（versions・period）、`test_doclist_sync.py`（再登場の順序・`state_as_of`）、`test_resolve_population.py`（理由）にあり、今回は DB を通した経路で不足していた分を補いました。

| M2-4 の項目 | テスト | 確認内容 |
|---|---|---|
| 取下げ（withdrawalStatus=1/2）の除外 | `test_withdrawn_versions_excluded`、`test_only_version_withdrawn_has_no_adopted_version` | 取下げ済み（2、メタデータ空欄の再掲）と取下書（1、様式は原本と同じ）を `excluded=withdrawn` で外し、再提出版を採用。唯一の版が取下げなら採用版なし（`no_valid_version`）で突合理由は未提出／遅延、除外した版を判定根拠に記録 |
| 不開示の除外 | `test_not_disclosed_excluded` | 当期・前期とも `disclosureStatus=2` の版を `not_disclosed` で外す |
| docInfoEditStatus=1 の再登場で新しいレコードを採用 | `test_edit_relisting_adopted`、`test_edit_relisting_after_T_not_used_at_T` | 修正後（1）が取得順に関係なく勝ち、periodEnd の修正が採用版・`documents` に反映。opeDateTime が T より後の修正は T 時点では使わず、基準日を後ろにすると使う |
| 基準日より後の訂正を除外 | `test_correction_after_T_excluded` | `submitted_after_T` で除外し原本を採用。基準日を後ろにすると訂正を採用 |
| 基準日より後の取下げが T 時点の判定に影響しない | `test_withdrawal_after_T_not_applied` | 取下書が T 後なら T 時点では有効（`withdrawal_after_T` を記録）。基準日を後ろにすると除外 |
| xbrlFlag=0 の訂正だけ → 直前の版＋`correction_unparsed` | `test_unparsed_correction_uses_previous_version`、`test_unparsed_correction_after_parsed_one` | 当期・前期とも原本を採用して `correction_unparsed`。解析できる訂正の後に xbrlFlag=0 の訂正がある場合は、解析できる最新の訂正を採用 |
| 702001 を版として扱う | `test_xbrl_fix_702001_is_a_version`、`test_xbrl_fix_702001_without_text_is_pending_not_partial` | 702001 が同じ版グループに入り `version_kind=xbrl_fix` で採用。本文が未取得なら `texts_pending` で、`correction_partial` にはしない |
| TextBlock を欠く・断片だけの訂正 → `correction_partial` | `test_partial_correction_falls_back_to_full_version`（TextBlock なし／断片の2件）、`test_partial_correction_keeps_latest_full_correction` | 完全な本文を持つ最新の版に戻り、訂正版を `partial_corrections` に記録。完全な訂正の後の断片（702001）なら、完全な訂正を採用して `risk_changed_by_correction` も付く |
| 前年が欠け2年前だけ → 比較せず `prev_missing_year` | `test_prev_missing_year_not_compared` | 前期の doc_id は空、`no_prev_reason=prev_missing_year`、gap 366日、外した2年前の docID を記録 |

**実装の修正はありません**（全項目が既存の実装で期待どおりでした）。テスト作成中の失敗1件はテスト側の誤り（修正後の periodEnd が窓内だった）で、テストを直しました。

**要確認（仕様の値）**: 不開示の除外は計画 §6-5 のとおり `disclosureStatus == "2"` で判定し、テストもそれに合わせています。ただし EDINET API 仕様書では開示不開示区分が「1: 不開示を開始／2: 不開示を解除／3: 不開示中」と定義されている可能性があり、その場合は除外すべき値が異なります。手元に仕様書が無く確認できていません。実データの書類一覧（99,946行）はすべて `0` で、現時点の結果には影響しません。全社実行の前に仕様書で確認します。

## 4. CLI 一覧（現時点）

| コマンド | 状態 |
|---|---|
| `init-db [--config] [--db]` | M1。v2・v3 のマイグレーションを含む |
| `codes sync [--refresh] [--from-file] [--aggregation-file] [--cache] [--db]` | M2 第1バッチ |
| `population load --jpx <xlsx> --as-of-month YYYY-MM [--db]` | M2 第1バッチ |
| `doclist sync (--from D [--to D] \| --daily \| --rescan-month [--window ...] \| --from-cache) [--refresh] [--dry-run] [--offline] [--db] [--cache]` | M2 第1バッチで拡張 |
| `resolve --fy YYYY-MM --as-of D [--entity CODE… \| --targets TOML \| --transport7] [--as-of-month] [--successor-map] [--db] [--cache] [--out]` | M2 第2バッチ Step F（既定は母集団全社。オフライン） |
| `fetch [--entity … \| --targets \| --transport7 \| --all] [--run-id] [--retry-failed] [--max-attempts] [--dry-run] [--offline]` | M2 第2バッチ Step G |
| `monitor [--run RUN_ID] [--fail-on-threshold] [--thresholds TOML] [--since ISO] [--db] [--out]` | M2 第2バッチ Step H（オフライン） |
| `m1_resolve`（`resolve` 経由）・`m1_segment`・`m1_match`・`compare`・`report`・`run` | M1（7社用）。抽出〜比較の全社化は次バッチ以降 |
| `m0_spike`・`m0_truncation`・`m0_multi`・`m0_scan` | M0 |
| `gold eval`・`selftest` | 未実装 |

共通オプション: `--offline`（ネットワークを禁止。違反時は終了コード 3）、`--log-level`。

## 5. 受け入れ条件（計画 §4 M2）との対応

**M2 の合格は宣言しません。** 判定できたのは条件4だけです。人手のラベル・確認が要る条件（5・10・12・13）は、仕組みが整っても合格とはしません。

| # | 条件（要約） | 状況 | メモ |
|---|---|---|---|
| 1 | 突合率 ≥ 99%、残りに理由、`unknown` は人が全件確認 | 一部 | 採用版あり 1,531／1,543（99.2%）。ただし取得・抽出が未実施で、M2-1 の突合率（monitor の `match_rate`）は 0%。突合外 12社の理由は `late` 6・`unknown` 6（持株会社化、§6-1）。unknown の人手確認は未実施 |
| 2 | 取得失敗・空抽出が0件、または理由付きで監視に載る | 一部 | 再試行キュー（Step G）と monitor の理由付き一覧（Step H）は実装・テスト済み。全社の fetch・抽出は未実行 |
| 3 | 前期なしの理由別件数、`unknown` は人が確認、変則・`prev_gap` の一覧 | 一部 | 理由別件数・`irregular` 4社の一覧は出力済み。前期なしの `unknown` 439 はほぼ未走査窓が原因（§6）で、走査後に再判定してから人が確認 |
| 4 | 合成データのテスト（9項目） | **充足（合成テスト）** | `tests/test_m2_synthetic.py` 15件で全項目を DB 経由で確認（Step I）。不開示の値の意味は要確認（Step I の注記） |
| 5 | 層別サンプル評価用で適合率・再現率・境界F1、閾値は調整用だけで | 人手待ち | ラベル付け担当者の確保（B7）。合格宣言しない |
| 6 | 間隔 ≥ 1秒・並列1、429/5xx で60秒からの指数バックオフ、ログにキーなし | 一部 | 書類一覧・書類取得の両方で、間隔・バックオフ秒列 [60,120,240,480]・401/403 即停止・ログ等にキーが出ないことをテスト済み。全社実行のログを grep する確認は未実施 |
| 7 | 強制終了後の再実行で再取得せず完了し、結果が一致 | 一部 | docID ごとのコミット・sha256 一致で再取得しない・キャッシュ救済をテスト済み。全社 fetch での強制終了試験は未実施 |
| 8 | 監視レポート（Excel・CSV）と `monitor --fail-on-threshold` | 実装済み（判定は全社実行後） | 実DBで終了コード2を確認。L2（タクソノミの年次確認）と LLM 棄却率（M3）は値なしの行。全社実行後の数値で改めて確認 |
| 9 | 初回12時間以内、日次差分30分以内 | 未着手 | 全社実行で計測。fetch だけで約43分の見込み（§7） |
| 10 | 訂正の実例10件が人の判断と一致（10/10） | 人手待ち | 候補は訂正ありで本文未取得の 158社。全社 fetch 後に再判定してから抽出。合格宣言しない |
| 11 | 120 があるのに採用版が無い会社が0件、または理由付きで人が確認 | 一部 | `form_out_of_scope` の判定は実装済み（実DBでは 0件）。`filing_filter.toml` の M2 版が未整備 |
| 12 | 理由ごとに無作為5件を人が確認 | 人手待ち | 未走査窓を埋めて理由が確定してから。合格宣言しない |
| 13 | 3万字超の3社で末尾段落が閲覧画面と一致（全社実行の前） | 人手待ち | 対象3社の選定と閲覧画面・PDF との照合。合格宣言しない |

## 6. 未走査の書類一覧の窓と unknown 445 の内訳

`doclist_raw` の対象日は 2025-06-02〜2026-10-27 のうち 264日分です。FY 2026-03（T=2026-09-30）の判定に要る窓のうち、次がまだ走査されていません。

| 窓 | 期間 | 影響 |
|---|---|---|
| 当期の提出日窓の前側 | 2025-04-01〜2025-05-31 | 2〜3月決算の当期・前期の有報（前期分は下記と重なる） |
| 当期の提出日窓の後ろ側 | 2026-07-01〜2026-10-25 | 提出遅延6社の T までの提出の有無、T 前後の訂正・取下げ・修正（T 後のものは T 時点の判定に入らないが、`state_as_of` の根拠になる） |
| 前年度（FY 2025-03）の提出日窓 | 2024-04-01〜2025-05-31（2025-06-01 以降は走査済み） | 非3月決算の前期の有報。unknown 439件が指す未走査日は 2024-05-01〜2025-05-30 に収まる（月別の件数は 2025-01 が 368、2025-03 が 138、2024-10 が 78 など） |

API 呼び出しは 2024-04-01〜2025-05-31（426日）と 2026-07-01〜2026-10-25（117日）で約540回（1秒間隔で10分強）の見込みです。

unknown 445 の内訳（resolve run `resolve-20261028T151259-ac31`）:

| 区分 | cause | 件数 | 内容 |
|---|---|---|---|
| 前期なし | `doclist_not_scanned` | 439 | 前期の有報があるべき提出日が未走査。当期の決算月は 12月 183、2月 75、9月 39、6月 33、5月 24、1月 17、8月 17、11月 16、4月 12、10月 12、7月 10、3月 1。このうち 2件（E05155、E35928）は2年前（2023-12期）の有報が見つかっており、走査しても前年が無ければ `prev_missing_year` になる（`would_be`） |
| 突合 | `no_annual_report_history` | 6 | 2026年に持株会社化した6社。有報の履歴も決算日以前の書類もなく期限を決められない。人の確認が必要（新設会社として `new_listing` 相当に扱うか、承継の登録か） |

## 7. 人手待ち

- **M2-5 層別サンプル**: 40〜60社のラベル付け（業務側）と、調整用・評価用の分割。担当者の確保（B7）が前提です。
- **M2-10 訂正の実例10件**: 年度窓で訂正（130・702001）がある会社から10件以上を選び、採用 docID とフラグを人が判断します。全社 fetch 後に `texts_pending` を解消してから抽出します。
- **M2-12 理由のサンプル確認**: 突合・前期なしの理由ごとに無作為5件を確認します。未走査窓を埋めて理由が確定した後に行います。
- **M2-13 末尾段落**: 3万字超の3社（体裁の異なるもの）で、抽出本文の末尾段落と閲覧画面（または PDF）の末尾段落を照合します。全社実行の前に行います。
- **unknown の全件確認（M2-1・M2-3）**: 走査後に残った unknown（少なくとも持株会社化の6社）。
- **successor_suspected 2社（ブルーゾーンHD、ARCHION）**: 確認して `config/successor_map.csv` へ登録するか決めます。
- **仕様の確認**: `disclosureStatus` の値の意味（Step I の注記）。

## 8. 既知の注意点

- **5桁コードの除外**: JPX 一覧のプライムのうち5桁コードの7行（優先株・社債型種類株式）は、母集団が会社単位のため除外しています。
- **上場中で証券コードが空の3社**: 今回の母集団の結合には影響しません（不一致0件）。
- **E38709**: 集約先がコード一覧に無くても読み替えます（Step F-2）。今回の突合には影響しません。
- **`state_as_of` の制約**: `doclist_raw` の全行が同じ `fetched_at`（2026-10-28T00:01）で、T 以前のスナップショットはありません。T 時点の状態は opeDateTime と取下書の提出日時から再構成しています。取下書が未走査の範囲にある場合は `withdrawal_time_unknown`（保守的に取下げ扱い）になるため、未走査窓の走査でこれも減ります。
- **rescan で行が増える**: `doclist_raw` は追記のみで、`processDateTime` が変わると再走査のたびに窓全体が追記されます。容量の見積りと古いスナップショットの扱いは運用開始前に決めます。
- **本体の置き場所**: `cache/docs/{docID}_type1.zip`（M0/M1 と同じ）。計画 §3 の `raw/` への移行は未実施です。

## 9. 次バッチ案

1. **未走査窓の `doclist sync`**: `doclist sync --from 2024-04-01 --to 2025-05-31` と `--from 2026-07-01 --to 2026-10-25`（約540回の API 呼び出し、静穏時間帯 00:00〜01:00 を避ける）。
2. **`resolve` の再実行**: unknown 439（`doclist_not_scanned`）と提出遅延6社の再判定。entity_id・理由が変わった会社を前回 run と比較して記録します。
3. **全社の `fetch --all`**: 取得が必要な docID は約2,600件で、1秒間隔で約43分以上。途中で一度強制終了して再実行し、再取得しないこと・結果の一致を確かめます（M2-7）。全ログのキー grep（M2-6）と所要時間の計測（M2-9）もここで行います。
4. **抽出**: 全社の type=1 から BusinessRisks を抽出して `extracts` に保存し、`resolve` を再実行して `texts_pending` 158社の `correction_partial`・`risk_changed_by_correction` を判定します。
5. **`monitor --fail-on-threshold`**: 全社の数値で `match_rate`・空抽出・取得失敗・unknown を確認し、超過分を理由付きで一覧にします。
6. **`config/filing_filter.toml` の M2 版**: `version` を上げ、`candidates`（130・030001・702001）を正式な絞り込みに移し、対象外の様式の判定（M2-11）の根拠を書きます。
7. 並行して人手の作業（§7）。M2-13 の3社は全社実行の前に照合します。

## 10. テスト結果

```
.venv/bin/pytest -q -m "not live"
343 passed, 1 skipped, 2 deselected in 18.56s
```

- 第1バッチ終了時の 276 から 67件増えました（増分は Step F 18、G 20、H 14、I 15。G は `test_fetch.py` 21件の追加と `test_store.py` の v3 対応の差し引き）。
- skipped 1: `tests/test_match.py`。scipy が未導入のため、ハンガリアン法のオラクル比較を skip しています（M1 と同じ）。
- deselected 2: live テストです（ネットワークを使うため除外）。
