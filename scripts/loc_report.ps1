param(
    [string]$RepoRoot = "",
    [string]$OutFile = "",
    [string]$BaselineFile = "",
    [switch]$FailOnIncrease,
    [int]$MaxIncreasePercent = 15
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "Repo root not detected: $RepoRoot"
}

if ([string]::IsNullOrWhiteSpace($OutFile)) {
    $OutFile = Join-Path $RepoRoot "out\loc_report.json"
}

function Get-NonBlankLineCount([string]$filePath) {
    $lines = Get-Content -Path $filePath -ErrorAction Stop
    return @($lines | Where-Object { $_.Trim().Length -gt 0 }).Count
}

function Build-Stats([string]$root, [string[]]$globs) {
    $excludePattern = '(\\|/)(\.git|\.venv|venv|node_modules|dist|out|__pycache__|\.pytest_cache)(\\|/)'
    $files = @()
    foreach ($g in $globs) {
        $files += Get-ChildItem -Path $root -Recurse -File -Include $g -ErrorAction SilentlyContinue
    }
    $files = $files |
        Where-Object { $_.FullName -notmatch $excludePattern } |
        Sort-Object FullName -Unique

    $total = 0
    foreach ($f in $files) {
        $total += Get-NonBlankLineCount $f.FullName
    }
    return [PSCustomObject]@{
        files = $files.Count
        nonblank_lines = $total
    }
}

$pyStats = Build-Stats $RepoRoot @("*.py")
$goStats = Build-Stats $RepoRoot @("*.go")

$report = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    repo_root = $RepoRoot
    python = $pyStats
    go = $goStats
    total_nonblank_lines = [int]($pyStats.nonblank_lines + $goStats.nonblank_lines)
}

$outDir = Split-Path -Path $OutFile -Parent
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}
$json = $report | ConvertTo-Json -Depth 5
Set-Content -Path $OutFile -Value $json -Encoding UTF8

Write-Host "LOC report written: $OutFile" -ForegroundColor Green
Write-Host ("python nonblank: {0}, go nonblank: {1}, total: {2}" -f $pyStats.nonblank_lines, $goStats.nonblank_lines, $report.total_nonblank_lines)

if ($FailOnIncrease) {
    if ([string]::IsNullOrWhiteSpace($BaselineFile)) {
        throw "BaselineFile is required when -FailOnIncrease is set."
    }
    if (-not (Test-Path $BaselineFile)) {
        throw "Baseline file not found: $BaselineFile"
    }

    $baseline = Get-Content -Path $BaselineFile -Raw | ConvertFrom-Json
    $baseTotal = [int]$baseline.total_nonblank_lines
    $newTotal = [int]$report.total_nonblank_lines
    if ($baseTotal -le 0) {
        throw "Invalid baseline total_nonblank_lines: $baseTotal"
    }
    $increasePct = (($newTotal - $baseTotal) * 100.0) / $baseTotal
    Write-Host ("baseline total: {0}, increase: {1:N2}%%" -f $baseTotal, $increasePct)
    if ($increasePct -gt $MaxIncreasePercent) {
        throw ("LOC increased by {0:N2}%% (limit {1}%%)" -f $increasePct, $MaxIncreasePercent)
    }
}
