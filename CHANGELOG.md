# Changelog

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
