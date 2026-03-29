param(
    [ValidateSet("quick", "full", "nightly")]
    [string]$Mode = "quick",
    [string]$Suite = "",
    [int]$ShardIndex = -1,
    [int]$TotalShards = -1,
    [switch]$SkipLocGate
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$Title, [string]$CiLayer, [string]$ReproCommand) {
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    Write-Host "    CI layer: $CiLayer" -ForegroundColor DarkGray
    Write-Host "    Repro: $ReproCommand" -ForegroundColor DarkGray
}

function Invoke-Checked([scriptblock]$Command, [string]$ErrorMessage) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage (exit=$LASTEXITCODE)"
    }
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-PythonExe([string]$RepoRoot) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython -PathType Leaf)) {
        throw "Project venv Python not found: $venvPython"
    }
    return $venvPython
}

function Get-SelectedTests(
    [string]$RepoRoot,
    [string]$PythonExe,
    [string]$TargetSuite,
    [int]$TargetShardIndex,
    [int]$TargetTotalShards
) {
    $args = @(
        (Join-Path $RepoRoot "scripts\ci_select_tests.py"),
        "--suite",
        $TargetSuite,
        "--format",
        "json"
    )
    if ($TargetShardIndex -ge 0 -or $TargetTotalShards -ge 0) {
        if ($TargetShardIndex -lt 0 -or $TargetTotalShards -lt 1) {
            throw "Both -ShardIndex and -TotalShards must be provided for sharded preflight runs."
        }
        $args += @("--total-shards", "$TargetTotalShards", "--shard-index", "$TargetShardIndex")
    }
    $raw = & $PythonExe @args
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to resolve tests for suite '$TargetSuite'."
    }
    $payload = $raw | ConvertFrom-Json
    return @($payload.tests)
}

function Invoke-PytestSuite(
    [string]$RepoRoot,
    [string]$PythonExe,
    [string]$Title,
    [string]$CiLayer,
    [string]$TargetSuite,
    [int]$TargetShardIndex = -1,
    [int]$TargetTotalShards = -1
) {
    $selectedTests = Get-SelectedTests `
        -RepoRoot $RepoRoot `
        -PythonExe $PythonExe `
        -TargetSuite $TargetSuite `
        -TargetShardIndex $TargetShardIndex `
        -TargetTotalShards $TargetTotalShards
    $selectorArgs = @("--suite", $TargetSuite)
    if ($TargetShardIndex -ge 0) {
        $selectorArgs += @("--total-shards", "$TargetTotalShards", "--shard-index", "$TargetShardIndex")
    }
    $selectorCmd = "$PythonExe scripts/ci_select_tests.py $($selectorArgs -join ' ') --format json"
    $pytestCmd = "$PythonExe -m pytest -p no:timeout -q $($selectedTests -join ' ')"
    Step -Title $Title -CiLayer $CiLayer -ReproCommand "$selectorCmd`n           $pytestCmd"
    Push-Location $RepoRoot
    try {
        $env:PYTEST_ADDOPTS = ""
        $pytestArgs = @("-m", "pytest", "-p", "no:timeout", "-q") + @($selectedTests)
        Invoke-Checked -Command { & $PythonExe @pytestArgs } -ErrorMessage "$Title failed"
    } finally {
        Pop-Location
    }
}

$repoRoot = Get-RepoRoot
$pythonExe = Get-PythonExe -RepoRoot $repoRoot

if ([string]::IsNullOrWhiteSpace($Suite)) {
    if ($ShardIndex -ge 0 -or $TotalShards -ge 0) {
        throw "-Suite core is required when using shard arguments."
    }
} elseif ($Suite -ne "core" -and ($ShardIndex -ge 0 -or $TotalShards -ge 0)) {
    throw "Shard arguments are only supported with -Suite core."
}

if ($Mode -eq "nightly" -and -not [string]::IsNullOrWhiteSpace($Suite)) {
    throw "-Suite cannot be combined with -Mode nightly."
}

