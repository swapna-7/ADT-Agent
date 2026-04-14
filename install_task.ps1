$ErrorActionPreference = 'Stop'

$taskName = 'ADTAgentMinimal'
$projectRoot = (Resolve-Path $PSScriptRoot).Path
$scriptPath = Join-Path $projectRoot 'agent.py'

if (-not (Test-Path $scriptPath)) {
    throw "Could not find $scriptPath"
}

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue)
$python = (Get-Command python -ErrorAction SilentlyContinue)

if ($pythonw) {
    $pythonExe = $pythonw.Path
} elseif ($python) {
    $pythonExe = $python.Path
} else {
    throw "Python not found in PATH. Install Python 3.10+ and retry."
}

$action = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$scriptPath`"" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Description 'Minimal Windows background Python metrics agent' -Force | Out-Null
Write-Host "Installed scheduled task: $taskName"
Write-Host "Command: $pythonExe $scriptPath"
