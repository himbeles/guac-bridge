# Changelog

## 5.0.0-beta.1

- Add an experimental FortiGate SSL-VPN web-SSH gateway for both SSH and LLM bridges, with portal password login, scoped session cookies, hterm custom SSH sessions and the existing Windows printable helper carrier. Existing commands continue to default to Guacamole.
- Add server-host, server-port and server-user options, separately stored server credentials and private background credential handoff. Keep the local listener and helper service destination independent of the gateway SSH server.
- Label gateway types in CLI connection lists and the Windows manager, and include FortiGate server identity in managed bridge reuse checks.
- Add simulated FortiGate tests using the real helper for binary transfer, concurrency, half-closes, streamed HTTP and managed lifecycle behavior. Real FortiGate deployment validation, MFA/SSO, bookmarks and Linux server support remain outside this first beta.
- Publish beta builds as GitHub prereleases and preserve prerelease text in Windows installer version metadata.

## 4.0.2

- Restrict Windows background bridge, tray and manager child processes to explicitly supplied input and output handles. Prevent inherited SSH config file handles from being retained after a Match exec hook returns.
- Preserve password-pipe handoff, log redirection and detached process lifetimes. Add Windows regression tests for inherited file release, live bridge config replacement and argument quoting.

## 4.0.1

- Replace fs2 with standard-library file locking for bridge startup, update exclusion, tray and manager coordination. Preserve shared startup guards and exclusive update guards.
- Restrict client-only and platform-specific dependencies, enable only the required Tokio and futures-util features, and remove unused transitive dependencies.
- Declare Rust 1.95 as the minimum supported version, matching the Windows UI dependencies, and add coverage for startup locks blocking updates until released.

## 4.0.0

- Simplify connection startup to `guac-bridge ssh URL --user USER` or `guac-bridge llm URL --user USER`. The URL is positional and required for startup; `--port` selects the local listener and `--remote-port` selects the server service. Background startup is the default; `--foreground` keeps an attached session. Remove `--show-console` and the redundant `--ensure-running` option.
- Consolidate builds, helper staging, checks, portable packaging, installer construction and release preparation behind Rust's `cargo xtask`. Remove the competing Make, Python and PowerShell build scripts.
- Make Cargo the application version source, including Python/uv package and installer metadata. Keep native, portable and Python/uv distribution paths.
- Separate CLI options, managed-process startup and Guacamole session setup from forwarding, and expose `guac_bridge::run(argv)` as the application entry point.
- Use the current bridge registration model and authenticated usage inspection. Remove compatibility defaults, status-only inspection fallback, compatibility UI messaging and obsolete executable cleanup. Retain unresponsive current registrations without PID-based termination.
- Remove obsolete investigation scripts and archived benchmark reports. Keep focused build and architecture guidance, active protocol specifications and the current benchmark.

## 3.1.0

- Start or reuse background bridges by default for `ssh` and `llm`. Keep `--ensure-running` as an explicit synonym and add `--foreground` for attached sessions. Update examples and lifecycle documentation.
- Add `guac-bridge list`, showing bridge status, Guacamole username and the local → jump host → remote service route. Retain and label unresponsive registrations.
- Show Guacamole URLs and users in the Windows manager while keeping local endpoints and remote service URLs visible. Use subtle frog-green accents and preserve native light/dark controls.
- Show the bundled helper version alongside upload progress and remove remote-confirmation wording from the progress row. Keep remote acknowledgements and checksum verification unchanged.
- Omit URL credentials and authentication queries from displayed and stored jump-host metadata. Older registrations remain readable; restart those bridges to populate their jump-host URLs.

## 3.0.0

- Use `guac-bridge` as the single client CLI, with `ssh` and `llm` subcommands. Remove `guac-ssh-client` and `guac-llm-client` from Cargo, Python wheels, portable archives and Windows setup. Installer upgrades remove the obsolete executables.
- Keep Python/PyPI and uv installations, including `python -m guac_bridge`. Remove the CLI's self-update command and its temporary updater process. Use `guac-bridge prepare-update`, followed by your external package manager or installer.
- Remove the expandable per-bridge technical-details section from the manager. Keep the bridge list, activity, connection counts, traffic cards and stop/log controls.
- Keep the private Windows UI runtime in the installer and test managed startup, package entry points and installer upgrade cleanup using the unified CLI.

## 2.2.1

- Bundle a private x64 Windows App Runtime with the Windows installer using Microsoft's `windows-reactor-setup`. The manager starts without a separately installed shared runtime; setup works offline and uninstall removes the private runtime.
- Retain the existing framework-dependent uv and portable packages. Installer builds stage the private runtime separately, retain frog icons and version metadata, and include Microsoft's redistribution licenses.
- Test the installed manager with the shared runtime removed, verify installer updates release the manager and tray, and report the actual compressed installer size against v2.2.0.

