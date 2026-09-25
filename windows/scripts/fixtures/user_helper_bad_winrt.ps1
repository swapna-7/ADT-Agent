# Intentionally broken — reproduces 2.1.12 multi-line WinRT loader (rejected by PS 5.1).
[void][Windows.UI.Notifications.ToastNotificationManager,
    Windows.UI.Notifications, ContentType = WindowsRuntime]
Write-Host 'should not parse'
