param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "python",
    [string]$GoExe = "go",
    [switch]$SkipIntegration,
    [switch]$RequireCoverageDoc,
    [switch]$SkipLocGate,
    [switch]$FailOnLocIncrease,
    [string]$LocBaselineFile = "",
    [int]$LocMaxIncreasePercent = 15
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Invoke-ChildPowerShell {
    param(
        [string[]]$Arguments,
        [string]$ErrorMessage = "Child PowerShell command failed"
    )
    & powershell -NoProfile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage (exit=$LASTEXITCODE)"
    }
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "Repo root not detected: $RepoRoot"
}

if ($RequireCoverageDoc) {
    $coverageFile = Join-Path $RepoRoot "OPENCLAW_COVERAGE.md"
    if (-not (Test-Path $coverageFile)) {
        throw "Missing required coverage document: $coverageFile"
    }
}

Step "Check uv.lock is up-to-date"
& uv lock --check
if ($LASTEXITCODE -ne 0) {
    throw "uv.lock is out of date. Run 'uv lock' and commit the result."
}

Step "Run strict self-test gate"
$args = @(
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $RepoRoot "scripts\selftest_local.ps1"),
    "-RepoRoot", $RepoRoot,
    "-PythonExe", $PythonExe,
    "-GoExe", $GoExe,
    "-FailOnHighRiskConfig",
    "-FailOnLegacyConfig",
    "-FailOnInvalidSkillManifest"
)
if ($SkipIntegration) {
    $args += "-SkipIntegration"
}

Invoke-ChildPowerShell -Arguments $args -ErrorMessage "Strict self-test gate failed"

if (-not $SkipLocGate -or $FailOnLocIncrease) {
    if ([string]::IsNullOrWhiteSpace($LocBaselineFile)) {
        $defaultBaseline = Join-Path $RepoRoot "workspace\LOC_BASELINE.json"
        if (Test-Path $defaultBaseline) {
            $LocBaselineFile = $defaultBaseline
        } else {
            throw "LocBaselineFile is required when LOC gate is enabled."
        }
    }
    Step "Run LOC growth gate"
    $locArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $RepoRoot "scripts\loc_report.ps1"),
        "-RepoRoot", $RepoRoot,
        "-OutFile", (Join-Path $RepoRoot "out\loc_report_gate.json"),
        "-FailOnIncrease",
        "-BaselineFile", $LocBaselineFile,
        "-MaxIncreasePercent", "$LocMaxIncreasePercent"
    )
    Invoke-ChildPowerShell -Arguments $locArgs -ErrorMessage "LOC growth gate failed"
}

Step "Release gate passed"
