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

$helperSrc = Join-Path $repoRoot 'windows\user_helper.ps1'
$helperDest = Join-Path $dataDir 'user_helper.ps1'
$apiBase = if ($env:VIZHI_API_BASE) { $env:VIZHI_API_BASE.Trim().TrimEnd('/') } else { 'https://vizhi.rcsaware.com' }

if (Test-Path $helperSrc) {
    Copy-Item -Force $helperSrc $helperDest
    Write-Host "user_helper.ps1 copied from repo ($((Get-Item $helperDest).Length) bytes)"
} else {
    Write-Host "Registering user-session helper..."
    try {
        Invoke-WebRequest `
            -Uri "$apiBase/api/agent/download/user-helper" `
            -OutFile $helperDest `
            -UseBasicParsing `
            -TimeoutSec 30
        Write-Host "user_helper.ps1 downloaded ($((Get-Item $helperDest).Length) bytes)"
    } catch {
        Write-Warning "Could not download user_helper.ps1: $_"
        Write-Warning "Desktop alerts and wallpaper may not show on screen."
    }
}

if (Test-Path $helperDest) {
    # Space-safe -Command (matches user_helper_task.build_helper_task_arguments)
    $quotedHelper = $helperDest.Replace("'", "''")
    $helperArgs = "-WindowStyle Hidden -NoProfile -STA -NonInteractive -ExecutionPolicy Bypass -Command `"& '$quotedHelper'`""
    $helperAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $helperArgs
    $interactiveUser = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName
    if (-not $interactiveUser) {
        $interactiveUser = "$env:USERDOMAIN\$env:USERNAME"
    }
    $helperTrigger = New-ScheduledTaskTrigger -AtLogOn -User $interactiveUser
    $helperSettings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -MultipleInstances IgnoreNew
    $helperPrincipal = New-ScheduledTaskPrincipal -UserId $interactiveUser -LogonType Interactive
    Register-ScheduledTask `
        -TaskName 'ADTAgentHelper' `
        -Action $helperAction `
        -Trigger $helperTrigger `
        -Settings $helperSettings `
        -Principal $helperPrincipal `
        -Force | Out-Null
    Write-Host "ADTAgentHelper task registered ($helperDest) for $interactiveUser"
    schtasks /Run /TN 'ADTAgentHelper' | Out-Null
    Write-Host 'ADTAgentHelper start requested'
} else {
    Write-Warning "user_helper.ps1 not found at $helperSrc and download failed — helper not registered"
}

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
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
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

# ADTAgentHelper is registered above when user_helper.ps1 is present.

Write-Host ""
Write-Host "==> Interactive enrollment (enter organisation code when prompted)"
& $installExe

Write-Host ""
Write-Host "Installation complete. The agent will start at next boot"
Write-Host "or you can start it now by running the Scheduled Task:"
Write-Host "  Start-ScheduledTask -TaskName ADTAgent"
