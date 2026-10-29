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
  - 同期フォルダへは上書きコピーのみ（削除→新規作成にしない。リンクが変わるため）。社一覧.xlsx を最後にコピーし
    「完了の印」にする（途中で失敗したら社一覧.xlsx は前回のまま）。ロック中のファイルは短い間隔で数回やり直す。
    例外: 同期フォルダの 社別\*.md のうち今回の対象にないもの（対象外・label 変更前）は消す（Copilot が読まないように）。
  - 終了コード: 0 = 成功、1 = 失敗（同期していない、またはコピー途中で失敗）、3 = -FailOnThreshold 指定時に
    monitor の閾値超過があった（公開はした）。
  - compare / report の終了コード 2 は「一部の社が比較できない／照合が全文一致しない」なら警告で続行、
    出力の集計行がない（入力がない）なら失敗にする。
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
.PARAMETER FailOnThreshold
  monitor の閾値超過（breached が1件以上）があれば、公開したうえで終了コード 3 にする（既定オフ = 警告のみで 0）。

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
    [switch]$NoSync,
    [switch]$FailOnThreshold
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
$Script:Pending = New-Object System.Collections.Generic.List[string]   # ログファイルが決まる前の行（publish config の stderr 等）
$Script:Warnings = New-Object System.Collections.Generic.List[string]
$Script:CliOut = New-Object System.Collections.Generic.List[string]   # 直前の Invoke-Cli の出力行
$Script:LastRc = 0
# fetch の集計行（__main__._fetch の print と一致させる。tests/test_peer_publish.py で照合）
$FetchSummary = 'fetched=(\d+) adopted_from_cache=(\d+) failed=(\d+)'
# compare / report / monitor の集計行（終了コード 2 の「一部の社だけ」と「入力なし」を分ける。同上で照合）
$CompareSummary = 'compare\.json in '
$ReportSummary = 'run_id=\S+ xlsx=.* html=\d+ pages'   # ASCII 部分で照合（コンソールの文字コードに依らない）
$MonitorSummary = 'breached=(\d+)'
$Script:Breached = 0
$CompanyList = "社一覧.xlsx"   # 同期フォルダへ最後にコピーする（完了の印）
$CopyRetries = 5
$CopyRetrySec = 10

function Write-Log([string]$Text) {
    Write-Host $Text
    if ($Script:Log) { Add-Content -LiteralPath $Script:Log -Value $Text -Encoding UTF8 } else { $Script:Pending.Add($Text) }
}

