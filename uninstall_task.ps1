$ErrorActionPreference = 'Stop'

$taskName = 'ADTAgentMinimal'
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed scheduled task: $taskName"
