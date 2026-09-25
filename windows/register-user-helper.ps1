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

# AtLogOn tasks registered by the SYSTEM agent need an explicit interactive user.
$user = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName
if (-not $user) {
    throw 'No interactive user is logged in — log on at the console, then re-run this script.'
}
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

Register-ScheduledTask `
    -TaskName 'ADTAgentHelper' `
    -Action $helperAction `
    -Trigger $helperTrigger `
    -Settings $helperSettings `
    -Principal $principal `
    -Force | Out-Null

Start-ScheduledTask -TaskName 'ADTAgentHelper' -ErrorAction SilentlyContinue

Write-Host "Registered and started ADTAgentHelper"
