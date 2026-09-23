# Vizhi ADT user-session display helper.
# Runs as the logged-in user (ADTAgentHelper scheduled task at logon).
# Reads pending_display.json written by the SYSTEM agent and applies display tasks.

$ErrorActionPreference = 'Continue'

$DataDir = Join-Path $env:ProgramData 'ADT Agent'
$PendingFile = Join-Path $DataDir 'pending_display.json'
$ResultFile = Join-Path $DataDir 'display_results.json'
$LogFile = Join-Path $DataDir 'user_helper.log'

function Write-HelperLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

Write-HelperLog 'ADTAgentHelper started (pid=' + $PID + ')'

while ($true) {
    Start-Sleep -Seconds 10

    if (-not (Test-Path $PendingFile)) { continue }

    try {
        $raw = Get-Content $PendingFile -Raw -ErrorAction Stop
        if (-not $raw.Trim()) { continue }
        $tasks = $raw | ConvertFrom-Json
        if ($null -eq $tasks) { continue }
        if ($tasks -isnot [System.Array]) {
            $tasks = @($tasks)
        }

        $results = @()

        foreach ($task in $tasks) {
            $result = @{
                id     = $task.id
                status = 'ok'
                error  = $null
            }

            try {
                switch ($task.type) {
                    'toast' {
                        [void][Windows.UI.Notifications.ToastNotificationManager,
                               Windows.UI.Notifications, ContentType = WindowsRuntime]
                        [void][Windows.Data.Xml.Dom.XmlDocument,
                               Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
                        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
                        $xml.LoadXml([string]$task.xml)
                        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
                        [Windows.UI.Notifications.ToastNotificationManager
                        ]::CreateToastNotifier('Vizhi ADT').Show($toast)
                    }
                    'wallpaper' {
                        Add-Type @"
using System.Runtime.InteropServices;
public class VizhiWallpaper {
  [DllImport("user32.dll")]
  public static extern int SystemParametersInfo(int uAction, int uParam, string lpvParam, int fuWinIni);
}
"@
                        $p = 'HKCU:\Control Panel\Desktop'
                        Set-ItemProperty -Path $p -Name WallpaperStyle -Value ([int]$task.fit_code)
                        Set-ItemProperty -Path $p -Name TileWallpaper -Value ([int]$task.tile_code)
                        [VizhiWallpaper]::SystemParametersInfo(20, 0, [string]$task.path, 3) | Out-Null
                    }
                    'screensaver' {
                        $p = 'HKCU:\Control Panel\Desktop'
                        Set-ItemProperty -Path $p -Name ScreenSaveActive -Value 1
                        Set-ItemProperty -Path $p -Name ScreenSaveTimeOut -Value ([int]$task.timeout_s)
                        Set-ItemProperty -Path $p -Name 'SCRNSAVE.EXE' `
                            "$env:SystemRoot\System32\Scrnsave.scr"
                        Set-ItemProperty -Path $p -Name ScreenSaverIsSecure -Value 1
                    }
                    default {
                        throw "Unknown task type: $($task.type)"
                    }
                }
            } catch {
                $result.status = 'error'
                $result.error = $_.Exception.Message
                Write-HelperLog "Task $($task.id) failed: $($_.Exception.Message)"
            }

            $results += $result
        }

        $results | ConvertTo-Json -Depth 4 | Set-Content $ResultFile -Encoding UTF8
        Remove-Item $PendingFile -Force -ErrorAction SilentlyContinue
        Write-HelperLog "Processed $($results.Count) display task(s)"

    } catch {
        Write-HelperLog "Helper loop error: $_"
    }
}
