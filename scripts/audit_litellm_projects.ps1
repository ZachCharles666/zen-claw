param(
    [Parameter(Mandatory = $true, Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$ProjectRoots
)

$ErrorActionPreference = "Stop"

function Get-LiteLLMVersionFromPyproject {
    param(
        [string]$PyprojectPath
    )

    if (-not (Test-Path $PyprojectPath)) {
        return $null
    }

    $match = Select-String -Path $PyprojectPath -Pattern '^\s*"litellm([^"]*)"\s*,?\s*$' | Select-Object -First 1
    if ($null -eq $match) {
        return $null
    }

    return $match.Matches[0].Groups[1].Value.Trim()
}

function Get-VenvLiteLLMVersion {
    param(
        [string]$Root
    )

    $pythonExe = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $pythonExe)) {
        return $null
    }

    try {
        $version = & $pythonExe -c "import importlib.metadata as m; print(m.version('litellm'))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return ($version | Out-String).Trim()
        }
    }
    catch {
        return $null
    }

    return $null
}

function Test-UsesLiteLLM {
    param(
        [string]$Root
    )

    $hits = rg -n "litellm" -S $Root 2>$null
    return [bool]$hits
}

function Get-EnvFiles {
    param(
        [string]$Root
    )

    $items = Get-ChildItem -Force $Root -Filter ".env*" -ErrorAction SilentlyContinue
    return @($items | ForEach-Object { $_.FullName })
}

function Get-RiskSummary {
    param(
        [string]$DependencySpec,
        [string]$InstalledVersion,
        [bool]$UsesLiteLLM,
        [string[]]$EnvFiles
    )

    $risks = New-Object System.Collections.Generic.List[string]

    if ($UsesLiteLLM) {
        $risks.Add("uses_litellm")
    }

    if ($null -ne $DependencySpec) {
        if ($DependencySpec -match '^[=]{2}') {
            $risks.Add("dependency_pinned")
        }
        else {
            $risks.Add("dependency_not_pinned")
        }
    }
    else {
        $risks.Add("no_pyproject_litellm_spec")
    }

    if ([string]::IsNullOrWhiteSpace($InstalledVersion)) {
        $risks.Add("venv_litellm_not_installed_or_no_venv")
    }
    else {
        $risks.Add("venv_litellm_$InstalledVersion")
    }

    if ($EnvFiles.Count -gt 0) {
        $risks.Add("env_files_present")
    }

    return ($risks -join ",")
}

$rows = foreach ($root in $ProjectRoots) {
    $resolved = Resolve-Path $root -ErrorAction SilentlyContinue
    if ($null -eq $resolved) {
        [pscustomobject]@{
            project_root      = $root
            exists            = $false
            pyproject_litellm = $null
            venv_litellm      = $null
            uses_litellm      = $false
            env_files         = ""
            risk_summary      = "missing_path"
        }
        continue
    }

    $projectRoot = $resolved.Path
    $pyprojectPath = Join-Path $projectRoot "pyproject.toml"
    $dependencySpec = Get-LiteLLMVersionFromPyproject -PyprojectPath $pyprojectPath
    $installedVersion = Get-VenvLiteLLMVersion -Root $projectRoot
    $usesLiteLLM = Test-UsesLiteLLM -Root $projectRoot
    $envFiles = Get-EnvFiles -Root $projectRoot

    [pscustomobject]@{
        project_root      = $projectRoot
        exists            = $true
        pyproject_litellm = $dependencySpec
        venv_litellm      = $installedVersion
        uses_litellm      = $usesLiteLLM
        env_files         = ($envFiles -join ";")
        risk_summary      = Get-RiskSummary -DependencySpec $dependencySpec -InstalledVersion $installedVersion -UsesLiteLLM:$usesLiteLLM -EnvFiles $envFiles
    }
}

$rows | Format-Table -AutoSize
