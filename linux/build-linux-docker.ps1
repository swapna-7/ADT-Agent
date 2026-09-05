# Build the Linux agent on Windows using Docker (Ubuntu container).
# Run from the repo root:  powershell -File .\linux\build-linux-docker.ps1
# Requires Docker Desktop to be running.
#
# Output:
#   adt-agent/linux/dist/adt-agent
#   adt/public/vizhi-agent-linux  (copy for web tool / email)

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$linuxDir = Join-Path $root 'linux'
$publicDir = Join-Path (Split-Path $root -Parent) 'adt\public'

Write-Host "==> Checking Docker..."
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not running. Start Docker Desktop, wait until it is ready, then run this script again."
}

Write-Host "==> Building Linux agent in Ubuntu container (first run may take several minutes)..."
$mount = "${root}:/work"
docker run --rm `
    -v $mount `
    -w /work/linux `
    ubuntu:24.04 `
    bash -c @"
set -euo pipefail
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip binutils >/dev/null
chmod +x build.sh
./build.sh
"@

$built = Join-Path $linuxDir 'dist\adt-agent'
if (-not (Test-Path $built)) {
    throw "Build failed: $built not found"
}

New-Item -ItemType Directory -Force -Path $publicDir | Out-Null
$published = Join-Path $publicDir 'vizhi-agent-linux'
Copy-Item -Force $built $published

$sizeMb = [math]::Round((Get-Item $published).Length / 1MB, 2)
Write-Host ""
Write-Host "==> Done"
Write-Host "    Built:     $built"
Write-Host "    Published: $published ($sizeMb MB)"
Write-Host ""
Write-Host "Email attachment: attach vizhi-agent-linux (or zip it first)."
Write-Host "Recipient install (on target Linux host, as root):"
Write-Host "  sudo ./linux/install.sh"
