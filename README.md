# ADT Agent

Windows, Linux, and macOS endpoint agents. Shared code lives in [`common/`](common/). Current version is [`VERSION`](VERSION) / [`common/version.py`](common/version.py) (2.1.0).

## Layout

```
adt-agent/
  VERSION                          single semver (must match common/version.py)
  generate_api_config.py           bake VIZHI_API_BASE → api_config.py at build time
  install-windows.ps1              Windows install (task + stdin enrollment)
  uninstall-windows.ps1            remove binary/task; preserve ProgramData
  build.ps1 / build.sh             convenience wrappers → platform build scripts
  .github/workflows/release.yml    tag vX.Y.Z → pytest + GitHub Release (all three OS)

  common/                          shared Python used by every platform
    config_io.py                   atomic config.json read/write
    api_base.py                    resolve API_BASE (config > env > baked)
    first_run.py                   stdin enrollment on first run
    scheduler.py                   persisted last_runs task intervals
    version.py                     AGENT_VERSION
    enrollment.py                  org code → ENDPOINT_ID + DEVICE_TOKEN
    report.py                      heartbeat, inventory, commands, updates
    self_update.py                 Vizhi /latest + /download, hash, replace
    command_poller.py              remote console commands
    patch_runner.py                assigned patch jobs
    updates.py                     classify OS updates / apply
    normalize.py                   software identity + version compare
    software_quality.py            inventory quality scoring
    requirements.txt               psutil, requests, packaging, pyinstaller

  windows/
    agent.py                       entrypoint (scheduled task, WUA)
    inventory_windows.py           registry / MSI / AppX / KB / services
    build.ps1                      PyInstaller → windows/dist/adt-agent.exe
    install_task.ps1               wrapper → ../install-windows.ps1
    uninstall_task.ps1             wrapper → ../uninstall-windows.ps1
    scripts/install-fresh.ps1        wipe + install-windows.ps1

  linux/
    agent.py                       systemd, deb/rpm/snap/flatpak collectors
    build.sh                       PyInstaller → linux/dist/adt-agent
    install.sh / uninstall.sh        systemd install; preserve /var/lib/vizhi-agent

  mac/
    agent.py                       LaunchAgent, zsh remote commands
    build.sh                       PyInstaller → mac/dist/adt-agent
    install.sh / uninstall.sh        LaunchAgent install; preserve Application Support data

  scripts/                         publish-to-tool, SQL helpers
  tests/                           pytest (conftest.py puts common/ on sys.path)
```

Run tests from the repo root:

```bash
python -m pytest tests/ -q
```

## Building

Release binaries **do not bundle `.env` or credentials**. Set `VIZHI_API_BASE` to your Vizhi server URL before building; build scripts generate `api_config.py` and embed it in the binary.

```powershell
# Windows
$env:VIZHI_API_BASE = "https://vizhi.example.com"
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

```bash
# Linux / macOS
export VIZHI_API_BASE=https://vizhi.example.com
./build.sh   # or cd linux && ./build.sh / cd mac && ./build.sh
```

Build scripts **fail** if `windows/.env`, `linux/.env`, `mac/.env`, or repo-root `.env` exists.

CI requires GitHub secret **`VIZHI_API_BASE`** (same URL used for production agents).

Windows and Linux agents do **not** need a runtime `.env`. Metrics and inventory go to Vizhi with the device token.

## Installing

The frozen binary **self-installs**. Download it from the Vizhi portal (or copy `windows/dist/adt-agent.exe` / `linux/dist/adt-agent`) and run it as administrator / root. The Vizhi URL is baked in at build time. After enrollment the agent reports through `/api/agent/telemetry` — **no `.env` file**.

### From the Vizhi portal

1. Open **Install agents** and copy the enrollment code (`VZ-XXXX-XXXX-XXXX`).
2. Download `vizhi-agent.exe` or `vizhi-agent-linux`.
3. Windows: right-click → **Run as administrator**, paste the code when prompted (or `vizhi-agent.exe --code VZ-…`).
4. Linux: `sudo ./vizhi-agent-linux --code VZ-…`

The binary copies itself into place, registers the boot task / systemd unit, enrolls, and starts.

### Windows (from this repo, after build)

```powershell
# Administrator PowerShell:
.\windows\dist\adt-agent.exe --code VZ-XXXX-XXXX-XXXX
# or:
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

