# Register ADTAgentHelper — user-session display helper (AtLogOn, runs as logged-in user).
param(
    [string]$HelperPath = (Join-Path ${env:ProgramFiles} 'ADT Agent\user_helper.ps1')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $HelperPath)) {
    throw "user_helper.ps1 not found at: $HelperPath"
}

$helperAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File `"$HelperPath`""

$helperTrigger = New-ScheduledTaskTrigger -AtLogOn

$helperSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName 'ADTAgentHelper' `
    -Action $helperAction `
    -Trigger $helperTrigger `
    -Settings $helperSettings `
    -Force | Out-Null

Start-ScheduledTask -TaskName 'ADTAgentHelper' -ErrorAction SilentlyContinue

Write-Host "Registered and started ADTAgentHelper"
