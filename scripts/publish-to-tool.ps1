# Copy built Windows/Linux agents into the sibling Vizhi web app (adt/public/).
# Run from anywhere: powershell -File .\scripts\publish-to-tool.ps1
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$public = Join-Path (Split-Path $root -Parent) 'adt\public'

New-Item -ItemType Directory -Force -Path $public | Out-Null

$winSrc = Join-Path $root 'windows\dist\adt-agent.exe'
$winDst = Join-Path $public 'vizhi-agent.exe'
if (Test-Path $winSrc) {
  Copy-Item -Force $winSrc $winDst
  Write-Host "Published Windows agent -> $winDst"
} else {
  Write-Host "Skip Windows: $winSrc not found (run windows/build.ps1)"
}

$linuxSrc = Join-Path $root 'linux\dist\adt-agent'
$linuxDst = Join-Path $public 'vizhi-agent-linux'
if (Test-Path $linuxSrc) {
  Copy-Item -Force $linuxSrc $linuxDst
  Write-Host "Published Linux agent -> $linuxDst"
} else {
  Write-Host "Skip Linux: $linuxSrc not found (run linux/build.sh on Linux)"
}
