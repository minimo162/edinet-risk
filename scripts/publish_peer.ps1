<#
.SYNOPSIS
  週次公開（docs/publish_peer.spec.md）。対象社（publish.targets = transport7 + 依頼社リスト）について
  doclist → resolve → fetch → extract → resolve → compare/report → monitor を既存 CLI で実行し、
  出力を正規名で publish/ に置いて、SharePoint の同期フォルダ（publish.dest_dir）へコピーする。

.DESCRIPTION
  - API キーは EDINET_API_KEY 環境変数か settings の paths.api_key_file からだけ読む（このスクリプトは扱わない）。
  - 成功の定義（spec §2-5）: publish/ に5種がそろい、社一覧.xlsx と monitor.xlsx が当日付。
    1つでも欠けたら同期フォルダへはコピーしない（前回分が残る）。
  - 取得ゲート（spec §2-3）: doclist sync が 0 以外で終わったら公開しない。fetch が終了コード 2 のとき、
    新規取得（fetched + adopted_from_cache）が 0 件なら公開しない。どちらも終了コード 1（同期しない）。
  - 設定ゲート: -DryRun でも -NoSync でもないのに settings が見つからない、または publish.dest_dir が空なら
    何も実行せず終了コード 1（公開したつもりで何も置かれない状態を成功にしない）。
  - 基準日T: resolve には publish config の as_of（publish.as_of。既定 "today" = 当日 JST）を --as-of で渡す。
    社一覧.xlsx の「基準日T」列と週次要約.md の見出しに出る。
  - 書類一覧の範囲: publish config の doclist_from..doclist_to（まだ確定していない最も古い日から当日まで。
    休んだ週の分も取り直す。上限 publish.doclist_max_days 日）。
  - 同期フォルダへは上書きコピーのみ（削除→新規作成にしない。リンクが変わるため）。
  - 手順の詳細は docs/admin-runbook.md。PowerShell 5.1 で動く書き方にしている（UTF-8 BOM 付きで保存すること）。

.PARAMETER DryRun
  実行するコマンドとコピー先を表示するだけ（CLI は実行せず、ファイルも書かない。設定の読み取りだけ行う）。
.PARAMETER Config
  settings TOML（既定: config\settings.toml）。
.PARAMETER Dest
  公開先の同期フォルダ（既定: settings の publish.dest_dir）。
.PARAMETER Python
  python.exe（既定: リポジトリの .venv\Scripts\python.exe、なければ PATH の python）。
.PARAMETER SkipDoclist
  書類一覧の同期を省く（キャッシュと DB にある書類だけで回す）。
.PARAMETER NoSync
  publish/ を作って点検するところまで。同期フォルダへはコピーしない（settings・dest_dir がなくてもよい）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_peer.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Config = "",
    [string]$Dest = "",
    [string]$Python = "",
    [switch]$SkipDoclist,
    [switch]$NoSync
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
[Environment]::CurrentDirectory = $Root   # [IO.Path] の相対パスもリポ根基準にする
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }   # コンソールなし（タスク実行）でも続ける

if (-not $Python) {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venv) { $Python = $venv } else { $Python = "python" }
}
$CfgArgs = @()
if ($Config) { $CfgArgs = @("--config", $Config) }

$Stamp = Get-Date -Format "yyyyMMdd'T'HHmmss"
$Script:Log = $null
$Script:Warnings = New-Object System.Collections.Generic.List[string]
$Script:CliOut = New-Object System.Collections.Generic.List[string]   # 直前の Invoke-Cli の出力行
$Script:LastRc = 0
# fetch の集計行（__main__._fetch の print と一致させる。tests/test_peer_publish.py で照合）
$FetchSummary = 'fetched=(\d+) adopted_from_cache=(\d+) failed=(\d+)'

function Write-Log([string]$Text) {
    Write-Host $Text
    if ($Script:Log) { Add-Content -LiteralPath $Script:Log -Value $Text -Encoding UTF8 }
}

