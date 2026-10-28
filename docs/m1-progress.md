# M1 進捗（7社の再現）

作成日: 2026-10-28（同日 Step J で更新）。対象: 輸送用機器7社（`config/transport7.toml`）× 2期（2025年3月期・2026年3月期）、基準日 T=2026-09-30。
参照: `/workspace/edinet-risk-review/implementation-plan.md` §3・§4 M1・§5・§6-5・§6-6・§7。M0 の成果は `docs/spike-findings.md`・`out/m0/`。

**状況の要約**: 版の選択 → 書類取得（キャッシュ優先）→ 項目分割 → 対応付け → 文字差分 → SQLite 保存 → Excel・HTML・照合シートまでを、`run --offline` の一括実行で7社分通しました。
**手作業 Excel は不要**です（2026-10-28 侑輝指示）。正解（gold）が必要な受け入れ条件 1〜3 と 8 は M1 の**対象外**とし、評価しません。
冪等性（条件5）は7社の実データで確認しました。`run --offline` を連続で実行し、SQLite のテーブル・Excel のセル・HTML・テキスト出力のハッシュがすべて一致し、書類取得 API の呼び出しは 0 回でした（§2-5）。
M2 には着手していません。

## 1. 実装したモジュール

依存は §7 の範囲（標準ライブラリ＋openpyxl）。scipy はテストのオラクルとしてだけ使い、未導入なら該当テストを skip します。

| 区分 | モジュール | 内容 |
|---|---|---|
| ingest | `ingest/doclist.py` | 保存済み doclist（cache）の取り込みとマージ（M1 用の最小実装） |
| resolve | `resolve/versions.py` | 版グループの構成と採用版の選択（§6-5、基準日 T で xbrlFlag=1 の最新版）。訂正の periodEnd は parentDocID から継承 |
| resolve | `resolve/period.py` | 当期・前期の特定、gap、前期なしの理由 |
| segment | `segment/headings.py`, `segment/segmenter.py` | 見出し候補の検出と項目分割（最下位の見出し＝項目、決定事項 D2）。番号の体系 (1)/（１）・①・１． の階層 |
| match | `match/ngram_tfidf.py` | 文字 2〜3gram の TF-IDF とコサイン類似度 |
| match | `match/hungarian.py`, `match/lis.py` | 純 Python のハンガリアン法と LIS（移動の判定） |
| match | `match/matcher.py` | 二段方式（アンカー → 割当 → 包含で分割・統合 → 新設・削除 → 移動）。閾値は `config/thresholds.toml`（version=`m1-initial`） |
| diff | `diff/chardiff.py`, `diff/minor.py`, `diff/sentences.py` | 文字差分、軽微な変更の判定（D4）、文の分割と対応付け |
| report | `report/excel.py`, `report/html.py`, `report/reconcile.py` | 会社別の差分 Excel（概要・会社別・照合・出典の各シート）、全文差分 HTML、照合シート |
| store | `store/schema.sql`, `store/migrate.py`, `store/db.py` | SQLite スキーマ（§5）、前進のみのマイグレーション（`PRAGMA user_version`）、WAL・外部キー・upsert |
| net | `net/http.py`, `net/edinet_api.py` | EDINET API v2 クライアント（標準ライブラリのみ）。API キーはログ・出力・キャッシュに出さない |
| CLI | `m1_resolve.py`, `m1_segment.py`, `m1_match.py`, `m1_compare.py`, `m1_report.py`, `m1_run.py` | `python -m edinet_risk {m1_resolve, m1_segment, m1_match, compare, report, run, init-db}`。`compare --db` は SQLite にも保存（Step H）。`run` は一括実行で、`--offline` 付きならネットワークを禁止（Step I） |
| tools | `tools/gold_import.py` | 手作業 Excel → 正解 JSON。**インタフェースとスタブのみ**。手作業 Excel は不要になったため、M1 では使いません（`--gold` は任意の引数として残しています） |

## 2. 7社の結果（out/m1）

### 2-1. 版選択・前期特定（`out/m1/resolve_transport7.md`）