## 2.2.0

- Replace the Windows manager with native WinUI 3 controls using Microsoft's Rust `windows-reactor` library. Organize the resizable view into a bridge sidebar, activity summary, live rate and cumulative traffic cards, and expandable technical details. Follow Windows light/dark appearance and retain the frog icon.
- Require Windows 11 / Windows Server 2025 or newer for the manager; delay-load its OS dependency to retain command-line and tray compatibility on earlier Windows systems.
- Use the separately installed, shared Microsoft Windows App Runtime 2.4 or newer (x64). No UI runtime is bundled or automatically installed; installers disclose the prerequisite and CLI bridges and the tray remain usable without it.
- Keep the tray host independent of the manager window. Closing the window preserves the tray and running bridges; reopening restores their current usage. Update preparation and Exit tray close the manager and wait for executable release.
- Test WinUI discovery, traffic cards, stop confirmation, resizing, manager reopening and update shutdown with native Windows UI Automation.

## 2.1.2

- Keep the tray and bridges running when the manager window is closed with X or Alt+F4. Closing the window hides it; Exit tray remains the explicit quit action.
- Fix bridge-list and detail-pane layout updates during native window resizing, maximize and restore.
- Add native Windows regression coverage for system close and maximize/restore. Keep the existing UI framework and startup behavior.

## 2.1.1

- Start the Windows tray as soon as the first bridge opens its local listener, before remote initialization. Restore it when reusing an existing background bridge.
- Add a multi-resolution frog icon to the tray, manager, client executables and installer.
- Enable native Windows visual styles and Explorer control theming, use Segoe UI, and remove sunken borders.
- Embed client executable version metadata and explicit installer file/product versions; use the modern installer wizard.
- Test tray startup during pending login and verify installer version metadata.

## 2.1.0

- Add a native Windows tray manager for foreground and background bridges, with SSH/LLM tags, authenticated stop controls, executable/version details and unresponsive registration visibility.
- Show live upload/download rates, cumulative payload totals since bridge startup, active/total connections and last activity. Keep polling off the UI thread and outside the transport path.
- Automatically stop bridges and the tray before Windows installer updates; wait for process exit before replacing executables. Add a Start menu shortcut to the manager.
- Add `guac-bridge update` for uv tool installations, using a separate Windows updater to release installed executables before upgrading. Direct `uv tool upgrade` is not intercepted; `prepare-update` supports external updates. Active sessions disconnect and bridges must be restarted afterwards.
- Add authenticated control, usage, update exclusion, native Windows UI and installer upgrade tests.

## 2.0.0

- Use reliable input for all SSH and LLM bridges, including managed bridges: bounded checksummed records, resynchronization, selective acknowledgements, duplicate suppression and retries. This is the only input transport, with no selection flags or raw fallback. The reliable record window is fixed at 64; existing gateway and TCP flow-control settings remain available.
- Report record recovery counters and actionable window/timeout suggestions on the client, with rate-limited warnings and bounded delivery errors.
- Display one updating helper-upload bar in interactive terminals and SSH hooks, with sparse progress in redirected logs. Keep first-install remote block acknowledgements and final checksums.
- Add damaged-stream and progress integration tests, codec bounds tests, and reproducible RTT/CPU/loss benchmarks. Document the first-install bootstrap limitation and the scope of input protection.

## 1.1.4

- Use conservative defaults for both SSH and LLM bridges: 4 TCP frame credits, 65536-byte frames, and an 8-blob Guacamole input window.
- Add independent `--input-window` tuning (1-128, default 8). Changing `--window` no longer expands gateway input buffering. Managed bridges forward and validate the setting; client debug logs record the effective transport settings.
- Update tuning documentation and the benchmark tool. Keep binary framing, TCP credit semantics, the Linux helper-upload protocol, and checksum verification unchanged. Reliable transport and retransmission are a separate future change.

## 1.1.3

- Relay fatal remote-helper errors through a separate printable terminal envelope, including decoder state and recent lifecycle events, so malformed console input no longer appears only as a closed output pipe.
- Fix a race that could discard a remote decoder error when the input reader ended. Target socket and channel queue errors now retain more context. The cause of recurring SSH disconnects remains unconfirmed; this release improves diagnosis.
- Add opt-in `--debug-log` and `--server-debug-log`, including managed bridge support. Metadata logs use a bounded nonblocking queue, a separate writer thread, rotation, and no traffic payload capture. Data/Credit frames only update counters when logging is enabled. Healthy binary framing and flow control are unchanged.
- Document reproduction and log collection, and add corruption-injection and payload-exclusion tests. Local Linux PTY throughput checks showed no measurable regression against v1.1.2; real corporate-gateway performance remains deployment-dependent.

