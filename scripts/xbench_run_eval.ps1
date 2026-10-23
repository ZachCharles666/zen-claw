<#
.SYNOPSIS
    Run xbench DeepSearch against the local zen-claw OpenAI-compatible server.

.DESCRIPTION
    This script starts the local zen-claw xbench API server, prepares the xbench-evals
    environment, runs xbench_evals.py, and archives the generated result CSV with a
    timestamped filename.

    It does not print API keys. If OPENROUTER_API_KEY is not already set, it tries to
    read providers.openrouter.apiKey from $env:USERPROFILE\.zen-claw\config.json.

.EXAMPLE
    pwsh .\scripts\xbench_run_eval.ps1 -Dataset data\DeepSearch-2510-10q.csv

.EXAMPLE
    pwsh .\scripts\xbench_run_eval.ps1 -Dataset data\DeepSearch-2510.csv -TimeoutMinutes 360
#>

param(
    [string]$XbenchRoot = "E:\xbench-evals",
    [string]$Dataset = "data\DeepSearch-2510-10q.csv",
    [int]$Repeats = 1,
    [string]$Model = "zen-claw",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 18791,
    [int]$StartupTimeoutSeconds = 45,
    [int]$TimeoutMinutes = 180,
    [string]$JudgeModel = "qwen/qwen3-32b",
    [switch]$SkipJudgePatch,
    [switch]$NoPatchZenClawModel,
    [switch]$KeepServer
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Fail {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Add-NoProxyEntry {
    param([string]$Entry)
    $current = [Environment]::GetEnvironmentVariable("NO_PROXY", "Process")
    if ([string]::IsNullOrWhiteSpace($current)) {
        [Environment]::SetEnvironmentVariable("NO_PROXY", $Entry, "Process")
        [Environment]::SetEnvironmentVariable("no_proxy", $Entry, "Process")
        return
    }
    $parts = $current -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    if ($parts -notcontains $Entry) {
        $next = ($parts + $Entry) -join ","
        [Environment]::SetEnvironmentVariable("NO_PROXY", $next, "Process")
        [Environment]::SetEnvironmentVariable("no_proxy", $next, "Process")
    }
}

function Get-ZenConfigOpenRouterKey {
    $configPath = Join-Path $env:USERPROFILE ".zen-claw\config.json"
    if (-not (Test-Path $configPath)) {
        return $null
    }
    try {
        $config = Get-Content $configPath -Raw | ConvertFrom-Json
        $key = $config.providers.openrouter.apiKey
        if ([string]::IsNullOrWhiteSpace($key)) {
            return $null
        }
        return [string]$key
    } catch {
        Write-Host "[WARN] Could not read OpenRouter key from ${configPath}: $($_.Exception.Message)" -ForegroundColor Yellow
        return $null
    }
}

function Patch-XbenchJudgeModel {
    param(
        [string]$LanguageModelsPath,
        [string]$TargetJudgeModel
    )

    if ([string]::IsNullOrWhiteSpace($TargetJudgeModel)) {
        return
    }

    $source = Get-Content $LanguageModelsPath -Raw
    $pattern = 'model="[^"]+", api_key=_or_key, base_url="https://openrouter\.ai/api/v1", judge=judge'
    $replacement = 'model="' + $TargetJudgeModel + '", api_key=_or_key, base_url="https://openrouter.ai/api/v1", judge=judge'

    if ($source -notmatch $pattern) {
        Write-Host "[WARN] Could not find the OpenRouter Gemini judge mapping to patch in language_models.py" -ForegroundColor Yellow
        return
    }

    $patched = [regex]::Replace($source, $pattern, $replacement, 1)
    if ($patched -ne $source) {
        $backupPath = "$LanguageModelsPath.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
        Copy-Item -LiteralPath $LanguageModelsPath -Destination $backupPath
        Set-Content -LiteralPath $LanguageModelsPath -Value $patched -Encoding UTF8
        Write-Host "[OK] Patched xbench judge model to $TargetJudgeModel"
        Write-Host "     Backup: $backupPath"
    } else {
        Write-Host "[OK] xbench judge model already appears patched"
    }
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$apiServerScript = Join-Path $repoRoot "scripts\xbench_api_server.py"
$patchScript = Join-Path $repoRoot "scripts\xbench_patch.py"
$xbenchEvalScript = Join-Path $XbenchRoot "xbench_evals.py"
$languageModelsPath = Join-Path $XbenchRoot "language_models.py"
$datasetPath = Join-Path $XbenchRoot $Dataset
$baseUrl = "http://${HostName}:${Port}/v1"
$healthUrl = "http://${HostName}:${Port}/api/v1/health"

Write-Step "Preflight"
if (-not (Test-Path $venvPython)) {
    Fail "Project venv Python not found: $venvPython"
}
if (-not (Test-Path $apiServerScript)) {
    Fail "API server script not found: $apiServerScript"
}
if (-not (Test-Path $XbenchRoot)) {
    Fail "xbench root not found: $XbenchRoot"
}
if (-not (Test-Path $xbenchEvalScript)) {
    Fail "xbench_evals.py not found: $xbenchEvalScript"
}
if (-not (Test-Path $languageModelsPath)) {
    Fail "language_models.py not found: $languageModelsPath"
}
if (-not (Test-Path $datasetPath)) {
    Fail "Dataset not found: $datasetPath"
}

& $venvPython -c "import openai, fastapi, uvicorn; print('deps OK')"
if ($LASTEXITCODE -ne 0) {
    Fail "Missing project dependencies. Install the project venv dependencies first."
}

Add-NoProxyEntry "127.0.0.1"
Add-NoProxyEntry "localhost"

if ([string]::IsNullOrWhiteSpace($env:OPENROUTER_API_KEY)) {
    $openRouterKey = Get-ZenConfigOpenRouterKey
    if (-not [string]::IsNullOrWhiteSpace($openRouterKey)) {
        $env:OPENROUTER_API_KEY = $openRouterKey
        Write-Host "[OK] Loaded OPENROUTER_API_KEY from zen-claw config"
    } else {
        Write-Host "[WARN] OPENROUTER_API_KEY is not set. xbench judge may fail." -ForegroundColor Yellow
    }
} else {
    Write-Host "[OK] OPENROUTER_API_KEY is already set"
}

if (-not $NoPatchZenClawModel) {
    Write-Step "Patch xbench zen-claw model"
    Push-Location $XbenchRoot
    try {
        & $venvPython $patchScript --base-url $baseUrl
        if ($LASTEXITCODE -ne 0) {
            Fail "xbench zen-claw patch failed"
        }
    } finally {
        Pop-Location
    }
}

if (-not $SkipJudgePatch) {
    Write-Step "Patch xbench judge model"
    Patch-XbenchJudgeModel -LanguageModelsPath $languageModelsPath -TargetJudgeModel $JudgeModel
}

Write-Step "Start zen-claw xbench API"
$serverStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$serverStdoutPath = Join-Path $XbenchRoot "${Model}_api_${serverStamp}.stdout.log"
$serverStderrPath = Join-Path $XbenchRoot "${Model}_api_${serverStamp}.stderr.log"
$serverProc = Start-Process -FilePath $venvPython `
    -ArgumentList $apiServerScript, "--host", $HostName, "--port", $Port `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $serverStdoutPath `
    -RedirectStandardError $serverStderrPath

try {
    $healthy = $false
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $serverProc.Refresh()
        if ($serverProc.HasExited) {
            if (Test-Path $serverStderrPath) {
                Write-Host "[api stderr tail]" -ForegroundColor Yellow
                Get-Content $serverStderrPath -Tail 80
            }
            if (Test-Path $serverStdoutPath) {
                Write-Host "[api stdout tail]" -ForegroundColor Yellow
                Get-Content $serverStdoutPath -Tail 80
            }
            Fail "zen-claw API exited before becoming healthy with code $($serverProc.ExitCode)"
        }
        try {
            $resp = Invoke-WebRequest -Uri $healthUrl -Method GET -TimeoutSec 2
            if ($resp.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }

    if (-not $healthy) {
        Fail "zen-claw API did not become healthy: $healthUrl"
    }
    $serverProc.Refresh()
    if ($serverProc.HasExited) {
        Fail "zen-claw API exited immediately after health check with code $($serverProc.ExitCode)"
    }
    Write-Host "[OK] API healthy: $healthUrl"

    Write-Step "Run xbench"
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $datasetName = [IO.Path]::GetFileNameWithoutExtension($Dataset)
    $resultName = "${Model}_results_${datasetName}_${stamp}.csv"
    $rawResultPath = Join-Path $XbenchRoot "${Model}_results.csv"
    $finalResultPath = Join-Path $XbenchRoot $resultName
    $evalStdoutPath = Join-Path $XbenchRoot "${Model}_xbench_${datasetName}_${stamp}.stdout.log"
    $evalStderrPath = Join-Path $XbenchRoot "${Model}_xbench_${datasetName}_${stamp}.stderr.log"

    if (Test-Path $rawResultPath) {
        $backupPath = Join-Path $XbenchRoot "${Model}_results.backup_${stamp}.csv"
        Move-Item -LiteralPath $rawResultPath -Destination $backupPath -Force
        Write-Host "[OK] Backed up existing result: $backupPath"
    }

    Push-Location $XbenchRoot
    try {
        & $venvPython "xbench_evals.py" "--model" $Model "--dataset" $Dataset "--n-repeats" "$Repeats" > $evalStdoutPath 2> $evalStderrPath
        $evalExitCode = $LASTEXITCODE
        if ($evalExitCode -ne 0) {
            if (Test-Path $evalStderrPath) {
                Write-Host "[xbench stderr tail]" -ForegroundColor Yellow
                Get-Content $evalStderrPath -Tail 40
            }
            if (Test-Path $evalStdoutPath) {
                Write-Host "[xbench stdout tail]" -ForegroundColor Yellow
                Get-Content $evalStdoutPath -Tail 40
            }
            Fail "xbench exited with code $evalExitCode"
        }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $rawResultPath)) {
        Fail "xbench finished but result file was not found: $rawResultPath"
    }

    Move-Item -LiteralPath $rawResultPath -Destination $finalResultPath -Force

    $rows = @(Import-Csv $finalResultPath)
    $errorRows = @($rows | Where-Object { $_."error-1" -eq "Y" }).Count
    $nonEmpty = @($rows | Where-Object { -not [string]::IsNullOrWhiteSpace($_."response-1") }).Count
    $scoreValues = @($rows | ForEach-Object {
        $score = 0.0
        if ([double]::TryParse($_."score-1", [ref]$score)) { $score }
    })
    $scoreSum = if ($scoreValues.Count -gt 0) { ($scoreValues | Measure-Object -Sum).Sum } else { 0 }
    $scoreAvg = if ($scoreValues.Count -gt 0) { ($scoreValues | Measure-Object -Average).Average } else { 0 }

    Write-Step "Summary"
    Write-Host "Result:             $finalResultPath"
    Write-Host "Rows:               $($rows.Count)"
    Write-Host "Model error rows:   $errorRows"
    Write-Host "Non-empty response: $nonEmpty"
    Write-Host "Score sum:          $scoreSum"
    Write-Host "Score avg:          $scoreAvg"
} finally {
    if (-not $KeepServer) {
        $serverProc.Refresh()
        if ($serverProc -and -not $serverProc.HasExited) {
            Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
        }
        Write-Host "[OK] Server stopped"
    } else {
        Write-Host "[OK] Server kept running, PID=$($serverProc.Id)"
    }
}
