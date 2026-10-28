#Requires -Version 5.1
<#
.SYNOPSIS
  Weekly peer publish for edinet-risk (Windows admin PC). See docs/admin-runbook.md.
.PARAMETER DryRun
  Print planned commands and dest; do not call the network pipeline or overwrite publish/.
.PARAMETER Config
  settings.toml (default: config\settings.toml).
.PARAMETER Dest
  Override publish.dest_dir (SharePoint sync folder).
.PARAMETER Python
  python.exe (default: .venv\Scripts\python.exe then PATH).
.PARAMETER SkipDoclist
  Skip doclist sync.
.PARAMETER NoSync
  Build and check publish/ only; do not copy to dest_dir.
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

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $RepoRoot

function Resolve-Python {
  param([string]$Hint)
  if ($Hint -and (Test-Path -LiteralPath $Hint)) { return (Resolve-Path -LiteralPath $Hint).Path }
  $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
  if (Test-Path -LiteralPath $venvPy) { return $venvPy }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "python.exe not found. Create .venv or pass -Python."
}

function Invoke-Edinet {
  param(
    [Parameter(Mandatory = $true)][string]$Py,
    [Parameter(Mandatory = $true)][string[]]$CliArgs,
    [switch]$WhatIfOnly
  )
  $line = "{0} -m edinet_risk {1}" -f $Py, ($CliArgs -join " ")
  Write-Host (">> {0}" -f $line)
  if ($WhatIfOnly) { return 0 }
  & $Py -m edinet_risk @CliArgs
  if ($LASTEXITCODE -ne 0) {
    throw ("edinet_risk failed (exit {0}): {1}" -f $LASTEXITCODE, ($CliArgs -join " "))
  }
  return $LASTEXITCODE
}

$Py = Resolve-Python -Hint $Python
$CfgPath = if ($Config) { $Config } else { Join-Path $RepoRoot "config\settings.toml" }

$cfgArgs = @("publish", "config")
if (Test-Path -LiteralPath $CfgPath) { $cfgArgs += @("--config", $CfgPath) }
$cfgRaw = & $Py -m edinet_risk @cfgArgs
if ($LASTEXITCODE -ne 0) { throw "publish config failed" }
# ConvertFrom-Json without -AsHashtable (PS 5.1)
$cfg = $cfgRaw | ConvertFrom-Json

if ($cfg.api_key_source -eq "none" -and -not $DryRun) {
  throw "API key not available (need EDINET_API_KEY env or paths.api_key_file). Refusing to run."
}

$OutDir = Join-Path $RepoRoot $(if ($cfg.out_dir) { $cfg.out_dir } else { "out\publish" })
$WorkDir = Join-Path $RepoRoot $(if ($cfg.work_dir) { $cfg.work_dir } else { "publish" })
$SyncDest = if ($Dest) { $Dest } elseif ($cfg.dest_dir) { [string]$cfg.dest_dir } else { "" }
$Days = [int]$cfg.doclist_days
if ($Days -lt 1) { $Days = 8 }

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir, $OutDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir ("publish_peer_{0}.log" -f $Stamp)
Start-Transcript -Path $LogFile -Append | Out-Null

