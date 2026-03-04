param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "python",
    [string]$OutFile = "",
    [string]$BaselineFile = "",
    [switch]$FailOnRegression,
    [int]$MaxRegressionPercent = 25
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
    $OutFile = Join-Path $RepoRoot "out\perf_baseline.json"
}

function Run-CommandTimed([string]$Name, [string]$Exe, [string[]]$Args, [string]$WorkDir) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $Exe @Args
    $code = $LASTEXITCODE
    $sw.Stop()
    if ($code -ne 0) {
        throw "Benchmark step failed: $Name (exit=$code)"
    }
    return [math]::Round($sw.Elapsed.TotalMilliseconds, 2)
}

function Compare-Regression([string]$Name, [double]$CurrentMs, [double]$BaselineMs, [int]$MaxPct) {
    if ($BaselineMs -le 0) { return }
    $allowed = $BaselineMs * (1.0 + ($MaxPct / 100.0))
    if ($CurrentMs -gt $allowed) {
        $incPct = (($CurrentMs - $BaselineMs) / $BaselineMs) * 100.0
        throw ("Performance regression in {0}: {1:N2}ms -> {2:N2}ms (+{3:N2}%, limit {4}%)" -f $Name, $BaselineMs, $CurrentMs, $incPct, $MaxPct)
    }
}

$tmpDir = Join-Path $env:TEMP ("nano-claw-perf-" + [Guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

try {
    $startupPy = Join-Path $tmpDir "startup_check.py"
    @'
import nano-claw.cli.commands
'@ | Set-Content -Path $startupPy -Encoding UTF8

    $toolPy = Join-Path $tmpDir "tool_call_check.py"
    @'
import asyncio
from pathlib import Path

from nano-claw.agent.tools.filesystem import ListDirTool

async def _run():
    tool = ListDirTool(allowed_dir=Path("."))
    result = await tool.execute(path=".")
    if hasattr(result, "ok") and not result.ok:
        raise SystemExit(1)

asyncio.run(_run())
'@ | Set-Content -Path $toolPy -Encoding UTF8

    Push-Location $RepoRoot
    try {
        $env:PYTHONPATH = "."
        $startupMs = Run-CommandTimed -Name "startup_import" -Exe $PythonExe -Args @($startupPy) -WorkDir $RepoRoot
        $toolMs = Run-CommandTimed -Name "tool_list_dir" -Exe $PythonExe -Args @($toolPy) -WorkDir $RepoRoot
        $testMs = Run-CommandTimed -Name "pytest_smoke" -Exe $PythonExe -Args @("-m", "pytest", "-q", "tests/test_context_memory_pipeline.py") -WorkDir $RepoRoot
    } finally {
        Pop-Location
    }

    $payload = [ordered]@{
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
        repo_root = $RepoRoot
        metrics_ms = [ordered]@{
            startup_import = $startupMs
            tool_list_dir = $toolMs
            pytest_smoke = $testMs
        }
    }

    if ($FailOnRegression) {
        if (-not (Test-Path $BaselineFile)) {
            throw "Baseline file not found: $BaselineFile"
        }
        $baseline = Get-Content $BaselineFile -Raw | ConvertFrom-Json
        Compare-Regression -Name "startup_import" -CurrentMs $startupMs -BaselineMs ([double]$baseline.metrics_ms.startup_import) -MaxPct $MaxRegressionPercent
        Compare-Regression -Name "tool_list_dir" -CurrentMs $toolMs -BaselineMs ([double]$baseline.metrics_ms.tool_list_dir) -MaxPct $MaxRegressionPercent
        Compare-Regression -Name "pytest_smoke" -CurrentMs $testMs -BaselineMs ([double]$baseline.metrics_ms.pytest_smoke) -MaxPct $MaxRegressionPercent
    }

    $outDir = Split-Path -Parent $OutFile
    if (-not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
    Write-Host "Perf baseline report written: $OutFile" -ForegroundColor Green
} finally {
    Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

