# Full clean install: wipe (admin) -> install-windows.ps1 -> verify
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$wipe = Join-Path $PSScriptRoot 'wipe-agent.ps1'
$verify = Join-Path $PSScriptRoot 'verify-agent.ps1'
$install = Join-Path $root 'install-windows.ps1'

Write-Host "==> Wipe (requires Administrator)"
Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $wipe
)

Write-Host "==> Install (enrollment code prompt)"
Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $install
)

Write-Host "==> Verify"
& $verify
