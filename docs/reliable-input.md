# Reliable input transport

All SSH and LLM bridges use this transport automatically, including managed
bridges. There is no protocol selection flag and no raw
input fallback. Stop an existing managed bridge after upgrading, then restart it
with your normal command. The bundled helper updates automatically by content hash.

```powershell
guac-bridge ssh --foreground 'YOUR_GUACAMOLE_URL' --user 'YOUR_USERNAME' --port 2223
```

This protects the **client-to-helper console input** against missing, duplicated
or corrupted bytes. It does not require a guacd change. It does not protect
arbitrary damage in the return console stream or repair an expired Guacamole
session, a broken WebSocket, or a failed target SSH server.

## Records and delivery

The existing TCP frame header, payload, credits and half-close semantics are
unchanged. Before terminal escaping, input frames are wrapped in records:

| Field | Bytes |
| --- | ---: |
| Magic `RI` and version 1 | 3 |
| Sequence number, starting at 1 (big endian) | 8 |
| Payload length (big endian) | 4 |
| TCP frame bytes, including original headers | 1–65548 |
| IEEE CRC32 of the preceding fields | 4 |

COBS encoding removes zero bytes, then ESC and the escape byte are escaped for the
terminal. Zero delimiters bound each record, allowing the receiver to resynchronize
after missing bytes or delimiters. CRC validation happens before the TCP decoder
sees the contents. Windows applies its existing Base64/Enter console carrier to
these same records. CRC detects accidental damage; it is not authentication.

The helper retains up to 64 out-of-order records and delivers contiguous records
exactly once. An `InputAck` frame (kind 12, channel 0) carries the next
required sequence, a 64-bit selective acknowledgement mask, a cumulative rejected
record count, and a CRC32 (28 payload bytes). Duplicate records are acknowledged
without repeating TCP delivery. Ready metadata confirms the selected mode.

The client retains a fixed bounded sequence window of 64 records. Selectively acknowledged buffers are freed, but the permitted sequence
range advances only with contiguous delivery. Maximum payload retained in either
sequence window is about 4 MiB; encoded wire copies can be larger for escape-heavy
payloads. Existing queues and TCP credit bounds also apply.

Missing records are retried promptly when later sequences reveal a gap. Otherwise
the initial retry timeout is 500 ms; clean ACK samples set it to three times the
observed delay, bounded to 250 ms–30 s. Retries back off exponentially. Delivery is
bounded by `--connect-timeout` measured from the original send. There is no periodic
retry wakeup when no record is pending. SIMD byte searches and bulk copies keep
record scanning off a byte-by-byte path. A record fits one maximum TCP frame or
several smaller frames already waiting in the input queue. Coalescing never waits
for more data. This avoids an ACK for each queued control event, without adding a
fill timer or fragmenting full TCP frames into extra Guacamole input blobs.

## Warnings and remedies

Foreground clients print recovery warnings to stderr, at most once every five
seconds, with updated totals on orderly shutdown. Managed clients write them to
their client log. Use `--foreground` to see warnings in the current terminal. The debug log also records the warnings when enabled.

Counters report retransmitted records, rejected records, invalid ACK checksums,
and sequence gaps. They are **not IP packet-loss counters**. A retry can mean a
lost or delayed ACK even when the helper received the data. No recovery warning
is emitted for healthy traffic.

If warnings repeat:

1. Reduce `--input-window` from 8 to 4, then 2. This limits gateway input pressure.
2. Try `--window 2` if bursts remain problematic.
3. Increase `--connect-timeout` for an otherwise healthy but slow link.
4. Capture both logs as described in [transport debugging](transport-debugging.md).

Permanent loss produces an actionable error rather than a successful but damaged
stream. The transport also retains gateway input ACK handling: that ACK
alone still does not prove that the remote shell consumed the bytes.

## Helper upload and progress

A receiver inside the helper cannot protect the first upload of that same helper.
First-install Linux upload therefore keeps its existing independently decoded
24 KiB Base64 blocks, remote shell block acknowledgements, final SHA256 and atomic
installation. Windows retains its existing PowerShell bootstrap and final checksum.
The protected transport starts only after the helper is running.

Interactive terminals display one updating upload progress bar. Redirected output
uses sparse, readable progress lines, and SSH Match hooks redraw a bar on their
calling terminal. Linux progress represents remote shell confirmation; Windows
progress represents gateway acknowledgement until final checksum verification.
If an upload stalls or is damaged, try `--input-window 4` or `2`; on Linux also try
`--upload-batch-size 12288` or `3072`.

## Validation and performance

The test gateway uses a real Bash PTY and a remote PATH without Python or uv. Fault
tests acknowledge input at the gateway and then drop, corrupt or duplicate its
bytes before the PTY. Concurrent SSH and LLM transfers must return exact payloads.
Codec tests cover lost ACKs, missing delimiters, reordering, bounds, invalid ACK
checksums, permanent-loss deadlines, and Windows armor damage and maximum frames.
A real terminal test checks that upload progress occupies one row.

