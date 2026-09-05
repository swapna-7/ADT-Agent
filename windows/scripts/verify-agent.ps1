$ErrorActionPreference = 'Continue'
Write-Host "=== ADT Agent verification ==="
$exe = 'C:\Program Files\ADT Agent\adt-agent.exe'
$config = 'C:\ProgramData\ADT Agent\config.json'
$log = 'C:\ProgramData\ADT Agent\agent.log'
$installLog = 'C:\ProgramData\ADT Agent\install.log'

Write-Host "Installed exe: $(Test-Path $exe)  ($exe)"
Write-Host "config.json:   $(Test-Path $config)"
if (Test-Path $config) {
    $cfg = Get-Content $config -Raw | ConvertFrom-Json
    if ($cfg.DEVICE_TOKEN) { Write-Host "  DEVICE_TOKEN: present" } else { Write-Host "  DEVICE_TOKEN: MISSING (not enrolled)" }
    if ($cfg.ENDPOINT_ID) { Write-Host "  ENDPOINT_ID: $($cfg.ENDPOINT_ID.ToString().Substring(0,[Math]::Min(8,$cfg.ENDPOINT_ID.ToString().Length)))..." }
}
$task = schtasks /Query /TN ADTAgent 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "Scheduled task ADTAgent: present"
    schtasks /Query /TN ADTAgent /V /FO LIST | Select-String 'Task To Run|Status|Last Run'
} else {
    Write-Host "Scheduled task ADTAgent: NOT FOUND"
}
$proc = Get-Process adt-agent -ErrorAction SilentlyContinue
if ($proc) { Write-Host "Process: running (PID $($proc.Id))" } else { Write-Host "Process: not running" }
if (Test-Path $log) {
    Write-Host "--- agent.log (last 10 lines) ---"
    Get-Content $log -Tail 10
}
if (Test-Path $installLog) {
    Write-Host "--- install.log (last 15 lines) ---"
    Get-Content $installLog -Tail 15
}
