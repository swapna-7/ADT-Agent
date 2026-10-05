# Re-register ADTAgent scheduled task (dual trigger + IgnoreNew).
# Run as Administrator on endpoints that still have the old AtStartup-only task.
# Non-destructive — the agent keeps running.

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw 'Run this script as Administrator.'
}

$installDir = 'C:\Program Files\ADT Agent'
$agentPath = Join-Path $installDir 'adt-agent.exe'
if (-not (Test-Path $agentPath)) {
    throw "Agent binary not found: $agentPath"
}

$action = New-ScheduledTaskAction -Execute $agentPath

$triggerStartup = New-ScheduledTaskTrigger -AtStartup
$triggerRepeat = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -Once `
    -At (Get-Date)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName 'ADTAgent' `
    -Action $action `
    -Trigger @($triggerStartup, $triggerRepeat) `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host 'Task re-registered:'
Get-ScheduledTask -TaskName 'ADTAgent' |
    Get-ScheduledTaskInfo |
    Select-Object LastRunTime, NextRunTime, LastTaskResult
