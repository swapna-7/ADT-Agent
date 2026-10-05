# Register ADTAgentHelper — user-session display helper (AtLogOn, runs as logged-in user).
param(
    [string]$HelperPath = (Join-Path $env:ProgramData 'ADT Agent\user_helper.ps1')
)

$ErrorActionPreference = 'Stop'

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $elevated) {
    throw @'
Access denied: registering ADTAgentHelper requires Administrator PowerShell.
1) Run this script in an Administrator PowerShell (copy + register).
2) Then in a NORMAL (non-admin) PowerShell run:
     schtasks /Run /TN ADTAgentHelper
Do not start the task from the Administrator window — that leaves it Queued.
'@
}

if (-not (Test-Path $HelperPath)) {
    throw "user_helper.ps1 not found at: $HelperPath"
}

$quotedHelper = $HelperPath.Replace("'", "''")
$launcherPath = Join-Path (Split-Path -Parent $HelperPath) 'user_helper_launch.vbs'
@(
    'Option Explicit'
    'Dim sh, cmd'
    "cmd = `"powershell.exe -WindowStyle Hidden -NoProfile -STA -NonInteractive -ExecutionPolicy Bypass -Command `"`"& '$quotedHelper'`"`"`""
    'Set sh = CreateObject("Wscript.Shell")'
    'sh.Run cmd, 0, True'
) | Set-Content -LiteralPath $launcherPath -Encoding ASCII
$psArg = "//B //Nologo `"$launcherPath`""
$helperAction = New-ScheduledTaskAction `
    -Execute 'C:\Windows\System32\wscript.exe' `
    -Argument $psArg

# AtLogOn tasks registered by the SYSTEM agent need an explicit interactive user.
$user = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName
if (-not $user) {
    throw 'No interactive user is logged in — log on at the console, then re-run this script.'
}

$helperTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user

$helperSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive

Register-ScheduledTask `
    -TaskName 'ADTAgentHelper' `
    -Action $helperAction `
    -Trigger $helperTrigger `
    -Settings $helperSettings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Registered ADTAgentHelper for $user"
Write-Host 'Now open a NORMAL (non-admin) PowerShell and run:'
Write-Host '  schtasks /Run /TN ADTAgentHelper'