保存済み doclist 395日分（99,525件）から選択しました。docID は手で固定していません。
7社とも、当期は 2026年3月期の原本です。前期は 2025年3月期で gap=1、flags はありません。
SUBARU の前期だけは訂正版 **S100YF9D**（原本 S100W1AE）を採用しました（§3 を参照）。

### 2-2. 項目分割（`out/m1/segment_summary.md`、`seg-1`）

| 会社 | 2025年3月期 項目数 | 2026年3月期 項目数 | 番号の体系 |
|---|---|---|---|
| トヨタ自動車 | 16 | 16 | paren_num > circled |
| 本田技研工業 | 16 | 16 | paren_num > circled |
| 日産自動車 | 24 | 24 | num_dot > paren_num |
| SUBARU | 20 | 23 | paren_num → paren_num > circled（当期に体系が変わった） |
| マツダ | 17 | 17 | paren_num |
| 三菱自動車工業 | 18 | 18 | paren_num > circled |
| スズキ | 20 | 20 | paren_num > circled |

- 14件すべてが分割=normal です。照合シートの「全文一致」（抽出本文 → 分割 → 差分表示の間で文字が欠けていないこと）も 14/14 で ○ でした。
- 要確認（自動検出）:
  - 本田技研工業: 子を持つ見出しに自身の本文があります（「(1) 地政学的リスク」に 531字）。この本文は親の導入文として保持し、項目には含めていません。
  - SUBARU 2025年3月期: 前文が 2,070字と長いです。番号なしの見出しの下にある本文が、前文に入っている可能性があります。
  - SUBARU 2026年3月期: 「(4) 主要な事業等のリスク」に、導入文 179字があります。

### 2-3. 対応付け（`out/m1/match_summary.md`、`match-1`、当期 × 前期）

| 会社 | 当期/前期項目 | same | modified | retitled | moved | added | 要確認 | 番号変更 |
|---|---|---|---|---|---|---|---|---|
| トヨタ自動車 | 16/16 | 9 | 7 | 0 | 0 | 0 | 0 | 0 |
| 本田技研工業 | 16/16 | 7 | 8 | 0 | 1 | 0 | 0 | 5 |
| 日産自動車 | 24/24 | 12 | 12 | 0 | 0 | 0 | 0 | 0 |
| SUBARU | 23/20 | 0 | 20 | 0 | 0 | 3 | 1 | 0 |
| マツダ | 17/17 | 10 | 6 | 1 | 0 | 0 | 0 | 0 |
| 三菱自動車工業 | 18/18 | 13 | 5 | 0 | 0 | 0 | 2 | 0 |
| スズキ | 20/20 | 15 | 5 | 0 | 0 | 0 | 0 | 0 |
| **計** | 134/131 | 66 | 63 | 1 | 1 | 3 | 3 | 5 |

- merged・split・deleted は 0件でした。段階の内訳は アンカー 126／割当 5／包含 0 です。
- 要確認になったのは次の3件です。いずれも見出しは同一で、本文類似度が低いものです。
  - SUBARU「③金融市場の変動」（S=0.475）
  - 三菱自動車工業「（１）市場環境変化の影響」（S=0.430）
  - 三菱自動車工業「①営業戦略…」（S=0.405）
- 本田技研工業の「市場環境変化リスク」は (9)→(4) に移ったため moved と判定しました。マツダの「(7) 情報技術への依存」は retitled です。
- SUBARU では、当期に新設された冒頭のリスクマネジメント3項目が added になりました。

### 2-4. 差分・帳票（最新は `out/m1/run_20261028T130444-c58e/`）

- `compare.json` の各バージョン: compare-1／seg-1／match-1／diff-1／minor-1。閾値の fingerprint は `d3902d46d8d3c146` です。
- ペアごとの変更判定（changed／minor／none）と変更箇所数:

  | 会社 | changed | minor | none | 変更箇所 |
  |---|---|---|---|---|
  | トヨタ自動車 | 6 | 1 | 9 | 14 |
  | 本田技研工業 | 5 | 4 | 7 | 32 |
  | 日産自動車 | 12 | 0 | 12 | 44 |
  | SUBARU | 23 | 0 | 0 | 223 |
  | マツダ | 7 | 0 | 10 | 19 |
  | 三菱自動車工業 | 5 | 0 | 13 | 19 |
  | スズキ | 5 | 0 | 15 | 11 |

  minor はすべて番号だけの変更です。
