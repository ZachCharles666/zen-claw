param(
    [string]$RepoRoot = "",
    [switch]$UsePythonModuleFallback
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Resolve-RepoRoot([string]$inputRoot) {
    if (-not [string]::IsNullOrWhiteSpace($inputRoot)) {
        return (Resolve-Path $inputRoot).Path
    }
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Invoke-CheckedCommand([string[]]$commandParts, [string]$label) {
    Step $label
    & $commandParts[0] $commandParts[1..($commandParts.Length - 1)]
    if ($LASTEXITCODE -ne 0) {
        throw "$label failed (exit=$LASTEXITCODE)"
    }
}

$RepoRoot = Resolve-RepoRoot $RepoRoot
if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "Repo root not detected: $RepoRoot"
}

Push-Location $RepoRoot
try {
    $useModule = $UsePythonModuleFallback.IsPresent
    $nanoCmd = Get-Command "nano-claw" -ErrorAction SilentlyContinue
    if (-not $nanoCmd) {
        $useModule = $true
    }

    if ($useModule) {
        Invoke-CheckedCommand @("python", "-m", "nano_claw.cli.commands", "config", "providers") "config providers"
        Invoke-CheckedCommand @(
            "python", "-m", "nano_claw.cli.commands", "config", "wizard",
            "--provider", "openrouter",
            "--api-key", "sk-dry-run",
            "--model", "openrouter/anthropic/claude-3.5-sonnet",
            "--api-base", "https://openrouter.ai/api/v1",
            "--dry-run", "-y"
        ) "config wizard --dry-run (non-interactive)"
        Invoke-CheckedCommand @("python", "-m", "nano_claw.cli.commands", "config", "doctor", "--strict") "config doctor --strict"
        Invoke-CheckedCommand @("python", "-m", "nano_claw.cli.commands", "status", "-v") "status -v"
    } else {
        Invoke-CheckedCommand @("nano-claw", "config", "providers") "config providers"
        Invoke-CheckedCommand @(
            "nano-claw", "config", "wizard",
            "--provider", "openrouter",
            "--api-key", "sk-dry-run",
            "--model", "openrouter/anthropic/claude-3.5-sonnet",
            "--api-base", "https://openrouter.ai/api/v1",
            "--dry-run", "-y"
        ) "config wizard --dry-run (non-interactive)"
        Invoke-CheckedCommand @("nano-claw", "config", "doctor", "--strict") "config doctor --strict"
        Invoke-CheckedCommand @("nano-claw", "status", "-v") "status -v"
    }

    Step "Config acceptance passed"
} finally {
    Pop-Location
}
