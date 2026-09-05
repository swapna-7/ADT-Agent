# Run as Administrator for full cleanup (Program Files + ProgramData).
$ErrorActionPreference = 'SilentlyContinue'
schtasks /End /TN ADTAgent
schtasks /Delete /TN ADTAgent /F

# Kill only the installed copy under Program Files (not arbitrary dist\adt-agent.exe).
$installExe = 'C:\Program Files\ADT Agent\adt-agent.exe'
Get-CimInstance Win32_Process -Filter "Name='adt-agent.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $installExe } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Remove-Item -Recurse -Force 'C:\Program Files\ADT Agent'
Remove-Item -Recurse -Force 'C:\ProgramData\ADT Agent'
Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'ADT Agent')
Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE '.adt-agent')
Write-Host "exe exists: $(Test-Path 'C:\Program Files\ADT Agent\adt-agent.exe')"
Write-Host "config exists: $(Test-Path 'C:\ProgramData\ADT Agent\config.json')"
$q = schtasks /Query /TN ADTAgent 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "task: removed" } else { Write-Host "task: still present" }