- 帳票:
  - `risk_diff_transport7.xlsx`: 概要・会社別7枚・照合・出典の各シートがあります。1セルの最大は 1,787字で、32,767字以内です。スコア・ラベル・段階は別の列に出しています。
  - `html/`: 会社別の全文差分7ファイルと index.html です。
  - `reconcile.csv` / `reconcile.md`: 照合シートです。「全文一致」は 14/14 で ○ です。「手作業」の列は空で、判定は「対象外」です。

### 2-5. SQLite 保存（Step H）と `run --offline`・冪等性（Step I）

**SQLite（`out/m1/m1.sqlite`、スキーマ user_version=1）の件数**（最新の run 時点）:

| テーブル | 件数 | 内容 |
|---|---|---|
| doclist_raw | 99,525 | 保存済み doclist 395日分 |
| documents | 99,020 | docID 単位 |
| filing_selection | 14（run ごと） | 7社×2期の採用版。過去の resolve・run の分を含めた累計は 112 行 |
| extracts | 14 | 7社×2期の抽出本文。cache の type=1 ZIP から再抽出した本文と一致（警告なし） |
| items | 316 | seg-1 の見出しノードすべて（項目＝葉 265＝当期 134＋前期 131、残りは親の見出し）。分割はすべて normal |
| matches | 134 | 当期×前期の対応ペア（match-1） |
| sentence_changes | 362 | 文単位の変更。根拠ID（evidence_id）から抽出本文の該当箇所を復元できることをテストで確認 |
| runs | 12 | resolve 4・compare 1・run 7 |
| api_calls・fetches | 0 | 14件の ZIP はすべて cache 済みのため、取得なし |
| code_aggregation・edinet_codes・successor_map・metrics | 0 | M1 では使いません（M2 以降） |

**`python -m edinet_risk run --offline`**: resolve → fetch → compare → report を1つの run_id で実行し、`out/m1/run_{run_id}/` に resolve・compare.json・帳票・`fingerprint.json` を出力します。
`--offline` のときはネットワーク接続を禁止します（接続しようとすると `NetworkForbidden`）。cache に無い ZIP は取得せず、missing として報告します。
7社では毎回 `fetch: cached=14 fetched=0 missing=0`、`api_calls: documents=0 documents.json=0（network calls=0）` でした。

**冪等性（M1-5）**: `fingerprint.json` で、(a) テーブルごとの行ハッシュ（run_id の列を除く。runs・api_calls・metrics は対象外）、(b) Excel のセル値と書式のスナップショット、(c) HTML、(d) テキスト出力（run_id はマスク）を比較します。xlsx のバイナリは比較しません。

| run_id | tables | excel | html | 前の run との比較 |
|---|---|---|---|---|
| 20261028T125952-d7cf ほか計4回 | 42a6d0f8faeb | d76eef9f433b | ab588781a083 | 4回とも一致 |
| 20261028T130422-6e3f | 42a6d0f8faeb | b15be09bebd4 | ab588781a083 | excel・reconcile のみ相違（照合の判定ラベルを「未受領」→「対象外」に変えたため。意図した変更） |
| 20261028T130430-a8fd | 同上 | 同上 | 同上 | 一致（tables・excel・html・files） |
| 20261028T130444-c58e | 同上 | 同上 | 同上 | 一致（tables・excel・html・files）。**最新** |

ラベルを変えた前後で、テーブルと HTML のハッシュは変わっていません。
同じ確認は `tests/test_run.py::test_transport7_run_offline_twice`（7社、一時ディレクトリで2回実行）でも自動で行っています。

`out/m1/` には、ラベル変更前の run ディレクトリ（`run_20261028T121739`、`run_20261028T125445`、`run_20261028T1259*`〜`T130234*`）も残しています。これらの照合シートの「未受領」は、現在の「対象外」と同じ意味です（`out/m1/README.md`）。

## 3. SUBARU 訂正報告書の結論（`out/m0/subaru_amendment_note.md`）

