$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run uninstall-windows.ps1 as Administrator."
}

Stop-ScheduledTask -TaskName 'ADTAgent' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'ADTAgent' -Confirm:$false -ErrorAction SilentlyContinue

Remove-Item 'C:\Program Files\ADT Agent' -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "ADT Agent uninstalled. Data directory preserved at:"
Write-Host "  C:\ProgramData\ADT Agent"
