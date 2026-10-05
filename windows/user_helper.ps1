# Vizhi ADT user-session display helper.
# Runs as the logged-in user (ADTAgentHelper scheduled task at logon).
# Reads pending_display.json written by the SYSTEM agent and applies display tasks.

$ErrorActionPreference = 'Continue'

$DataDir = Join-Path $env:ProgramData 'ADT Agent'
$PendingFile = Join-Path $DataDir 'pending_display.json'
$ResultFile = Join-Path $DataDir 'display_results.json'
$LogFile = Join-Path $DataDir 'user_helper.log'
$ShownFile = Join-Path $DataDir 'shown_display_ids.json'
$script:ShownIds = @{}
$script:ConsoleFreed = $false

function Hide-HelperWindow {
    try {
        if (-not ('VizhiNative.VizhiWin' -as [type])) {
            Add-Type -Name VizhiWin -Namespace VizhiNative -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("kernel32.dll")] public static extern bool FreeConsole();
[DllImport("kernel32.dll")] public static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
        }
        $consoleHwnd = [VizhiNative.VizhiWin]::GetConsoleWindow()
        $mainHwnd = [IntPtr]::Zero
        try {
            $proc = Get-Process -Id $PID -ErrorAction Stop
            $mainHwnd = $proc.MainWindowHandle
        } catch {}
        foreach ($hwnd in @($consoleHwnd, $mainHwnd)) {
            if ($hwnd -and $hwnd -ne [IntPtr]::Zero) {
                [void][VizhiNative.VizhiWin]::ShowWindow($hwnd, 0)
            }
        }
        $freed = $false
        if (-not $script:ConsoleFreed) {
            [void][VizhiNative.VizhiWin]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
            $freed = [VizhiNative.VizhiWin]::FreeConsole()
            $script:ConsoleFreed = $true
        }
        return @{
            consoleHwnd = [int64]$consoleHwnd
            mainHwnd = [int64]$mainHwnd
            freed = $freed
        }
    } catch {
        return @{ consoleHwnd = 0; mainHwnd = 0; freed = $false; error = "$_" }
    }
}

function Write-SharedText {
    param([string]$Path, [string]$Text)
    $dir = Split-Path $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $fs = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::ReadWrite
    )
    try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
}

function Add-SharedLine {
    param([string]$Path, [string]$Line)
    $dir = Split-Path $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Line + [Environment]::NewLine)
    $fs = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::ReadWrite
    )
    try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
}

function Read-SharedText {
    param([string]$Path)
    $fs = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite
    )
    try {
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8, $true)
        try { return $sr.ReadToEnd() } finally { $sr.Dispose() }
    } finally { $fs.Dispose() }
}

function Write-HelperLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    try {
        Add-SharedLine -Path $LogFile -Line $line
    } catch {
        $fallback = Join-Path $env:TEMP 'adt-agent-user_helper.log'
        try { Add-SharedLine -Path $fallback -Line $line } catch {}
    }
}

function Load-ShownIds {
    try {
        if (-not (Test-Path $ShownFile)) { return }
        $prev = (Read-SharedText -Path $ShownFile) | ConvertFrom-Json
        foreach ($id in @($prev)) {
            if ($id) { $script:ShownIds[[string]$id] = $true }
        }
    } catch {}
}

function Save-ShownIds {
    try {
        $ids = @($script:ShownIds.Keys)
        if ($ids.Count -gt 200) {
            $ids = $ids[($ids.Count - 200)..($ids.Count - 1)]
        }
        Write-SharedText -Path $ShownFile -Text ($ids | ConvertTo-Json)
    } catch {}
}

function Clear-PendingQueue {
    $deleted = $false
    try {
        Remove-Item $PendingFile -Force -ErrorAction Stop
        $deleted = $true
    } catch {}
    # Do not overwrite SYSTEM-owned pending_display.json — that can hang the helper.
    @{ deleted = $deleted; overwritten = $false }
}

function Mark-StalePendingIds {
    $n = 0
    if (-not (Test-Path $PendingFile)) { return $n }
    try {
        $raw = Read-SharedText -Path $PendingFile
        if (-not $raw -or -not $raw.Trim() -or $raw.Trim() -eq '[]') { return $n }
        $tasks = $raw | ConvertFrom-Json
        if ($null -eq $tasks) { return $n }
        if ($tasks -isnot [System.Array]) { $tasks = @($tasks) }
        foreach ($task in $tasks) {
            $tid = [string]$task.id
            if ($tid -and -not $script:ShownIds.ContainsKey($tid)) {
                $script:ShownIds[$tid] = $true
                $n++
            }
        }
        Save-ShownIds
    } catch {}
    return $n
}

