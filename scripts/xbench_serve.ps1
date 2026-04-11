<#
.SYNOPSIS
    One-click launcher for xbench evaluation mode.

.DESCRIPTION
    Starts the zen-claw dashboard with OpenAI-compatible API enabled,
    verifies the /v1/chat/completions endpoint is reachable, and prints
    the base URL for use with the openai SDK or xbench_evals.py.

.PARAMETER Port
    Dashboard listen port (default 18791).

.PARAMETER Host
    Bind address (default 127.0.0.1).

.EXAMPLE
    .\scripts\xbench_serve.ps1
    .\scripts\xbench_serve.ps1 -Port 8080
#>

param(
    [int]$Port = 18791,
    [string]$Host = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[ERROR] Virtual environment not found at $VenvPython" -ForegroundColor Red
    Write-Host "        Run:  python -m venv .venv && .venv\Scripts\pip install -e '.[dev,rag,eval]'" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "=== zen-claw xbench evaluation server ===" -ForegroundColor Cyan
Write-Host ""

# ── Pre-flight: verify openai SDK is importable ──────────────────────────
Write-Host "[1/3] Checking dependencies..." -ForegroundColor Cyan
& $VenvPython -c "import openai; import fastapi; print('  deps OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Missing deps. Run:  .venv\Scripts\pip install -e '.[dev,rag,eval]'" -ForegroundColor Red
    exit 1
}

# ── Start dashboard in background ────────────────────────────────────────
Write-Host "[2/3] Starting dashboard on ${Host}:${Port}..." -ForegroundColor Cyan
$proc = Start-Process -FilePath $VenvPython `
    -ArgumentList "-m", "zen_claw", "dashboard", "--host", $Host, "--port", $Port `
    -PassThru -NoNewWindow

# ── Wait for health check ────────────────────────────────────────────────
Write-Host "[3/3] Waiting for health check..." -ForegroundColor Cyan
$baseUrl = "http://${Host}:${Port}"
$healthy = $false
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri "$baseUrl/api/v1/health" -Method GET -TimeoutSec 2
        if ($resp.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}

if (-not $healthy) {
    Write-Host "[ERROR] Dashboard did not become healthy within 30s" -ForegroundColor Red
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host ""
Write-Host "Server is running." -ForegroundColor Green
Write-Host ""
Write-Host "  OpenAI-compat base URL:  $baseUrl/v1" -ForegroundColor White
Write-Host "  Health check:            $baseUrl/api/v1/health" -ForegroundColor White
Write-Host "  Models:                  $baseUrl/v1/models" -ForegroundColor White
Write-Host ""
Write-Host "  Quick test:" -ForegroundColor Yellow
Write-Host "    python -c `"from openai import OpenAI; c=OpenAI(base_url='$baseUrl/v1',api_key='test'); print(c.chat.completions.create(model='zen-claw',messages=[{'role':'user','content':'hello'}]).choices[0].message.content)`"" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  xbench-evals:" -ForegroundColor Yellow
Write-Host "    python xbench_evals.py --model zen-claw --dataset data/DeepSearch-2510.csv --n-repeats 1" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray

try {
    Wait-Process -Id $proc.Id
} catch {
    # Ctrl+C
} finally {
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "`nServer stopped." -ForegroundColor Cyan
}
