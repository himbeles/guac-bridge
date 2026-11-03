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

## Shared Windows UI runtime

The manager is written in Rust with `windows-reactor` and renders native WinUI 3
controls. Install **Microsoft Windows App Runtime 2.4 or newer, x64**, separately
from [Microsoft's downloads page](https://learn.microsoft.com/windows/apps/windows-app-sdk/downloads).
It is shared with other applications. Guac Bridge does not bundle, extract or
automatically install the runtime, and does not require .NET or a web view.

If the runtime is missing, opening the manager displays Reactor's prerequisite
message and a link to Microsoft. The command-line bridges and small native tray
host continue to work. Manager startup errors are saved to `manager.log` in the
per-user data directory. Install the runtime and reopen the manager.

The tray host and the WinUI manager are separate processes. Closing the manager
window keeps the tray and bridges running. Clicking the tray reopens the manager;
only one manager runs at a time. Exit tray and update preparation close the manager
as well, waiting for both executables to exit before replacement.

The responsive manager has a bridge sidebar, SSH/LLM tags and activity labels,
live rate and cumulative usage cards, explicit stop confirmations and expandable
technical details. It follows the Windows light/dark theme.

The list labels each bridge `[SSH]` or `[LLM]` and shows its local endpoint and
remote target. Select a bridge to see:

- Idle/transferring state, uptime, active and total local TCP connections.
- Sampled upload and download rates, refreshed approximately every two seconds.
- Total uploaded and downloaded payload since that bridge started, and time since
  its last payload transfer. Totals remain available when connections close.
- PID, version, executable path and a unique bridge instance ID.

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

For a uv tool installation, use:

```powershell
guac-bridge update
```

This verifies that the command belongs to the uv tool installation, stops bridges
and the tray, then runs `uv tool upgrade guac-bridge`, preserving uv's saved install
settings. On Windows, an updater copied outside the installation runs in a separate
window, waits for the original launcher to exit, then performs the upgrade. That
window reports success or failure; the initial command only reports that it launched
the updater. New bridge starts are blocked while this updater runs. Start bridges
again with your normal command after updating. Connections are not restarted.

A direct `uv tool upgrade guac-bridge` is not intercepted. For direct uv/pip commands
or manual portable replacements, first run `guac-bridge prepare-update` and check
that it succeeds. Do not start bridges between preparation and replacement.

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
