param(
    [string]$BinaryPath = ""
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run install-windows.ps1 as Administrator."
}

$repoRoot = (Resolve-Path $PSScriptRoot).Path
if (-not $BinaryPath) {
    $BinaryPath = Join-Path $repoRoot 'windows\dist\adt-agent.exe'
}
if (-not (Test-Path $BinaryPath)) {
    throw "Binary not found: $BinaryPath. Build first with VIZHI_API_BASE set."
}

$installDir = 'C:\Program Files\ADT Agent'
$dataDir = 'C:\ProgramData\ADT Agent'
$installExe = Join-Path $installDir 'adt-agent.exe'

New-Item -ItemType Directory -Force -Path $installDir, $dataDir | Out-Null
Copy-Item -Force $BinaryPath $installExe

# Restrict install dir to SYSTEM and Administrators.
$acl = Get-Acl $installDir
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
$systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$adminSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $systemSid, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $adminSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
)))
Set-Acl $installDir $acl

$action = New-ScheduledTaskAction -Execute $installExe
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest
Register-ScheduledTask `
    -TaskName 'ADTAgent' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host ""
Write-Host "==> Interactive enrollment (enter organisation code when prompted)"
& $installExe

Write-Host ""
Write-Host "Installation complete. The agent will start at next boot"
Write-Host "or you can start it now by running the Scheduled Task:"
Write-Host "  Start-ScheduledTask -TaskName ADTAgent"
