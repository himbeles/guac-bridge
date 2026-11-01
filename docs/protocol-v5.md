# Binary transport in 0.10.0

Both SSH and LLM bridges use compact binary frames with raw payload bodies.
JSON remains only for ready/request/header metadata. Guacamole still encodes
its blob payloads in Base64; the extra payload encoding is removed.

Each record has a 12-byte network-order header: `GB`, protocol byte `5`, event
opcode, a 32-bit channel ID and a 32-bit payload length. ESC (`1b`) becomes
`1d 3b`; GS (`1d`) becomes `1d 3d`. No encoded record contains a raw ESC, so even
a payload containing a Guacamole OSC close command stays data in the terminal
pipe. Helpers enter raw TTY mode before emitting readiness.

Data bodies are bytes. Credit/EOF/close/cancel/end events have no payload.
Ready and HTTP-header events carry JSON metadata. An HTTP request carries a
32-bit metadata length, JSON metadata, then the raw request body. Error text
is UTF-8. Numeric IDs replace UUID strings on the wire.

Decoders reject invalid magic/version/opcodes, reserved IDs, bad escapes,
oversized lengths, malformed/reserved metadata and truncated input. Limits are
64 KiB per data frame, 64 KiB metadata and 16 MiB per HTTP request body. Bodies
are never interpreted as terminal text or JSON by the bridge.

Defaults are 64 KiB maximum SSH frames, 32 data credits and 64 input ACKs.
Input blobs stay within Guacamole's 6048-byte limit. `--window N` sets data
credits and an input ACK budget of `2 × N`. LLM responses use available bytes
up to 64 KiB via `read1`, without waiting to fill a frame. Larger windows/SSH frames trade
more buffering for bulk throughput; they do not speed up model inference.

The independent bootstrap still checks versions, offers updates and reuses the
helper's Python interpreter and configured package index. Both installed ends
must support protocol v5. Keychain login, managed SSH startup, cancellation,
HTTP fallback and the ordinary CLI entry points are retained.

Tests cover all 256 bytes, adjacent escapes, fragmented/coalesced messages,
metadata integrity, size limits, large Unicode requests, SSE, concurrent
channels, half-closes, helper updates and shutdown. See [connection tests](testing.md).