function Format-Args([string[]]$CliArgs) {
    ($CliArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join " "
}

# 既存 CLI を1つ実行する。終了コード 0 と WarnCodes 以外で止める。出力行は $Script:CliOut に残す。
function Invoke-Cli([string]$Step, [string[]]$CliArgs, [int[]]$WarnCodes = @()) {
    Write-Log ("[{0}] {1} -m edinet_risk {2}" -f $Step, $Python, (Format-Args $CliArgs))
    $Script:CliOut.Clear()
    $Script:LastRc = 0
    if ($DryRun) { return }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # PowerShell 5.1: 標準エラーの行で止めない
    & $Python -m edinet_risk @CliArgs 2>&1 | ForEach-Object { $Script:CliOut.Add("$_"); Write-Log ("    " + $_) }
    $rc = $LASTEXITCODE
    $Script:LastRc = $rc
    $ErrorActionPreference = $prev
    if ($rc -eq 0) { return }
    if ($WarnCodes -contains $rc) {
        $Script:Warnings.Add("$Step exit $rc")
        Write-Log ("    -> 警告: 終了コード {0}（続行）" -f $rc)
        return
    }
    throw ("{0} が終了コード {1} で失敗しました。同期フォルダは更新しません（前回分が残ります）。" -f $Step, $rc)
}

# fetch のゲート: 終了コード 2 で新規取得 0 件（または集計行なし＝設定・DB の誤り）なら止める。
function Assert-FetchGate {
    if ($DryRun) { return }
    $m = $null
    foreach ($ln in $Script:CliOut) { if ($ln -match $FetchSummary) { $m = $Matches } }
    if ($null -eq $m) {
        if ($Script:LastRc -ne 0) { throw "fetch が集計前に失敗しました（ログを確認）。同期フォルダは更新しません（前回分が残ります）。" }
        return
    }
    $new = [int]$m[1] + [int]$m[2]
    Write-Log ("    -> 新規取得 {0} 件（fetched {1} + adopted_from_cache {2}）、失敗 {3} 件" -f $new, $m[1], $m[2], $m[3])
    if ($Script:LastRc -ne 0 -and $new -eq 0) {
        throw "fetch: 新規取得 0 件でエラーがあります。古いデータを当日分として公開しないため止めます（前回分が残ります）。"
    }
}

# JSON を出す helper（publish config / publish build）を実行して結果を返す。
function Invoke-Json([string]$Step, [string[]]$CliArgs) {
    Write-Log ("[{0}] {1} -m edinet_risk {2}" -f $Step, $Python, (Format-Args $CliArgs))
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $Python -m edinet_risk --log-level WARNING @CliArgs
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rc -ne 0) { throw ("{0} が終了コード {1} で失敗しました。" -f $Step, $rc) }
    return (($out | Out-String) | ConvertFrom-Json)
}

# 同期フォルダへ上書きコピー（削除しない）。
function Copy-Over([string]$From, [string]$To) {
    Write-Log ("[copy] {0} -> {1}" -f $From, $To)
    if ($DryRun) { return }
    $dir = Split-Path -Parent $To
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Copy-Item -LiteralPath $From -Destination $To -Force
}