try {
  Write-Host ("publish_peer start={0} DryRun={1} api_key_source={2}" -f $Stamp, [bool]$DryRun, $cfg.api_key_source)
  Write-Host ("out={0} work={1} dest={2}" -f $OutDir, $WorkDir, $SyncDest)

  $MergedTargets = Join-Path $OutDir "targets.toml"
  $tArgs = @("publish", "targets", "--out", $MergedTargets)
  foreach ($t in @($cfg.targets)) {
    $tp = if ([System.IO.Path]::IsPathRooted($t)) { $t } else { Join-Path $RepoRoot $t }
    $tArgs += @("--targets", $tp)
  }
  if (Test-Path -LiteralPath $CfgPath) { $tArgs += @("--config", $CfgPath) }
  Invoke-Edinet -Py $Py -CliArgs $tArgs -WhatIfOnly:$DryRun | Out-Null

  $today = Get-Date
  $from = $today.AddDays(1 - $Days).ToString("yyyy-MM-dd")
  $to = $today.ToString("yyyy-MM-dd")
  $RunId = "pub_{0}" -f $Stamp

  if (-not $SkipDoclist) {
    Invoke-Edinet -Py $Py -CliArgs @("doclist", "sync", "--from", $from, "--to", $to) -WhatIfOnly:$DryRun | Out-Null
  }

  # resolve (selection) -> fetch --include-pending -> extract -> resolve (after text)
  Invoke-Edinet -Py $Py -CliArgs @("resolve", "--targets", $MergedTargets, "--out", $OutDir, "--run-id", $RunId) -WhatIfOnly:$DryRun | Out-Null
  Invoke-Edinet -Py $Py -CliArgs @("fetch", "--run-id", $RunId, "--include-pending") -WhatIfOnly:$DryRun | Out-Null
  Invoke-Edinet -Py $Py -CliArgs @("--offline", "extract") -WhatIfOnly:$DryRun | Out-Null
  $ResolveRun2 = "{0}_b" -f $RunId
  Invoke-Edinet -Py $Py -CliArgs @("resolve", "--targets", $MergedTargets, "--out", $OutDir, "--run-id", $ResolveRun2) -WhatIfOnly:$DryRun | Out-Null

  # compare / report / monitor (report writes risk_diff_*.xlsx)
  $CompareOut = Join-Path $OutDir ("run_{0}" -f $ResolveRun2)
  $ResolveJson = Join-Path $OutDir ("resolve_{0}\resolve.json" -f $ResolveRun2)
  # Prefer folder layout used by m2; also pass --resolve path when JSON exists after dry path
  Invoke-Edinet -Py $Py -CliArgs @("compare", "--targets", $MergedTargets, "--out", $OutDir, "--run-id", $ResolveRun2, "--resolve", $ResolveJson) -WhatIfOnly:$DryRun | Out-Null
  Invoke-Edinet -Py $Py -CliArgs @("report", "--run-id", $ResolveRun2, "--out", $CompareOut) -WhatIfOnly:$DryRun | Out-Null
  Invoke-Edinet -Py $Py -CliArgs @("monitor", "--run-id", $ResolveRun2, "--out", $OutDir) -WhatIfOnly:$DryRun | Out-Null

  if ($DryRun) {
    Write-Host ("DryRun: would publish build/check into {0} then sync to '{1}'" -f $WorkDir, $SyncDest)
    Write-Host ("log={0}" -f $LogFile)
    exit 0
  }

  $buildArgs = @(
    "publish", "build",
    "--out-root", $OutDir,
    "--resolve-run", $ResolveRun2,
    "--targets", $MergedTargets,
    "--publish-dir", $WorkDir
  )
  if (Test-Path -LiteralPath $CfgPath) { $buildArgs += @("--config", $CfgPath) }
  Invoke-Edinet -Py $Py -CliArgs $buildArgs | Out-Null
  Invoke-Edinet -Py $Py -CliArgs @("publish", "check", "--publish-dir", $WorkDir) | Out-Null

  if ($NoSync -or -not $SyncDest) {
    Write-Host "No sync (NoSync or empty dest_dir). publish/ ready."
  }
  else {
    if (-not (Test-Path -LiteralPath $SyncDest)) { throw ("dest_dir not found: {0}" -f $SyncDest) }
    Write-Host ("Sync {0} -> {1} (overwrite copy, no delete)" -f $WorkDir, $SyncDest)
    foreach ($name in @("risk_diff.xlsx", "monitor.xlsx", "社一覧.xlsx", "週次要約.md", "社別", "templates")) {
      $src = Join-Path $WorkDir $name
      if (-not (Test-Path -LiteralPath $src)) { throw ("missing artifact: {0}" -f $src) }
      $dst = Join-Path $SyncDest $name
      if (Test-Path -LiteralPath $src -PathType Container) {
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
      }
      else {
        Copy-Item -LiteralPath $src -Destination $dst -Force
      }
    }
    Write-Host "Sync done."
  }

  Write-Host ("publish_peer OK log={0}" -f $LogFile)
  exit 0
}
catch {
  Write-Error $_
  Write-Host ("publish_peer FAILED log={0}" -f $LogFile)
  exit 1
}
finally {
  Stop-Transcript | Out-Null
}
