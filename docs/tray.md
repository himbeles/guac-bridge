# Windows bridge manager

A single native Windows tray icon starts automatically when an SSH or LLM bridge
opens its local listener, including bridges started with `--ensure-running`.
It also reappears when an existing background bridge is reused. Bridges enter
the list once their remote helper is ready. The tray, manager and installer use
the frog icon; Windows controls use native visual styles and Segoe UI. Open the bridge
manager by clicking the icon, or run:

```powershell
guac-bridge tray
```

The installer also adds **Guac Bridge Manager** to the Start menu. The portable
and Python installations use the same command.

## Windows UI runtime

The manager is written in Rust with `windows-reactor` and renders native WinUI 3
controls. The **Windows installer bundles a private x64 Windows App Runtime**.
Setup copies its DLLs, resources and licenses into the Guac Bridge directory;
it needs no separate runtime download or system-wide runtime registration.
Updates replace these files together with Guac Bridge, and uninstall removes them.
A missing or damaged private runtime requires repairing or reinstalling Guac Bridge.

The **uv and portable packages** use the shared **Microsoft Windows App Runtime
2.4 or newer, x64**, installed from
[Microsoft's downloads page](https://learn.microsoft.com/windows/apps/windows-app-sdk/downloads).
If it is missing, opening the manager displays Reactor's prerequisite message.
The command-line bridges and small native tray host continue to work.
Manager startup errors are saved to `manager.log` in the per-user data directory.
The UI is native Rust/WinUI; it does not require .NET or a browser runtime.

The tray host and the WinUI manager are separate processes. Closing the manager
window keeps the tray and bridges running. Clicking the tray reopens the manager;
only one manager runs at a time. Exit tray and update preparation close the manager
as well, waiting for both executables to exit before replacement.

The responsive manager has a bridge sidebar, SSH/LLM tags and activity labels,
live rate and cumulative usage cards, connection activity and explicit stop
confirmations. It follows the Windows light/dark theme.

The list labels each bridge `[SSH]` or `[LLM]` and shows its local endpoint and
remote target. Select a bridge to see:

- Idle/transferring state, uptime, active and total local TCP connections.
- Sampled upload and download rates, refreshed approximately every two seconds.
- Total uploaded and downloaded payload since that bridge started, and time since
  its last payload transfer. Totals remain available when connections close.

Upload means bytes read from local clients. Download means bytes successfully
written to local clients. Counters exclude helper deployment, framing, ACKs and
retransmissions. They reset when the bridge process restarts; restarting the tray
preserves the running bridge's totals. These are aggregate counters, not payload
logs or records of earlier bridge runs.

**Stop selected** and **Stop all** ask for confirmation because they disconnect
active SSH sessions and LLM requests. **Open log** opens the managed bridge's
output log, or the configured debug log for a foreground bridge. A foreground
bridge without a debug log has no log to open.

Closing the manager exits its UI process while preserving the tray. **Exit tray (keep bridges running)** removes
the tray icon and exits the manager. A subsequent bridge start launches the tray
again, or use `guac-bridge tray` to reopen it.

## Updating

The Windows installer automatically stops registered bridges and the tray before
replacing files. It uses the new executable extracted into its temporary directory,
so this also works when upgrading an older version that lacks tray support. Active
SSH sessions and LLM requests disconnect. If shutdown fails, the installer aborts
before replacing files. Unresponsive registrations are retained; no PID is killed.

For a uv tool installation, prepare for the external upgrade:

```powershell
guac-bridge prepare-update
uv tool upgrade guac-bridge
```

Run the upgrade only if preparation succeeds. For direct pip commands or manual
portable replacements, also run `guac-bridge prepare-update` first. Do not start
bridges between preparation and replacement. Active sessions disconnect; start
bridges again afterwards. The CLI has no self-update command.

## Tracking and control

Each bridge registers in the existing per-user application data directory, using
`ssh-PORT.json` or `llm-PORT.json`. A registration contains a random instance ID,
PID, executable, version, target, and an authenticated loopback control endpoint.
The authentication token stays out of the UI and status responses. Treat the
registration directory as private, as with the existing background controls.

The manager holds a per-user file lock. A second manual launch raises the existing
window; automatic launches reuse the existing tray host. Explorer restarts are
handled by recreating the notification icon.

A worker polls the control sockets, keeping socket waits off the UI thread.
`inspect` returns usage; existing `status` and `stop` commands still return `OK`.
Older bridges remain manageable but cannot supply usage details until restarted
with the new version. Unresponsive registrations are shown explicitly, retained,
and never used for PID-based termination. Normal bridge shutdown removes only its
own registration, verified using its token.

The WinUI manager requires Windows 11 or Windows Server 2025 or newer because Reactor uses the OS package dependency APIs. Command-line bridges and the native tray host remain available on earlier Windows versions.
