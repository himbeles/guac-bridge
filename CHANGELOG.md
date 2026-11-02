# Changelog

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