$exit = 0
try {
    # ---- 0. 設定（キーそのものは読まない・表示しない）
    $cfg = Invoke-Json "config" (@("publish", "config") + $CfgArgs)
    $Out = $cfg.out_dir
    $Work = $cfg.work_dir
    $Cache = $cfg.cache_dir
    if (-not $Dest) { $Dest = $cfg.dest_dir }
    $Targets = Join-Path $Out "targets.toml"
    $R1 = "resolve-$Stamp-pre"   # fetch の対象を決める resolve
    $R2 = "resolve-$Stamp-pub"   # 本文取得後の resolve（公開に使う）
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path (Join-Path $Out "logs") -Force | Out-Null
        $Script:Log = Join-Path $Out ("logs\publish_peer_{0}.log" -f $Stamp)
    }
    Write-Log ("publish_peer {0}{1}: settings={2} out={3} publish={4} dest={5}" -f $Stamp,
        $(if ($DryRun) { " (DryRun)" } else { "" }), $(if ($cfg.settings) { $cfg.settings } else { "(既定値)" }),
        $Out, $Work, $(if ($Dest) { $Dest } else { "(未設定: コピーしない)" }))
    Write-Log ("API キー: {0}" -f $cfg.api_key_source)
    Write-Log ("基準日T: {0}（publish.as_of。settings の run.as_of は {1}）" -f $cfg.as_of, $cfg.run_as_of)
    foreach ($w in @($cfg.warnings)) {
        if (-not $w) { continue }
        $Script:Warnings.Add($w)
        Write-Log ("    -> 警告: {0}" -f $w)
    }
    if (-not $DryRun -and -not $NoSync) {
        if (-not $cfg.settings) {
            throw "settings が見つかりません（config\settings.toml か -Config）。公開先が決まらないので止めます（docs/admin-runbook.md）。"
        }
        if (-not $Dest) {
            throw "publish.dest_dir が空です。settings の [publish] dest_dir か -Dest に同期フォルダを指定してください（publish/ だけ作るなら -NoSync）。"
        }
    }
    if (-not $DryRun -and ($cfg.api_key_source -ne "env" -and $cfg.api_key_source -ne "file")) {
        throw "API キーがありません。EDINET_API_KEY 環境変数か settings の paths.api_key_file を設定してください（docs/admin-runbook.md）。"
    }
    if ($Dest -and ([IO.Path]::GetFullPath($Dest).TrimEnd('\') -eq [IO.Path]::GetFullPath($Work).TrimEnd('\'))) {
        throw "publish.dest_dir と publish.work_dir が同じです。別のフォルダにしてください。"
    }

    # ---- 1. 対象社（transport7 + 依頼社リスト）を1つの targets にまとめる
    Invoke-Cli "targets" (@("publish", "targets", "--out", $Targets) + $CfgArgs)

    # ---- 2. 書類一覧（まだ確定していない最も古い日〜当日。少なくとも doclist_days 日、多くて doclist_max_days 日）
    if (-not $SkipDoclist) {
        # 書類一覧が取れない週は公開しない（終了コード 2 も失敗扱い。WarnCodes なし）
        Invoke-Cli "doclist" (@("doclist", "sync", "--from", $cfg.doclist_from, "--to", $cfg.doclist_to) + $CfgArgs)
    }

    # ---- 3. resolve → fetch → extract → resolve（基準日T = $cfg.as_of。週次は当日）
    $AsOf = $cfg.as_of
    Invoke-Cli "resolve(pre)" (@("resolve", "--targets", $Targets, "--run-id", $R1, "--out", $Out, "--cache", $Cache,
        "--as-of", $AsOf) + $CfgArgs)
    Invoke-Cli "fetch" (@("fetch", "--targets", $Targets, "--run-id", $R1, "--include-pending") + $CfgArgs) -WarnCodes @(2)
    Assert-FetchGate
    Invoke-Cli "extract" (@("extract") + $CfgArgs)
    Invoke-Cli "resolve" (@("resolve", "--targets", $Targets, "--run-id", $R2, "--out", $Out, "--cache", $Cache,
        "--as-of", $AsOf) + $CfgArgs)

    # ---- 4. compare → report（risk_diff_transport7.xlsx）。本文は cache の type=1 ZIP から読む
    $resolveJson = Join-Path $Out ("resolve_{0}\resolve_transport7.json" -f $R2)
    $m1 = Join-Path $Out "m1"
    Invoke-Cli "compare" @("compare", "--resolve", $resolveJson, "--m0", (Join-Path $Out "m0_none"), "--cache", $Cache,
        "--out", $m1, "--run-id", $R2) -WarnCodes @(2)   # 2 = 前期なし等で比較できない社がある
    Invoke-Cli "report" @("report", "--run", (Join-Path $m1 "run_$R2")) -WarnCodes @(2)

    # ---- 5. monitor
    Invoke-Cli "monitor" (@("monitor", "--run", $R2, "--out", $Out) + $CfgArgs)

    # ---- 6. 社一覧.xlsx・社別 md・週次要約.md を生成し、risk_diff / monitor を正規名でコピー
    $buildArgs = @("publish", "build", "--out-root", $Out, "--resolve-run", $R2, "--targets", $Targets,
        "--publish-dir", $Work) + $CfgArgs
    if ($DryRun) {
        Write-Log ("[build] {0} -m edinet_risk {1}" -f $Python, (Format-Args $buildArgs))
        Write-Log ("[build] docs\templates\*.xlsx -> {0}" -f (Join-Path $Work "templates"))
        Copy-Over (Join-Path $m1 "run_$R2\risk_diff_transport7.xlsx") (Join-Path $Work "risk_diff.xlsx")
        Copy-Over ("{0}\monitor_<run-id>\monitor.xlsx" -f $Out) (Join-Path $Work "monitor.xlsx")
        Write-Log ("[check] {0} -m edinet_risk publish check --publish-dir {1}" -f $Python, $Work)
    } else {
        $built = Invoke-Json "build" $buildArgs
        Write-Log ("    社一覧 {0} 社、社別 md {1} 件、templates {2} 件" -f $built.companies, @($built.md).Count, @($built.templates).Count)
        Copy-Over $built.copies."risk_diff.xlsx" (Join-Path $Work "risk_diff.xlsx")
        Copy-Over $built.copies."monitor.xlsx" (Join-Path $Work "monitor.xlsx")
        # ---- 7. 成功の定義（5種そろい、社一覧・monitor が当日）
        Invoke-Cli "check" @("publish", "check", "--publish-dir", $Work)
    }

    # ---- 8. 同期フォルダへ上書きコピー
    if ($NoSync -or -not $Dest) {   # -not $Dest はここでは -DryRun のときだけ（それ以外は 0. で止めている）
        Write-Log "同期フォルダへのコピーは省略（-NoSync または publish.dest_dir 未設定）。"
    } elseif ($DryRun) {
        foreach ($n in @("risk_diff.xlsx", "monitor.xlsx")) { Copy-Over (Join-Path $Work $n) (Join-Path $Dest $n) }
        Write-Log ("[copy] {0}\(社一覧.xlsx・週次要約.md・社別\*.md・templates\*.xlsx) -> {1}\ （同じ相対パスに上書き）" -f $Work, $Dest)
    } else {
        if (-not (Test-Path -LiteralPath $Dest)) { throw "同期フォルダがありません: $Dest" }
        $base = (Resolve-Path -LiteralPath $Work).Path.TrimEnd('\')
        Get-ChildItem -LiteralPath $Work -Recurse -File | ForEach-Object {
            Copy-Over $_.FullName (Join-Path $Dest $_.FullName.Substring($base.Length + 1))
        }
    }

    if ($Script:Warnings.Count -gt 0) { Write-Log ("完了（警告 {0} 件: {1}）。monitor.xlsx を確認してください。" -f $Script:Warnings.Count, ($Script:Warnings -join "; ")) }
    else { Write-Log "完了。" }
} catch {
    Write-Log ("失敗: {0}" -f $_.Exception.Message)
    $exit = 1
}
exit $exit