**risk_changed_by_correction = False** です（SUBARU 2025年3月期）。

- 版グループは S100W1AE（原本）と S100YF9D（訂正、2026-06-22付け）です。§6-5 の規則に従い、訂正 S100YF9D を採用しました（version_kind=correction）。
- 訂正 ZIP は全文を再提出したインライン XBRL です。BusinessRisksTextBlock は1件で、原本と完全に一致しました（12,133字、正規化後 11,976字）。
- 訂正の理由は「セグメント情報」の注記と監査報告書の記載誤りで、事業等のリスクは訂正の対象外です。
- 全文の TextBlock があるため `correction_partial` は付きません。
- S100YF9D の type=1 ZIP は API で1回だけ取得し、`cache/docs/` に保存しました。それ以降はオフラインで処理しています。
- この件は「訂正あり・本文不変」の回帰ケースとして使えます。

## 4. 受け入れ条件の状況と残課題

| 受け入れ条件（§4 M1） | 状況 |
|---|---|
| 1. 照合 14/14・境界F1 ≥ 0.95 | **対象外**（手作業 Excel 不要の指示による）。代わりに自動の「全文一致」14/14 と、分割の lossless（前文＋全項目＝抽出本文）を確認済みです |
| 2. 適合率 ≥ 0.95・再現率 ≥ 0.90・混同行列 | **対象外**（正解ペアを作らないため）。`match_summary.md` はラベル分布の確認用です |
| 3. 変更あり/なしの100%一致・色付け範囲F1 | **対象外**（同上） |
| 4. Excel のセル上限・全文 HTML・出典シート | 達成（最大 1,787字、HTML あり、出典シートあり） |
| 5. 冪等性 | **達成**。7社の実データで、テーブル・Excel・HTML・テキストのハッシュが一致し、2回目以降の書類取得 API は 0 回でした（§2-5） |
| 6. `--offline` での再現 | **達成**。`run --offline` でネットワークを禁止した状態で7社を再現しました（§2-5） |
| 7. スコア・ラベル・段階を別列に出すこと | 達成 |
| 8. `tests/golden/seven/` の固定と CI 回帰 | **対象外**（正解 JSON を作らないため）。7社の回帰は、cache がある環境では `test_transport7_into_sqlite`・`test_transport7_run_offline_twice` で走ります（cache が無い環境では skip） |

残課題（M1 の受け入れを止めるものではありません）:

- 要確認の項目は、まだ人が見ていません。分割では SUBARU 2025年3月期の前文 2,070字など（§2-2）、対応付けでは本文類似度が低い3件（§2-3）です。
- `tools/gold_import.py` はスタブのままです。M1 では使いません。
- ハンガリアン法の scipy オラクル比較は、scipy が未導入のため skip しています。
- M0 からの持ち越し: 社内PCでの実機確認（M0 条件1・ブロッカー B5）と、702001 の実例 docID（M0 条件8）。詳細は `docs/spike-findings.md` §6 にあります。

## 5. 次のアクション

**M2 は未着手です。**

1. M1 の受け入れを判断します（条件 4〜7 は達成、1〜3・8 は対象外）。
2. 必要に応じて、要確認の項目（SUBARU 2025年3月期の前文、類似度が低い3件）を人が確認します。結果によっては seg-2／thresholds の版を上げます（閾値の本格的な調整は M2 の層別サンプルで行います）。
3. M0 の持ち越し（社内PCでの実機確認、702001 の実例）を進めます。
4. 受け入れが決まったら M2 に進みます。

（2026-10-28 追記）M2 第1バッチ着手（docs/m2-progress.md）

## 6. テスト結果（最終）

```
.venv/bin/pytest -q -m "not live"
229 passed, 1 skipped, 2 deselected in 14.91s
```

- 前回（Step G 時点）の 212 passed から、Step H の `tests/test_compare_db.py`（8件）と Step I の `tests/test_run.py`（9件）で 17件増えました。

- skipped 1: `tests/test_match.py:123`。scipy が未導入のため、ハンガリアン法のオラクル比較を skip しています。
- deselected 2: live テストです（ネットワークを使うため除外）。
