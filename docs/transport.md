# Transport

Each SSH, RDP, HTTP or TCP bridge opens a gateway terminal WebSocket connection, checks or uploads
its bundled helper, and runs it inside the remote terminal. TCP connections share
numbered channels with bounded flow control. HTTP headers, bodies and streaming
responses pass through unchanged. The helper exits with the session; its cached
executable remains.

Client-to-helper input always uses checksummed records, selective acknowledgements,
retries and duplicate suppression. This is the only input protocol. It requires no
selection flags or Guacamole server changes. See [Reliable input](reliable-input.md)
for framing, recovery warnings, upload constraints and measured overhead.

| Setting | Default | Scope |
| --- | ---: | --- |
| `--window` | 4 frames | TCP credits per connection |
| `--frame-size` | 65536 bytes | Maximum payload in a TCP frame |
| `--input-window` | 8 blobs | Guacamole input blobs awaiting gateway acknowledgements, shared by all channels |
| Reliable sequence window | 64 records | Fixed bound on records awaiting remote delivery acknowledgement |

TCP credits and the gateway input window are independent. Gateway acknowledgements
confirm acceptance into Guacamole; reliable acknowledgements confirm decoding at
the remote helper. Reduce `--input-window` to 4 or 2 if recovery warnings repeat,
then try `--window 2`. See [Benchmarking](benchmarking.md) for controlled tuning.

Restart a managed bridge after changing its settings. The Windows installer stops
bridges before upgrading. Before an external package upgrade, run
`guac-bridge prepare-update` and check that it succeeds. Start bridges again afterwards.
The running process retains its original executable and settings.

See [Connections](connections.md) for interfaces and helper deployment, and
[Transport debugging](transport-debugging.md) for collecting client and server logs.

## FortiGate output integrity

FortiGate terminal output also uses the shared reliable record protocol: sequence
numbers, CRC32, selective acknowledgements, bounded history, retries and duplicate
suppression. Records carry complete logical frames before terminal OSC/Base64
chunking. Missing, duplicated or altered terminal chunks cannot be delivered as
unchecked TCP bytes; the receiver requests replay of the damaged record.

Output acknowledgements travel through reliable input. Input acknowledgements
are independent, checksummed feedback messages that do not themselves require
output acknowledgements, avoiding an acknowledgement loop. Windows output flushes each complete OSC sequence separately to avoid large
buffered writes through the terminal. The Guacamole binary output protocol remains
unchanged. Client and helper must come from the same build.
