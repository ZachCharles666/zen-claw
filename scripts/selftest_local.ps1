param(
    [string]$RepoRoot = "",
    [string]$PythonExe = "python",
    [string]$GoExe = "go",
    [switch]$SkipIntegration,
    [switch]$FailOnHighRiskConfig,
    [switch]$FailOnLegacyConfig,
    [switch]$FailOnInvalidSkillManifest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Wait-Health([string]$url, [int]$timeoutSec = 20) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $url -Method GET -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-Sha256Hex([string]$text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash) -replace "-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-HmacSha256Hex([string]$secret, [string]$text) {
    $key = [System.Text.Encoding]::UTF8.GetBytes($secret)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($key)
    try {
        $hash = $hmac.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash) -replace "-", "").ToLowerInvariant()
    } finally {
        $hmac.Dispose()
    }
}

function New-SecExecdHmacHeaders([string]$TraceId, [string]$Method, [string]$Path, [string]$Body, [string]$Secret) {
    $ts = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $bodyHash = Get-Sha256Hex $Body
    $canonical = "$TraceId`n$ts`n$Method`n$Path`n$bodyHash"
    $sig = Get-HmacSha256Hex $Secret $canonical
    return @{
        "Content-Type" = "application/json"
        "X-Trace-Id" = $TraceId
        "X-Approval-Timestamp" = "$ts"
        "X-Approval-Signature" = $sig
    }
}

function Wait-AuditEvent([string]$LogPath, [string]$EventName, [string]$TraceId, [int]$TimeoutSec = 10) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $LogPath) {
            $lines = Get-Content -Path $LogPath -ErrorAction SilentlyContinue
            foreach ($line in $lines) {
                try {
                    $obj = $line | ConvertFrom-Json
                    if ($obj.event -eq $EventName -and $obj.trace_id -eq $TraceId) {
                        return $true
                    }
                } catch {}
            }
        }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

function Invoke-Checked {
    param(
        [scriptblock]$Command,
        [string]$ErrorMessage = "Command failed"
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$ErrorMessage (exit=$LASTEXITCODE)"
    }
}

function Resolve-ExePath([string]$Exe, [string[]]$FallbackCandidates = @()) {
    $exeName = ""
    if ($null -ne $Exe) { $exeName = ([string]$Exe) }
    $exeName = $exeName.Trim()
    if ([string]::IsNullOrWhiteSpace($exeName)) { return $null }

    # If caller passed an explicit path, prefer it (must be a file).
    if (Test-Path -Path $exeName -PathType Leaf) {
        try { return (Resolve-Path $exeName).Path } catch { return $exeName }
    }

    $cmd = Get-Command $exeName -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Path) { return $cmd.Path }

    foreach ($c in $FallbackCandidates) {
        if (-not $c) { continue }
        $p = $c.Trim()
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Get-GoFallbackCandidates {
    $candidates = @()
    if ($env:GOROOT) { $candidates += (Join-Path $env:GOROOT "bin\\go.exe") }
    $candidates += (Join-Path $HOME "scoop\\apps\\go\\current\\bin\\go.exe")
    $candidates += "C:\\Program Files\\Go\\bin\\go.exe"
    $candidates += "C:\\Go\\bin\\go.exe"
    foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
        try { $candidates += (Join-Path $d.Root "tools\\go\\bin\\go.exe") } catch {}
    }
    return $candidates
}

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "Repo root not detected: $RepoRoot"
}

$resolvedPython = Resolve-ExePath -Exe $PythonExe
if (-not $resolvedPython) {
    throw "Python executable not found: '$PythonExe'. Pass -PythonExe with a valid path or ensure it's on PATH."
}
$PythonExe = $resolvedPython

$resolvedGo = Resolve-ExePath -Exe $GoExe -FallbackCandidates (Get-GoFallbackCandidates)
if (-not $resolvedGo) {
    throw "Go executable not found: '$GoExe'. Pass -GoExe (e.g. G:\\tools\\go\\bin\\go.exe) or add go to PATH (or set GOROOT)."
}
$GoExe = $resolvedGo