Step -Title "Preflight mode: $Mode" -CiLayer "operator entry" -ReproCommand "pwsh .\scripts\preflight.ps1 -Mode $Mode"

if ($Mode -eq "nightly") {
    Push-Location $repoRoot
    try {
        Step -Title "Nightly baseline" -CiLayer "nightly-integration / preflight-full" -ReproCommand "pwsh .\scripts\preflight.ps1 -Mode full"
        Invoke-Checked -Command { & $PSScriptRoot\preflight.ps1 -Mode full } -ErrorMessage "Nightly preflight full failed"

        Step -Title "Nightly selftest" -CiLayer "nightly-integration / selftest" -ReproCommand "pwsh .\scripts\selftest_local.ps1"
        Invoke-Checked -Command { & $PSScriptRoot\selftest_local.ps1 } -ErrorMessage "Nightly selftest failed"

        Step -Title "Nightly performance baseline" -CiLayer "nightly-integration / perf-baseline" -ReproCommand "pwsh .\scripts\perf_baseline.ps1"
        Invoke-Checked -Command { & $PSScriptRoot\perf_baseline.ps1 } -ErrorMessage "Nightly performance baseline failed"

        Step -Title "Nightly performance report" -CiLayer "nightly-integration / perf-report" -ReproCommand "pwsh .\scripts\perf_report.ps1"
        Invoke-Checked -Command { & $PSScriptRoot\perf_report.ps1 } -ErrorMessage "Nightly performance report failed"
    } finally {
        Pop-Location
    }
    exit 0
}

Push-Location $repoRoot
try {
    Step -Title "PR Gate: lint" -CiLayer "lint-and-guards" -ReproCommand "$pythonExe -m ruff check ."
    Invoke-Checked -Command { & $pythonExe -m ruff check . } -ErrorMessage "Ruff lint failed"

    if ($SkipLocGate) {
        Step -Title "PR Gate: LOC" -CiLayer "lint-and-guards" -ReproCommand "pwsh .\scripts\loc_report.ps1  # skipped via -SkipLocGate"
    } else {
        Step -Title "PR Gate: LOC" -CiLayer "lint-and-guards" -ReproCommand "pwsh .\scripts\loc_report.ps1"
        Invoke-Checked -Command { & pwsh .\scripts\loc_report.ps1 } -ErrorMessage "LOC gate failed"
    }
} finally {
    Pop-Location
}

if ([string]::IsNullOrWhiteSpace($Suite)) {
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: core shard-equivalent suite" -CiLayer "core-tests" -TargetSuite "core"
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: memory-recall" -CiLayer "memory-recall" -TargetSuite "memory-recall"
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: channel-webchat-webhook" -CiLayer "channel-matrix / webchat-webhook" -TargetSuite "channel-webchat-webhook"
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: channel-slack" -CiLayer "channel-matrix / slack" -TargetSuite "channel-slack"
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: channel-signal" -CiLayer "channel-matrix / signal" -TargetSuite "channel-signal"
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: channel-matrix" -CiLayer "channel-matrix / matrix" -TargetSuite "channel-matrix"
} elseif ($Suite -eq "core" -and $ShardIndex -ge 0) {
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: core shard $ShardIndex/$TotalShards" -CiLayer "core-tests / shard-$ShardIndex" -TargetSuite "core" -TargetShardIndex $ShardIndex -TargetTotalShards $TotalShards
} else {
    Invoke-PytestSuite -RepoRoot $repoRoot -PythonExe $pythonExe -Title "PR Gate: $Suite" -CiLayer $Suite -TargetSuite $Suite
}

if ($Mode -eq "full") {
    Push-Location $repoRoot
    try {
        Step -Title "Build sanity" -CiLayer "preflight-full / build" -ReproCommand "$pythonExe -m pip install -U build`n           $pythonExe -m build"
        Invoke-Checked -Command { & $pythonExe -m pip install -U build } -ErrorMessage "Build package install failed"
        Invoke-Checked -Command { & $pythonExe -m build } -ErrorMessage "Build failed"
    } finally {
        Pop-Location
    }
}