## 1.1.2

- Increase Linux helper-upload batches from 3 KiB to 24 KiB while retaining 1 KiB Guacamole input blobs, remote acknowledgements and final checksum verification. Live SSH/HTTP framing and flow control are unchanged.
- Add `--upload-batch-size` for tuning or falling back to 3072 bytes; forward it to managed background bridges. Windows helper uploads are unchanged.
- Add a reproducible local batch-size benchmark with integrity and dropped-input checks. With a simulated 20 ms remote-acknowledgement delay, median fresh upload time fell from 12.813 to 1.682 seconds. Actual jump-host performance and buffering limits require deployment verification.

## 1.1.1

- Prevent Linux helper-upload bursts: wait for remote acknowledgement of each small upload block and show remote-confirmed progress. The active TCP transport and Windows upload protocol are unchanged.
- Relay upload decoder stderr and preserve underlying login, WebSocket-close and target-connection errors.
- Keep healthy tunnels running after transient HTTP session-refresh failures and perform refresh requests without blocking connection acceptance. Authentication rejection still stops the bridge.
- Add Makefile and PowerShell entry points for local helper/client builds, checks, portable archives and Windows installer packaging.
- Correct the Cargo lockfile package version while retaining dependency versions.

## 1.1.0

- Add Windows server helper

## 1.0.1

- Report helper upload progress and surface remote bootstrap errors immediately.
- Prepare the terminal before sending the bootstrap script to prevent the long command being echoed.
- Show live startup progress and errors in the calling console for SSH Match hooks.
- Remove runtime-installation wording from CLI messages.
- Preserve final SSH data when both sides half-close a connection.

## 1.0.0

- Standalone Rust clients and a shared SSH/HTTP forwarding engine.
- Automatic deployment of a bundled static Linux helper; no server Python or uv setup.
- Windows installer with optional user PATH setup, portable executables, and uv installation of the same native tools.
- Native credential storage and managed background bridges with status and stop commands.

## 0.10.1

- Use WebSocket exclusively for Guacamole connections; remove HTTP tunnel fallback and transport selection.

## 0.10.0

- Use binary framing with raw bodies for both LLM and SSH bridges, reducing encoding work and traffic.
- Increase SSH frames and input windows for faster transfers while preserving token streaming and existing startup/update flows.
- Require protocol v5 helpers; the existing update prompt handles older installations.

## 0.9.0

- Reduce bridge traffic and encoding work by sending ASCII JSON lines directly through terminal pipes.
- Use protocol v4 with strict framing checks; retain independent bootstrap updates for older helpers.

## 0.8.3

- Relay server update approval from SSH hooks to the calling console.
- Print startup failures once while keeping full details in the log.

## 0.8.2

- Allow `--ensure-running --save-password` in SSH config hooks, prompting in the calling console on first login.
- Verify and save credentials before background startup; reuse them on later connections.

## 0.8.1

- Check helper versions before startup and offer an interactive update on mismatch (`[Y/n]`).
- Show background SSH startup progress and errors, including Windows hook output.
- Preserve the helper Python during server updates; add `--server-python` for explicit selection.

## 0.8.0

- Add opt-in server updates before connecting, using uv tool and the server’s configured package index.
- Verify the helper version after installation and provide manual update instructions on failure.

## 0.7.0

- Add optional window and SSH frame-size tuning, retaining the fast 32-credit/16 KiB defaults.
- Explain throughput and buffering tradeoffs in CLI help and confirm settings with the helper at startup.

## 0.6.1

- Remove older-server compatibility and use the faster transport limits directly.
- Reject outdated helper protocols with clear server update instructions.

## 0.6.0

- Speed up bulk SSH transfers and LLM responses with larger flow-control windows and SSH frames.
- Negotiate faster settings automatically, retaining compatible limits for older clients and helpers.
- Document connection tests, benchmark results, and reproduction steps.

## 0.5.2

- Warn on server version mismatches by default; add `--require-server-version` for strict checks.

## 0.5.1

- Add `--ignore-server-version` for temporary upgrade connections, including older helpers.

## 0.5.0

- Faster SSH transfers and LLM streaming with bounded ACK pipelining.
- Require matching client/server versions, with clear installation instructions on mismatch.
- Improve cancellation, shutdown, and local port-conflict diagnostics.
- Add benchmarking tools and documentation.