Or wipe and reinstall: `powershell -ExecutionPolicy Bypass -File .\windows\scripts\install-fresh.ps1`

| Path | Purpose |
|------|---------|
| `C:\Program Files\ADT Agent\adt-agent.exe` | Installed binary |
| `C:\ProgramData\ADT Agent\config.json` | Device identity (`ENDPOINT_ID`, `DEVICE_TOKEN`, `last_runs`) |
| `C:\ProgramData\ADT Agent\agent.log` | Runtime log |

**Uninstall:** `powershell -ExecutionPolicy Bypass -File .\uninstall-windows.ps1` (preserves `C:\ProgramData\ADT Agent\`)

### Linux

```bash
sudo ./linux/install.sh
```

Root install: `/opt/vizhi-agent` binary, `/var/lib/vizhi-agent` data, `vizhi-agent.service`. Non-root fallback data dir: `~/.config/vizhi-agent/`.

**Uninstall:** `sudo ./linux/uninstall.sh` (preserves `/var/lib/vizhi-agent`)

### macOS

```bash
./mac/install.sh
```

Installs to `~/Library/Application Support/ADT Agent/` with LaunchAgent `com.adt.agent`.

**Uninstall:** `./mac/uninstall.sh` (preserves Application Support data)

## Runtime behavior

- Main loop sleeps **20s** (`command_poll` interval); metrics, inventory, and update scans use persisted `last_runs` in `config.json`.
- First run without `DEVICE_TOKEN`: `--code` enrolls without a prompt; otherwise prompts on stdin when TTY; exit **2** without TTY; exit **1** on enrollment failure (config not written).
- `config.json` writes are atomic (tmp + `os.replace` + fsync).

## Releases and auto-update

1. Bump **both** [`VERSION`](VERSION) and `AGENT_VERSION` in [`common/version.py`](common/version.py).
2. Commit, tag, push: `git tag v2.1.1 && git push origin v2.1.1`
3. GitHub Actions runs pytest, builds all three platforms with `VIZHI_API_BASE`, uploads artifacts and `latest.json`.

Installed agents check Vizhi every six hours for a newer release, verify SHA-256, replace the binary, and exit so the scheduler restarts them. Freeze with `"AGENT_AUTO_UPDATE": "0"` in `config.json`.

| Platform | `config.json` |
|----------|----------------|
| Windows | `C:\ProgramData\ADT Agent\config.json` |
| Linux (root) | `/var/lib/vizhi-agent/config.json` |
| Linux (user) | `~/.config/vizhi-agent/config.json` |
| macOS | `~/Library/Application Support/ADT Agent/config.json` |

The Vizhi app needs `AGENT_GITHUB_REPO` and `AGENT_GITHUB_TOKEN` for private release downloads.

## Web tool downloads

After building, publish into the Vizhi dashboard: `scripts/publish-to-tool.ps1` or `publish-to-tool.sh`.

| File | Platform |
|------|----------|
| `vizhi-agent.exe` | Windows |
| `vizhi-agent-linux` | Linux |

---

## Rex Cyber Solutions — contact

| | |
| --- | --- |
| **Website** | [https://www.rexcybersolutions.com/](https://www.rexcybersolutions.com/) |
| **Email** | [contact@rexcybersolutions.com](mailto:contact@rexcybersolutions.com) |
| **Careers** | [career@rexcybersolutions.com](mailto:career@rexcybersolutions.com) |
| **LinkedIn** | [https://www.linkedin.com/company/rexcybersolutions/](https://www.linkedin.com/company/rexcybersolutions/) |
| **Facebook** | [https://www.facebook.com/rexcybersolutions](https://www.facebook.com/rexcybersolutions) |
