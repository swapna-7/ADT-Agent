param()

$ErrorActionPreference = 'Stop'

$windowsDir = (Resolve-Path $PSScriptRoot).Path
$repoRoot = (Resolve-Path (Join-Path $windowsDir '..')).Path
$commonDir = Join-Path $repoRoot 'common'
Set-Location $windowsDir

if (Test-Path (Join-Path $windowsDir '.env')) {
    throw "Do not build with .env present in windows/ - credentials must not be bundled"
}
if (Test-Path (Join-Path $repoRoot '.env')) {
    throw "Do not build with .env present at repo root - credentials must not be bundled"
}

$apiBase = $env:VIZHI_API_BASE
if (-not $apiBase) {
    throw "VIZHI_API_BASE not set. Example: `$env:VIZHI_API_BASE='https://vizhi.example.com'"
}

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
    throw "Python not found in PATH. Install Python 3.10+ and retry."
}

Write-Host "==> Generating api_config.py"
& $python.Path (Join-Path $repoRoot 'generate_api_config.py') $apiBase

Write-Host "==> Installing build dependencies"
& $python.Path -m pip install --upgrade pip | Out-Null
& $python.Path -m pip install -r requirements.txt

$pyinstaller = (Get-Command pyinstaller -ErrorAction SilentlyContinue)
if (-not $pyinstaller) {
    $pyinstallerPath = Join-Path (Split-Path $python.Path) 'Scripts\pyinstaller.exe'
    if (Test-Path $pyinstallerPath) {
        $pyinstaller = Get-Command $pyinstallerPath
    } else {
        throw "pyinstaller not found after install. Check your Python environment."
    }
}

Write-Host "==> Cleaning previous build artifacts"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
    (Join-Path $windowsDir 'build'), `
    (Join-Path $windowsDir 'dist'), `
    (Join-Path $windowsDir 'adt-agent.spec')

$apiConfig = Join-Path $repoRoot 'api_config.py'
if (-not (Test-Path $apiConfig)) {
    throw "api_config.py missing after generate step"
}

$pyiArgs = @(
    '--onefile',
    '--name', 'adt-agent',
    '--uac-admin',
    '--clean',
    '--noconfirm',
    '--paths', $commonDir,
    '--hidden-import', 'api_config',
    '--hidden-import', 'win32com',
    '--hidden-import', 'win32com.client',
    '--hidden-import', 'win32com.client.gencache',
    '--hidden-import', 'pythoncom',
    '--hidden-import', 'pywintypes',
    '--hidden-import', 'packaging',
    '--hidden-import', 'packaging.version',
    '--hidden-import', 'psutil',
    '--hidden-import', 'config_io',
    '--hidden-import', 'api_base',
    '--hidden-import', 'first_run',
    '--hidden-import', 'scheduler',
    '--hidden-import', 'command_poller',
    '--hidden-import', 'enrollment',
    '--hidden-import', 'normalize',
    '--hidden-import', 'patch_runner',
    '--hidden-import', 'report',
    '--hidden-import', 'self_update',
    '--hidden-import', 'software_quality',
    '--hidden-import', 'updates',
    '--hidden-import', 'version',
    '--hidden-import', 'inventory_windows',
    '--add-data', "$apiConfig;."
)

$iconFile = Join-Path $windowsDir 'adt-agent.ico'
if (Test-Path $iconFile) {
    $pyiArgs += @('--icon', 'adt-agent.ico')
}

$pyiArgs += 'agent.py'

Write-Host "==> Running PyInstaller"
& $pyinstaller.Path @pyiArgs

$exePath = Join-Path $windowsDir 'dist\adt-agent.exe'
if (-not (Test-Path $exePath)) {
    throw "Build failed: $exePath not found."
}

$size = [math]::Round((Get-Item $exePath).Length / 1MB, 2)
Write-Host ""
Write-Host "==> Build complete"
Write-Host "    Executable: $exePath ($size MB)"
Write-Host "    Install: powershell -ExecutionPolicy Bypass -File install-windows.ps1"
