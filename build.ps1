param()

# Build the Windows agent. Requires VIZHI_API_BASE in the environment.
$ErrorActionPreference = 'Stop'
$windowsBuild = Join-Path $PSScriptRoot 'windows\build.ps1'
& $windowsBuild
if ($LASTEXITCODE) { exit $LASTEXITCODE }
