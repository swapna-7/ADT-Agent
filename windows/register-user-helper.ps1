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

# AtLogOn tasks registered by the SYSTEM agent need an explicit interactive user.
$user = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName
if (-not $user) {
    throw 'No interactive user is logged in — log on at the console, then re-run this script.'
}

$helperTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user

$helperSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances StopExisting

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

Register-ScheduledTask `
    -TaskName 'ADTAgentHelper' `
    -Action $helperAction `
    -Trigger $helperTrigger `
    -Settings $helperSettings `
    -Principal $principal `
    -Force | Out-Null

# Start-ScheduledTask is unreliable for AtLogOn tasks from an elevated shell — use schtasks.
schtasks /Run /TN 'ADTAgentHelper' | Out-Null
Start-Sleep -Seconds 3

Write-Host "Registered ADTAgentHelper for $user"
if (Test-Path (Join-Path $env:ProgramData 'ADT Agent\user_helper.log')) {
    Get-Content (Join-Path $env:ProgramData 'ADT Agent\user_helper.log') -Tail 3
} else {
    Write-Host "Helper log not created yet — open a non-admin PowerShell and run:"
    Write-Host "  powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$HelperPath`""
}
