# Diagnosing bridge disconnects

Stop the managed bridge first so it does not retain the listener.

Run the bridge in a PowerShell terminal while reproducing the problem:

```powershell
guac-bridge stop --port 2223
guac-bridge ssh --foreground 'YOUR_GUACAMOLE_URL' --user 'YOUR_USERNAME' --port 2223 `
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

Both options are optional and also work with the default background startup. Adding them changes
the managed bridge configuration; stop the existing bridge before restarting it.

No client or server log file is created by default. For a Windows helper, use:

```powershell
--debug-log client-debug.log --server-debug-log 'C:\Users\REMOTE_USER\server-debug.log'
```

After reproducing, read the client log locally and the server log on the remote
machine with `Get-Content PATH -Tail 80`.

## What to collect

Keep the foreground error and both logs from the same reproduction. Note whether
all sessions on the bridge close together. Include the bridge version and the time
relative to startup. The log timestamps are elapsed seconds per process; channel IDs
correlate the client and helper. Logs do not record passwords or traffic payloads. Error messages can include operating-system details and file paths.

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

Logging does not change frame encoding, frame sizes, credits or flow control.
It adds no diagnostic packets. Data and Credit hooks update aggregate counters
without logging their contents. With debug logging enabled, reliable records and
InputAck/OutputAck messages include sequence numbers, selective bitmaps, rejection
counts, lengths and retry state. They do not include application payloads.
A bounded nonblocking queue feeds a separate writer thread. When full, records
are dropped and counted instead of delaying forwarding. Disk errors disable file
logging. Pending records are drained after forwarding stops.

Client debug logs record `window`, `frame_size`, `input_window`, and `input_chunk`
on startup. Use those values to verify which
configuration a foreground or managed bridge actually started with.

Counters show aggregate sent/received frames and payload bytes, and dropped log
records. Compare their progress across heartbeat summaries to investigate stalls.
The always-on in-memory history records only lifecycle/control events, not every
Data or Credit frame. Logging is not a packet capture. The reliable
input transport checks record integrity and reports recovery counters separately. A blocked OS stdout write can also prevent a fatal report from being sent.
EOF/decoder failures report partial frame state; silence alone is not proof of
corruption.

Run local validation without GitHub Actions:

```sh
cargo xtask check
cargo xtask integration --prepared
```

The integration tests inject invalid bytes into the actual helper's Bash PTY with
two channels open and assert that recovery preserves both connections. They also
check payload exclusion, private Unix log permissions, and exact binary transfer
with both logs enabled.

