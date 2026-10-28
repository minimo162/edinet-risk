# JPX 母集団（`population load`）

## 入手

1. https://www.jpx.co.jp/markets/statistics-equities/misc/01.html の「東証上場銘柄一覧」から `data_j.xlsx` をダウンロードする（2026-10-28 時点で xlsx。月末時点の一覧が翌月初に更新される）。
2. `ref/jpx/data_j_YYYYMM.xlsx` に保存し、`ref/jpx/SOURCE.md` に URL・日付・取得日・sha256 を追記する。
3. 旧形式の `data_j.xls`（OLE2）しか手に入らない場合は、Excel または LibreOffice で開いて `.xlsx` で保存し直す
   （例: `soffice --headless --convert-to xlsx data_j.xls`）。xlrd は依存に入れない。

## 取込み

```
python -m edinet_risk codes sync            # 先に edinet_codes を入れておく
python -m edinet_risk population load --jpx ref/jpx/data_j_202609.xlsx --as-of-month 2026-09
```

- `--as-of-month` は必須（YYYY-MM）。一覧の `日付` が別の月ならエラー。
- `市場・商品区分 == プライム（内国株式）` の行だけを入れる。5桁コードの優先株・社債型種類株式は除外して件数を表示する（普通株は4桁コードで別行にある）。
- EDINET 結合: `edinet_codes.sec_code5 == sec_code4 || '0'`。
  `sec_code`（候補1件）／`sec_code_listed`（複数候補のうち上場が1件）／`ambiguous`（決められない、edinet_code NULL）／`unmatched`（候補なし、edinet_code NULL＝コード不一致 §6-5）。複数候補と不一致は一覧表示する。
- 同じ基準月の再ロードは、その月の行を1トランザクションで置き換える（冪等）。
