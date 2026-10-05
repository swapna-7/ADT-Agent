from command_policy import command_age_expired, is_blocked_command, is_chocolatey_install_command

CHOCOLATEY_INSTALL_COMMAND = (
    "Set-ExecutionPolicy Bypass -Scope Process -Force; "
    "iex ((New-Object Net.WebClient).DownloadString("
    "'https://community.chocolatey.org/install.ps1'))"
)


def test_blocked_commands():
    assert is_blocked_command("rm -rf /") == "rm-rf"
    assert is_blocked_command("shutdown /s") == "shutdown"
    assert is_blocked_command("winget install foo") == "winget"
    assert is_blocked_command("iex Get-Process") == "iex"


def test_chocolatey_install_allowed():
    assert is_chocolatey_install_command(CHOCOLATEY_INSTALL_COMMAND)
    assert is_blocked_command(CHOCOLATEY_INSTALL_COMMAND) is None


def test_allowed_diagnostics():
    assert is_blocked_command("whoami") is None
    assert is_blocked_command("hostname") is None


def test_command_age_expired():
    assert command_age_expired("2000-01-01T00:00:00Z") is True
    assert command_age_expired(None) is False