function Ensure-ToastTypes {
    if ('Windows.UI.Notifications.ToastNotification' -as [type]) { return }
    # WinRT assembly refs must be a single line each (multi-line breaks the parser).
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
}

Hide-HelperWindow | Out-Null
Load-ShownIds
Write-HelperLog 'ADTAgentHelper started (pid=' + $PID + ')'
$staleMarked = Mark-StalePendingIds
Write-HelperLog "Marked $staleMarked stale pending id(s) without toasting"

while ($true) {
    Hide-HelperWindow | Out-Null
    if (Test-Path $PendingFile) {
        try {
            $raw = Read-SharedText -Path $PendingFile
            if ($raw -and $raw.Trim() -and $raw.Trim() -ne '[]') {
                $tasks = $raw | ConvertFrom-Json
                if ($null -ne $tasks) {
                    if ($tasks -isnot [System.Array]) { $tasks = @($tasks) }

                    $results = @()
                    $shownCount = 0
                    $skippedCount = 0

                    foreach ($task in $tasks) {
                        $tid = [string]$task.id
                        if ($tid -and $script:ShownIds.ContainsKey($tid)) {
                            $results += @{
                                id     = $task.id
                                status = 'ok'
                                error  = 'skipped_duplicate'
                            }
                            $skippedCount++
                            continue
                        }

                        $result = @{
                            id     = $task.id
                            status = 'ok'
                            error  = $null
                        }

                        try {
                            $doWork = $true
                            if ($task.type -eq 'toast' -and $shownCount -ge 1) {
                                $doWork = $false
                                $result.error = 'deferred_rate_limit'
                            }
                            if ($doWork) {
                                switch ($task.type) {
                                    'toast' {
                                        Ensure-ToastTypes
                                        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
                                        $xml.LoadXml([string]$task.xml)
                                        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
                                        $aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
                                        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
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
                                    'lockscreen' {
                                        $img = [string]$task.path
                                        if (-not (Test-Path -LiteralPath $img)) { throw "lockscreen image missing" }
                                        $cdm = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\ContentDeliveryManager'
                                        New-Item -Path $cdm -Force | Out-Null
                                        Set-ItemProperty -Path $cdm -Name RotatingLockScreenEnabled -Value 0
                                        Set-ItemProperty -Path $cdm -Name RotatingLockScreenOverlayEnabled -Value 0
                                        Add-Type -AssemblyName System.Runtime.WindowsRuntime
                                        $null = [Windows.System.UserProfile.LockScreen,Windows.System.UserProfile,ContentType=WindowsRuntime]
                                        $null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
                                        $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
                                        $asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and -not $_.IsGenericMethod })[0]
                                        $fileOp = [Windows.Storage.StorageFile]::GetFileFromPathAsync($img)
                                        $fileTask = $asTaskGeneric.MakeGenericMethod([Windows.Storage.StorageFile]).Invoke($null, @($fileOp))
                                        $fileTask.Wait()
                                        if ($fileTask.IsFaulted) { throw $fileTask.Exception.GetBaseException().Message }
                                        $setOp = [Windows.System.UserProfile.LockScreen]::SetImageFileAsync($fileTask.Result)
                                        $setTask = $asTaskAction.Invoke($null, @($setOp))
                                        $setTask.Wait()
                                        if ($setTask.IsFaulted) { throw $setTask.Exception.GetBaseException().Message }
                                    }
                                    default {
                                        throw "Unknown task type: $($task.type)"
                                    }
                                }
                            }
                            if ($doWork) {
                                if ($tid) { $script:ShownIds[$tid] = $true }
                                $shownCount++
                            } else {
                                $skippedCount++
                            }
                        } catch {
                            $result.status = 'error'
                            $result.error = $_.Exception.Message
                            Write-HelperLog "Task $($task.id) failed: $($_.Exception.Message)"
                        }

                        $results += $result
                    }

                    Write-SharedText -Path $ResultFile -Text ($results | ConvertTo-Json -Depth 4)
                    Save-ShownIds
                    [void](Clear-PendingQueue)
                    Write-HelperLog "Processed $($results.Count) display task(s) shown=$shownCount skipped=$skippedCount"
                }
            }
        } catch {
            Write-HelperLog "Helper loop error: $_"
        }
    }

    Start-Sleep -Seconds 10
}