# ログファイルを決め、それまでの行を書き出す
function Open-Log([string]$Dir) {
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    $Script:Log = Join-Path $Dir ("publish_peer_{0}.log" -f $Stamp)
    if ($Script:Pending.Count -gt 0) { Add-Content -LiteralPath $Script:Log -Value $Script:Pending -Encoding UTF8 }
    $Script:Pending.Clear()
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

# compare / report の終了コード 2: 集計行があれば「一部の社が比較できない／照合が全文一致しない」（警告で続行）、
# なければ入力がない（resolve / compare の出力がない）ので止める。
function Assert-Partial([string]$Step, [string]$Pattern) {
    if ($DryRun -or $Script:LastRc -eq 0) { return }
    $hit = $false
    foreach ($ln in $Script:CliOut) { if ($ln -match $Pattern) { $hit = $true } }
    if (-not $hit) {
        throw ("{0}: 終了コード 2 で集計行がありません（入力なし）。同期フォルダは更新しません（前回分が残ります）。" -f $Step)
    }
    Write-Log ("    -> {0}: 終了コード 2 = 一部の社が比較できない／照合が全文一致しない（入力はある。続行）" -f $Step)
}

# monitor の閾値超過の件数（monitor は --fail-on-threshold なしで呼ぶ。終了コード 2 は入力エラーにも使われるため）
function Read-MonitorBreach {
    if ($DryRun) { return }
    foreach ($ln in $Script:CliOut) { if ($ln -match $MonitorSummary) { $Script:Breached = [int]$Matches[1] } }
    if ($Script:Breached -gt 0) {
        $Script:Warnings.Add("monitor breached=$($Script:Breached)")
        Write-Log ("    -> 警告: monitor の閾値超過 {0} 件（monitor.xlsx 監視_概要）" -f $Script:Breached)
    }
}

# JSON を出す helper（publish config / publish build）を実行して結果を返す。
function Invoke-Json([string]$Step, [string[]]$CliArgs) {
    Write-Log ("[{0}] {1} -m edinet_risk {2}" -f $Step, $Python, (Format-Args $CliArgs))
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    # 標準エラー（失敗理由の logging）は ErrorRecord として分けて受け取り、ログに残す。標準出力だけを JSON にする
    $all = @(& $Python -m edinet_risk --log-level WARNING @CliArgs 2>&1)
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    $out = @($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
    foreach ($e in @($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })) {
        Write-Log ("    stderr: " + $e.ToString())
    }
    if ($rc -ne 0) { throw ("{0} が終了コード {1} で失敗しました（理由は上の stderr）。" -f $Step, $rc) }
    return (($out | Out-String) | ConvertFrom-Json)
}

# 同期フォルダへ上書きコピー（削除しない）。
function Copy-Over([string]$From, [string]$To) {
    Write-Log ("[copy] {0} -> {1}" -f $From, $To)
    if ($DryRun) { return }
    $dir = Split-Path -Parent $To
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    for ($i = 1; ; $i++) {   # Excel で開かれている等のロックは短い間隔でやり直す
        try { Copy-Item -LiteralPath $From -Destination $To -Force; return }
        catch {
            if ($i -ge $CopyRetries) { throw ("コピーできません: {0}（{1}）" -f $To, $_.Exception.Message) }
            Write-Log ("    -> ロック中？ {0} 秒後にやり直します（{1}/{2}）: {3}" -f $CopyRetrySec, $i, $CopyRetries, $_.Exception.Message)
            Start-Sleep -Seconds $CopyRetrySec
        }
    }
}

# 同期フォルダの 社別\*.md のうち、今回 publish\社別 にないもの（対象外・label 変更前）を消す。ほかのファイルは消さない。
function Remove-StaleMd([string]$WorkDir, [string]$DestDir) {
    $dd = Join-Path $DestDir "社別"
    if ($DryRun -or -not (Test-Path -LiteralPath $dd)) { return }
    $keep = @(Get-ChildItem -LiteralPath (Join-Path $WorkDir "社別") -Filter *.md -File | ForEach-Object { $_.Name })
    Get-ChildItem -LiteralPath $dd -Filter *.md -File | Where-Object { $keep -notcontains $_.Name } | ForEach-Object {
        Write-Log ("[remove] {0}（今回の対象にない社別 md）" -f $_.FullName)
        Remove-Item -LiteralPath $_.FullName -Force
    }
}

# パスを末尾 \ 付きの絶対パスにする（同じ／配下の判定用。Windows なので大小文字を区別しない）
function Get-FullDir([string]$P) { [IO.Path]::GetFullPath($P).TrimEnd('\') + '\' }
function Test-SameOrUnder([string]$Child, [string]$Parent) {
    (Get-FullDir $Child).StartsWith((Get-FullDir $Parent), [StringComparison]::OrdinalIgnoreCase)
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
    if (-not $DryRun) { Open-Log (Join-Path $Out "logs") }
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
    if ($Dest -and ((Test-SameOrUnder $Dest $Work) -or (Test-SameOrUnder $Work $Dest))) {
        throw "publish.dest_dir と publish.work_dir が同じか、一方が他方の中にあります（自己上書きになる）。別のフォルダにしてください。"
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
        "--out", $m1, "--run-id", $R2) -WarnCodes @(2)   # 2 = 前期なし等で比較できない社がある、または入力なし
    Assert-Partial "compare" $CompareSummary
    Invoke-Cli "report" @("report", "--run", (Join-Path $m1 "run_$R2")) -WarnCodes @(2)
    Assert-Partial "report" $ReportSummary

    # ---- 5. monitor（--fail-on-threshold は付けない。超過は集計行から読む）
    Invoke-Cli "monitor" (@("monitor", "--run", $R2, "--out", $Out) + $CfgArgs)
    Read-MonitorBreach

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
        Write-Log ("[copy] {0}\(週次要約.md・社別\*.md・templates\*.xlsx) -> {1}\ （同じ相対パスに上書き）" -f $Work, $Dest)
        Write-Log ("[remove] {0}\社別\*.md のうち今回の対象にないもの" -f $Dest)
        Copy-Over (Join-Path $Work $CompanyList) (Join-Path $Dest $CompanyList)
    } else {
        if (-not (Test-Path -LiteralPath $Dest)) { throw "同期フォルダがありません: $Dest" }
        $base = (Resolve-Path -LiteralPath $Work).Path.TrimEnd('\')
        $files = @(Get-ChildItem -LiteralPath $Work -Recurse -File | ForEach-Object { $_.FullName.Substring($base.Length + 1) })
        $rest = @($files | Where-Object { $_ -ne $CompanyList })
        $done = New-Object System.Collections.Generic.List[string]
        try {
            foreach ($rel in $rest) { Copy-Over (Join-Path $Work $rel) (Join-Path $Dest $rel); $done.Add($rel) }
            Remove-StaleMd $Work $Dest
            Copy-Over (Join-Path $Work $CompanyList) (Join-Path $Dest $CompanyList)   # 最後 = 完了の印
            $done.Add($CompanyList)
        } finally {
            Write-Log ("コピー済み {0}/{1} 件: {2}" -f $done.Count, ($rest.Count + 1), ($done -join ", "))
            if (-not $done.Contains($CompanyList)) {
                Write-Log "    -> 同期フォルダは新旧が混在しています（社一覧.xlsx は前回のまま）。ロックを外して再実行してください。"
            }
        }
    }

    if ($Script:Warnings.Count -gt 0) { Write-Log ("完了（警告 {0} 件: {1}）。monitor.xlsx を確認してください。" -f $Script:Warnings.Count, ($Script:Warnings -join "; ")) }
    else { Write-Log "完了。" }
    if ($FailOnThreshold -and $Script:Breached -gt 0) {
        Write-Log ("-FailOnThreshold: monitor の閾値超過 {0} 件のため終了コード 3（公開は済み）。" -f $Script:Breached)
        $exit = 3
    }
} catch {
    Write-Log ("失敗: {0}" -f $_.Exception.Message)
    $exit = 1
    if (-not $DryRun -and -not $Script:Log) {   # publish config で失敗: 既定の out\publish\logs に残す
        try { Open-Log (Join-Path $Root "out\publish\logs") } catch { }
    }
}
exit $exit