$configPath = Join-Path $HOME ".nano-claw\config.json"
if (Test-Path $configPath) {
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        $legacyFields = @()
        $hasNetwork = $false
        if ($cfg.tools -and $cfg.tools.network) { $hasNetwork = $true }
        if ($cfg.tools -and $cfg.tools.exec -and -not $hasNetwork) { $legacyFields += "tools.exec" }
        if ($cfg.tools -and $cfg.tools.web -and $cfg.tools.web.search -and -not $hasNetwork) { $legacyFields += "tools.web.search" }
        if ($cfg.tools -and $cfg.tools.web -and $cfg.tools.web.fetch -and -not $hasNetwork) { $legacyFields += "tools.web.fetch" }
        if ($legacyFields.Count -gt 0) {
            Write-Host ""
            Write-Host ("WARNING: legacy tool config detected: " + ($legacyFields -join ", ")) -ForegroundColor Yellow
            if ($FailOnLegacyConfig) {
                throw "Legacy tool config detected while -FailOnLegacyConfig is enabled."
            }
        }

        $allowSensitive = $false
        if ($cfg.tools -and $cfg.tools.policy -and $null -ne $cfg.tools.policy.allowSubagentSensitiveTools) {
            $allowSensitive = [bool]$cfg.tools.policy.allowSubagentSensitiveTools
        }
        if ($allowSensitive) {
            Write-Host ""
            Write-Host "WARNING: allowSubagentSensitiveTools=true detected in config." -ForegroundColor Yellow
            if (-not ${env:nano-claw_ALLOW_SUBAGENT_SENSITIVE_TOOLS}) {
                Write-Host "         Guardrail override env is not set, so hard deny remains active." -ForegroundColor Yellow
            } else {
                Write-Host "         Guardrail override env is set. This is high risk." -ForegroundColor Yellow
                if ($FailOnHighRiskConfig) {
                    throw "High-risk configuration detected: subagent hard guardrail is disabled with explicit env override."
                }
            }
        }
    } catch {
        Write-Host "Warning: unable to parse $configPath for guardrail checks." -ForegroundColor Yellow
    }
}

$goCache = Join-Path $env:TEMP "go-build"
New-Item -ItemType Directory -Force -Path $goCache | Out-Null
$env:GOCACHE = $goCache

Step "Python tests"
Push-Location $RepoRoot
try {
    $env:PYTHONPATH = "."
    Invoke-Checked -Command { & $PythonExe -m pytest -q } -ErrorMessage "Python tests failed"
} finally {
    Pop-Location
}

if ($FailOnInvalidSkillManifest) {
    Step "Strict skills manifest validation"
    Push-Location $RepoRoot
    try {
        $env:PYTHONPATH = "."
$script = @'
from nano_claw.config.loader import load_config
from nano_claw.agent.skills import SkillsLoader

cfg = load_config()
loader = SkillsLoader(cfg.workspace_path)
invalid = [r for r in loader.validate_all_skill_manifests(strict=True) if not r["ok"]]
if invalid:
    for item in invalid:
        print("invalid skill manifest: {}: {}".format(item["name"], item["errors"]))
    raise SystemExit(1)
print("skill manifest validation passed")
'@
        $tmpPy = Join-Path $env:TEMP "nano-claw_skill_manifest_check.py"
        Set-Content -Path $tmpPy -Value $script -Encoding UTF8
        Invoke-Checked -Command { & $PythonExe $tmpPy } -ErrorMessage "Strict skill manifest validation failed"
        Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
    } finally {
        Pop-Location
    }
}

Step "Go tests: sec-execd"
Push-Location (Join-Path $RepoRoot "go\sec-execd")
try {
    Invoke-Checked -Command { & $GoExe test ./... } -ErrorMessage "Go tests failed in go/sec-execd"
} finally {
    Pop-Location
}

Step "Go tests: net-proxy"
Push-Location (Join-Path $RepoRoot "go\net-proxy")
try {
    Invoke-Checked -Command { & $GoExe test ./... } -ErrorMessage "Go tests failed in go/net-proxy"
} finally {
    Pop-Location
}

if ($SkipIntegration) {
    Step "Skipped integration smoke tests (--SkipIntegration)"
    exit 0
}

# Kill any stale sidecar processes left by previous runs. On Windows, killing
# "go run ." does NOT kill the compiled child binary, so we sweep by name first.
Get-Process -Name "sec-execd" -ErrorAction SilentlyContinue | ForEach-Object {
    taskkill /F /T /PID $_.Id 2>$null | Out-Null
}
Get-Process -Name "net-proxy" -ErrorAction SilentlyContinue | ForEach-Object {
    taskkill /F /T /PID $_.Id 2>$null | Out-Null
}

$secProc = $null
$netProc = $null
$httpProc = $null
$secStdout = $null
$secStderr = $null
$netStdout = $null
$netStderr = $null

