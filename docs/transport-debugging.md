# Diagnosing SSH disconnects

These diagnostics are available in v1.1.3 and later. Stop the old managed bridge first so it does not retain the listener.

Run the bridge in a PowerShell terminal while reproducing the problem:

```powershell
guac-bridge ssh --port 2223 --stop
guac-bridge ssh --url 'YOUR_GUACAMOLE_URL' --username 'YOUR_USERNAME' --port 2223 `
  --debug-log "$env:TEMP\guac-ssh-debug.log" `
  --server-debug-log '/home/YOUR_REMOTE_USER/.cache/guac-bridge/helpers/ssh-debug.log'
```

Replace the remote path with an absolute path in a private directory that already
exists. For a Windows remote helper, add `--server-os windows` and use a Windows
absolute path there. The local log is on the Windows client; the server log remains
on the remote machine. Use separate paths for separate bridge processes. Log files
are overwritten when a bridge starts. One `.1` backup is kept on rotation; each file
is limited to approximately 2 MiB. Unix log files are mode 600; on Windows keep logs
in your private user directory. A custom `--server-command` helper must support the
new `--debug-log` option.

Both options are optional and also work with `--ensure-running`. Adding them changes
the managed bridge configuration; stop the existing bridge before restarting it.

## What to collect

Keep the foreground error and both logs from the same reproduction. Note whether
all sessions on the bridge close together. Include the bridge version and the time
relative to startup. The log timestamps are elapsed seconds per process; channel IDs
correlate the client and helper. Logs do not contain URLs, credentials, or traffic
payloads. Error messages can include operating-system details and file paths.

A helper fatal error now arrives through short printable terminal envelopes,
independently of the binary frame decoder. It includes the error chain and the
last 32 lifecycle events. For example, an invalid input escape reports
`Remote console decode failed`, wire bytes received, buffered bytes, and the
pending escape state. Queue failures include the affected channel and queue
capacity. Target socket errors identify connect/read/write failures. A fatal helper
error closes all channels; an ordinary target socket error applies to its channel.

If the helper is killed, the console disappears, or the gateway breaks, a fatal
report may not reach the client. The separate server file can still help. If the
output pipe ends without a fatal report, the client allows a short grace period
for a report before reporting that the helper exited. This does not restart it or
preserve existing SSH sessions.

## Performance and limits

Normal frame encoding, frame sizes, credits, and flow control are unchanged.
There are no additional acknowledgements or diagnostic packets during healthy
forwarding. Data and Credit frames never allocate log messages, acquire logging
locks, or write files. When logging is disabled their diagnostic hook only checks
a flag. When enabled it increments relaxed atomic aggregate counters. Lifecycle
records and periodic heartbeat summaries are offered to a bounded nonblocking
queue. A separate thread writes/rotates files. When that queue fills, records are
dropped and counted instead of delaying forwarding. Disk errors disable file
logging. Pending log records are drained only after forwarding stops.

Client debug logs record `window`, `frame_size`, `input_window`, and `input_chunk`
on startup from v1.1.4 onward. Use those values to verify which
configuration a foreground or managed bridge actually started with.

Counters show aggregate sent/received frames and payload bytes, and dropped log
records. Compare their progress across heartbeat summaries to investigate stalls.
The always-on in-memory history records only lifecycle/control events, not every
Data or Credit frame. Logging is not a packet capture: valid-looking payload
corruption cannot be detected without changing the protocol to add integrity
checks. A blocked OS stdout write can also prevent a fatal report from being sent.
EOF/decoder failures report partial frame state; silence alone is not proof of
corruption.

Run local validation without GitHub Actions:

```sh
make check
make integration
```

The integration tests inject invalid bytes into the actual helper's Bash PTY with
two channels open and assert that the decoder error reaches the client. They also
check payload exclusion, private Unix log permissions, and exact binary transfer
with both logs enabled.

## Local throughput check

Five trials per case transferred 32 MiB in each direction through the Guacamole
simulator and a real Bash PTY. Every transfer was checked for exact equality.
The baseline was built from the unmodified v1.1.2 release source. Order alternated
between trials; startup/upload time was excluded.

| Build | Median seconds | Combined transfer MiB/s |
| --- | ---: | ---: |
| baseline | 2.814 | 22.74 |
| debug_off | 2.683 | 23.85 |
| debug_on | 2.654 | 24.12 |

These results show no measurable regression in this local test. The differences
are smaller than trial variation; they are not proof of zero overhead on every
Windows host or corporate gateway. Enabled logging performs a small amount of
counter work even though disk I/O is isolated from forwarding.

Raw times and binary hashes are in [diagnostics-benchmark.json](diagnostics-benchmark.json).
To repeat with independently built baseline and modified binaries:

```sh
python scripts/benchmark-diagnostics.py \
  --baseline /path/to/v1.1.2/target/release/guac-bridge \
  --modified target/release/guac-bridge --trials 5 --mib 32
```

The follow-up [SSH disconnect investigation](ssh-disconnect-root-cause.md) contains
a reproducible libssh2 write-loss experiment and deployment confirmation steps.
