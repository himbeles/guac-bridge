# Native transport

The client and its bundled helper share a single Rust codec. A 12-byte big-endian header contains `GB`, protocol version 5, one-byte opcode, channel ID, and payload length. Channels carry open/opened, data, credit, EOF, close/closed, error and global ready/heartbeat/shutdown records. Ready metadata identifies the exact package version, window and frame size.

Each data frame contains up to 64 KiB of raw TCP bytes. Only ESC and GS are escaped so Guacamole’s terminal output pipe cannot interpret data as terminal commands. Guacamole blob instructions still apply their required outer Base64 encoding. No per-data-frame JSON or additional payload Base64 is used. HTTP runs over the same TCP channels as SSH; there are no HTTP request envelopes or response-body conversions.

Default data credits are 32 per connection. Every successfully written data frame returns one credit. The input acknowledgement window is twice the configured data window, with a fixed maximum 6048 raw bytes per Guacamole input blob. Bounded queues and independent half-closes preserve interactive writes and concurrent TCP streams.

Bootstrap uses a separate named output pipe with a per-connection nonce and ASCII status. Missing helpers are uploaded as a known-length Base64 stream, decoded, verified by SHA-256 and atomically installed. The helper then replaces the shell and opens the active binary output pipe. Guacamole tunneling uses WebSocket only.