try {
    Step "Start sec-execd sidecar"
    $env:SEC_EXECD_BIND = "127.0.0.1:4488"
    $env:SEC_EXECD_WORKSPACE = $RepoRoot
    $env:SEC_EXECD_REQUIRE_APPROVAL = "true"
    $env:SEC_EXECD_APPROVAL_TOKEN = ""
    $env:SEC_EXECD_APPROVAL_SECRET = "selftest-secret"
    $secStdout = Join-Path $env:TEMP ("sec-execd-selftest-" + [Guid]::NewGuid().ToString("n") + ".out.log")
    $secStderr = Join-Path $env:TEMP ("sec-execd-selftest-" + [Guid]::NewGuid().ToString("n") + ".err.log")
    Push-Location (Join-Path $RepoRoot "go\sec-execd")
    $secProc = Start-Process -FilePath $GoExe -ArgumentList @("run", ".") -PassThru -WindowStyle Hidden -RedirectStandardOutput $secStdout -RedirectStandardError $secStderr
    Pop-Location
    if (-not (Wait-Health "http://127.0.0.1:4488/healthz" 25)) {
        throw "sec-execd health check failed"
    }

    Step "Smoke test: exec sidecar"
    $execBody = [ordered]@{
        command = "echo sec-execd-ok"
        working_dir = $RepoRoot
        timeout_seconds = 10
    } | ConvertTo-Json -Compress
    $traceExec = "selftest-exec"
    $headersExec = New-SecExecdHmacHeaders -TraceId $traceExec -Method "POST" -Path "/v1/exec" -Body $execBody -Secret "selftest-secret"
    $execResp = Invoke-RestMethod -Uri "http://127.0.0.1:4488/v1/exec" -Method POST -Headers $headersExec -Body $execBody -TimeoutSec 10
    if (-not $execResp.ok) { throw "sec-execd returned failure" }
    if (-not (Wait-AuditEvent -LogPath $secStdout -EventName "exec.request.allowed" -TraceId $traceExec -TimeoutSec 10)) {
        throw "sec-execd audit missing exec.request.allowed for $traceExec"
    }

    Step "Smoke test: sessions (start/list/status/kill)"
    $sessBody = [ordered]@{
        command = "ping -n 6 127.0.0.1 > NUL"
        working_dir = $RepoRoot
        timeout_seconds = 30
    } | ConvertTo-Json -Compress
    $traceSess = "selftest-sess-start"
    $headersSess = New-SecExecdHmacHeaders -TraceId $traceSess -Method "POST" -Path "/v1/sessions/start" -Body $sessBody -Secret "selftest-secret"
    $sessResp = Invoke-RestMethod -Uri "http://127.0.0.1:4488/v1/sessions/start" -Method POST -Headers $headersSess -Body $sessBody -TimeoutSec 10
    if (-not $sessResp.ok) { throw "sessions start failed" }
    if (-not $sessResp.session_id) { throw "sessions start did not return session_id" }
    if (-not (Wait-AuditEvent -LogPath $secStdout -EventName "exec.session.started" -TraceId $traceSess -TimeoutSec 10)) {
        throw "sec-execd audit missing exec.session.started for $traceSess"
    }

    $traceList = "selftest-sess-list"
    $headersList = New-SecExecdHmacHeaders -TraceId $traceList -Method "GET" -Path "/v1/sessions" -Body "" -Secret "selftest-secret"
    $listResp = Invoke-RestMethod -Uri "http://127.0.0.1:4488/v1/sessions" -Method GET -Headers $headersList -TimeoutSec 10
    if (-not $listResp.ok) { throw "sessions list failed" }
    if (-not (Wait-AuditEvent -LogPath $secStdout -EventName "exec.session.list.allowed" -TraceId $traceList -TimeoutSec 10)) {
        throw "sec-execd audit missing exec.session.list.allowed for $traceList"
    }

    $sid = $sessResp.session_id
    $traceStatus = "selftest-sess-status"
    $headersStatus = New-SecExecdHmacHeaders -TraceId $traceStatus -Method "GET" -Path ("/v1/sessions/" + $sid) -Body "" -Secret "selftest-secret"
    $statusResp = Invoke-RestMethod -Uri ("http://127.0.0.1:4488/v1/sessions/" + $sid) -Method GET -Headers $headersStatus -TimeoutSec 10
    if (-not $statusResp.ok) { throw "sessions status failed" }
    if (-not (Wait-AuditEvent -LogPath $secStdout -EventName "exec.session.status.allowed" -TraceId $traceStatus -TimeoutSec 10)) {
        throw "sec-execd audit missing exec.session.status.allowed for $traceStatus"
    }

    $killBody = "{}"
    $traceKill = "selftest-sess-kill"
    $headersKill = New-SecExecdHmacHeaders -TraceId $traceKill -Method "POST" -Path ("/v1/sessions/" + $sid + "/kill") -Body $killBody -Secret "selftest-secret"
    $killResp = Invoke-RestMethod -Uri ("http://127.0.0.1:4488/v1/sessions/" + $sid + "/kill") -Method POST -Headers $headersKill -Body $killBody -TimeoutSec 10
    if (-not $killResp.ok) { throw "sessions kill failed" }
    if (-not (Wait-AuditEvent -LogPath $secStdout -EventName "exec.session.kill.allowed" -TraceId $traceKill -TimeoutSec 10)) {
        throw "sec-execd audit missing exec.session.kill.allowed for $traceKill"
    }

    Step "Start net-proxy sidecar"
    $env:NET_PROXY_BIND = "127.0.0.1:4499"
    $env:NET_PROXY_ALLOW_DOMAINS = "127.0.0.1,localhost,api.search.brave.com"
    $env:NET_PROXY_DENY_DOMAINS = ""
    $env:NET_PROXY_TIMEOUT_SEC = "10"
    $netStdout = Join-Path $env:TEMP ("net-proxy-selftest-" + [Guid]::NewGuid().ToString("n") + ".out.log")
    $netStderr = Join-Path $env:TEMP ("net-proxy-selftest-" + [Guid]::NewGuid().ToString("n") + ".err.log")
    Push-Location (Join-Path $RepoRoot "go\net-proxy")
    $netProc = Start-Process -FilePath $GoExe -ArgumentList @("run", ".") -PassThru -WindowStyle Hidden -RedirectStandardOutput $netStdout -RedirectStandardError $netStderr
    Pop-Location
    if (-not (Wait-Health "http://127.0.0.1:4499/healthz" 25)) {
        throw "net-proxy health check failed"
    }

    Step "Start local HTTP fixture server"
    $fixtureDir = Join-Path $env:TEMP "nano-claw-selftest-fixture"
    New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
    Set-Content -Path (Join-Path $fixtureDir "index.html") -Value "net-proxy-ok" -Encoding UTF8
    $httpProc = Start-Process -FilePath $PythonExe -ArgumentList @("-m", "http.server", "18080", "--bind", "127.0.0.1") -WorkingDirectory $fixtureDir -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 1

    Step "Smoke test: fetch proxy"
    $fetchBody = @{
        url = "http://127.0.0.1:18080/index.html"
        max_bytes = 1024
    } | ConvertTo-Json
    $fetchResp = Invoke-RestMethod -Uri "http://127.0.0.1:4499/v1/fetch" -Method POST -Headers @{
        "Content-Type" = "application/json"
        "X-Trace-Id" = "selftest-fetch"
    } -Body $fetchBody -TimeoutSec 10
    if (-not $fetchResp.ok) { throw "net-proxy fetch returned failure" }
    if (-not ($fetchResp.body -match "net-proxy-ok")) { throw "net-proxy fetch body mismatch" }
    if (-not (Wait-AuditEvent -LogPath $netStdout -EventName "net.fetch.allowed" -TraceId "selftest-fetch" -TimeoutSec 10)) {
        throw "net-proxy audit missing net.fetch.allowed for selftest-fetch"
    }

    if ($env:BRAVE_API_KEY) {
        Step "Smoke test: search proxy (BRAVE_API_KEY detected)"
        $searchBody = @{
            query = "nano-claw"
            count = 1
            api_key = $env:BRAVE_API_KEY
        } | ConvertTo-Json
        $searchResp = Invoke-RestMethod -Uri "http://127.0.0.1:4499/v1/search" -Method POST -Headers @{
            "Content-Type" = "application/json"
            "X-Trace-Id" = "selftest-search"
        } -Body $searchBody -TimeoutSec 20
        if (-not $searchResp.ok) { throw "net-proxy search returned failure" }
        if (-not (Wait-AuditEvent -LogPath $netStdout -EventName "net.search.allowed" -TraceId "selftest-search" -TimeoutSec 10)) {
            throw "net-proxy audit missing net.search.allowed for selftest-search"
        }
    } else {
        Step "Skip search proxy smoke test (BRAVE_API_KEY not set)"
    }

    Step "All local self-tests passed"
} finally {
    if ($httpProc -and -not $httpProc.HasExited) {
        Stop-Process -Id $httpProc.Id -Force -ErrorAction SilentlyContinue
    }
    # Use taskkill /T to kill the whole process tree: "go run ." spawns the
    # compiled binary as a child; Stop-Process alone leaves that child running.
    if ($netProc -and -not $netProc.HasExited) {
        try {
            cmd /c "taskkill /F /T /PID $($netProc.Id) >nul 2>nul" | Out-Null
        } catch {}
    }
    if ($secProc -and -not $secProc.HasExited) {
        try {
            cmd /c "taskkill /F /T /PID $($secProc.Id) >nul 2>nul" | Out-Null
        } catch {}
    }
    if ($secStdout -and (Test-Path $secStdout)) {
        Remove-Item $secStdout -Force -ErrorAction SilentlyContinue
    }
    if ($secStderr -and (Test-Path $secStderr)) {
        Remove-Item $secStderr -Force -ErrorAction SilentlyContinue
    }
    if ($netStdout -and (Test-Path $netStdout)) {
        Remove-Item $netStdout -Force -ErrorAction SilentlyContinue
    }
    if ($netStderr -and (Test-Path $netStderr)) {
        Remove-Item $netStderr -Force -ErrorAction SilentlyContinue
    }
}
