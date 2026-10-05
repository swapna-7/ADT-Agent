# Debug-only process-start watcher for remaining console flashes (session c15c98).
$ErrorActionPreference = 'SilentlyContinue'
$paths = @(
    'c:\Users\swapn\OneDrive\Desktop\Rex Projects\Vizhi\debug-c15c98.log',
    'C:\ProgramData\ADT Agent\debug-c15c98.log'
)

function Write-VizhiDbg {
    param([string]$HypothesisId, [string]$Message, [hashtable]$Data)
    $payload = @{
        sessionId = 'c15c98'
        runId = 'pre-fix'
        hypothesisId = $HypothesisId
        location = 'debug-ps-flash-watch.ps1'
        message = $Message
        data = $Data
        timestamp = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    } | ConvertTo-Json -Compress
    foreach ($p in $paths) {
        try { Add-Content -LiteralPath $p -Value $payload -Encoding utf8 } catch {}
    }
}

Write-VizhiDbg -HypothesisId 'E' -Message 'watcher started' -Data @{ pid = $PID }

Register-CimIndicationEvent -Query @"
SELECT * FROM __InstanceCreationEvent WITHIN 1
WHERE TargetInstance ISA 'Win32_Process'
AND (TargetInstance.Name = 'powershell.exe'
  OR TargetInstance.Name = 'pwsh.exe'
  OR TargetInstance.Name = 'schtasks.exe'
  OR TargetInstance.Name = 'cmd.exe'
  OR TargetInstance.Name = 'conhost.exe'
  OR TargetInstance.Name = 'wscript.exe')
"@ -SourceIdentifier 'VizhiDbgC15c98' -Action {
    $proc = $Event.SourceEventArgs.NewEvent.TargetInstance
    $name = [string]$proc.Name
    $cmd = [string]$proc.CommandLine
    if ($cmd -like '*debug-ps-flash-watch*') { return }
    $hid = 'E'
    if ($name -match 'powershell|pwsh') { $hid = 'B' }
    elseif ($name -eq 'wscript.exe') { $hid = 'B' }
    elseif ($name -eq 'schtasks.exe') { $hid = 'A' }
    elseif ($name -eq 'cmd.exe') { $hid = 'C' }
    $parentName = ''
    try {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction Stop
        $parentName = [string]$parent.Name
    } catch {}
    $line = (@{
        sessionId = 'c15c98'
        runId = 'pre-fix'
        hypothesisId = $hid
        location = 'debug-ps-flash-watch.ps1'
        message = 'process started'
        data = @{
            name = $name
            pid = [int]$proc.ProcessId
            parentPid = [int]$proc.ParentProcessId
            parentName = $parentName
            cmdPrefix = $cmd.Substring(0, [Math]::Min(180, $cmd.Length))
        }
        timestamp = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    } | ConvertTo-Json -Compress)
    foreach ($p in @(
        'c:\Users\swapn\OneDrive\Desktop\Rex Projects\Vizhi\debug-c15c98.log',
        'C:\ProgramData\ADT Agent\debug-c15c98.log'
    )) {
        try { Add-Content -LiteralPath $p -Value $line -Encoding utf8 } catch {}
    }
}

while ($true) { Start-Sleep -Seconds 30 }
