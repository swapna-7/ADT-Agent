# Validates windows/user_helper.ps1 before release.
# 1) Parse with Windows PowerShell 5.1 (powershell.exe)
# 2) Optional smoke: run via the same -Command invocation as register_helper_task()
param(
    [string]$ScriptPath = (Join-Path $PSScriptRoot '..\user_helper.ps1'),
    [switch]$SmokeTest,
    [switch]$ParseOnly
)

$ErrorActionPreference = 'Stop'

function Test-UserHelperParse {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Error "Script not found: $Path"
        exit 1
    }
    $errors = $null
    $tokens = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $Path).Path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors -and $errors.Count -gt 0) {
        foreach ($err in $errors) {
            Write-Error $err.Message
        }
        exit 1
    }
    Write-Host "Parse OK: $Path"
}

# Must match user_helper_task.build_helper_task_arguments() — space-safe -Command invoke.
function Get-HelperTaskArguments {
    param([Parameter(Mandatory)][string]$HelperPath)
    $quoted = $HelperPath.Replace("'", "''")
    return "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""& '$quoted'"""
}

function Test-UserHelperSmoke {
    param([Parameter(Mandatory)][string]$SourcePath)

    $ciRoot = Join-Path $env:TEMP 'ADT Agent CI Space'
    $env:ProgramData = Join-Path $ciRoot 'ProgramData Root'
    $dataDir = Join-Path $env:ProgramData 'ADT Agent'
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

    $helperPath = Join-Path $dataDir 'user_helper.ps1'
    Copy-Item -Force $SourcePath $helperPath

    if ($dataDir -notmatch ' ') {
        Write-Error 'Smoke staging path must contain a space (ProgramData ADT Agent).'
        exit 1
    }

    $arg = Get-HelperTaskArguments -HelperPath $helperPath
    Write-Host "Smoke invoke: powershell.exe $arg"

    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $arg -PassThru -WindowStyle Hidden
    $logPath = Join-Path $dataDir 'user_helper.log'
    $fallback = Join-Path $env:TEMP 'adt-agent-user_helper.log'
    $deadline = (Get-Date).AddSeconds(15)

    try {
        while ((Get-Date) -lt $deadline) {
            foreach ($candidate in @($logPath, $fallback)) {
                if (Test-Path $candidate) {
                    $content = Get-Content $candidate -Raw -ErrorAction SilentlyContinue
                    if ($content -and $content -match 'ADTAgentHelper started') {
                        Write-Host "Smoke OK: log line found in $candidate"
                        return
                    }
                }
            }
            Start-Sleep -Seconds 1
        }
        Write-Error 'Smoke test failed: ADTAgentHelper started not written within 15s'
        exit 1
    } finally {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

Test-UserHelperParse -Path $ScriptPath

if ($ParseOnly) {
    exit 0
}

if ($SmokeTest) {
    Test-UserHelperSmoke -SourcePath $ScriptPath
}
