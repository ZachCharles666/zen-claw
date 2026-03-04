param(
    [string]$InputFile = "",
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($InputFile)) {
    $InputFile = "out\perf_baseline.json"
}
if ([string]::IsNullOrWhiteSpace($OutFile)) {
    $OutFile = "PERFORMANCE_REPORT.md"
}

if (-not (Test-Path $InputFile)) {
    throw "Perf baseline input not found: $InputFile"
}

$data = Get-Content -Path $InputFile -Raw | ConvertFrom-Json
$metrics = $data.metrics_ms

$md = @()
$md += "# Performance Report"
$md += ""
$md += "- Generated At (UTC): $($data.generated_at)"
$md += "- Repo Root: $($data.repo_root)"
$md += ""
$md += "| Metric | Duration (ms) |"
$md += "|---|---:|"
$md += "| startup_import | $($metrics.startup_import) |"
$md += "| tool_list_dir | $($metrics.tool_list_dir) |"
$md += "| pytest_smoke | $($metrics.pytest_smoke) |"
$md += ""

Set-Content -Path $OutFile -Value ($md -join "`r`n") -Encoding UTF8
Write-Host "Performance report written: $OutFile" -ForegroundColor Green

