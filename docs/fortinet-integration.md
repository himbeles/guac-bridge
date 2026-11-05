# FortiGate gateway

Guac Bridge connects to the FortiGate SSL-VPN web portal and opens a custom SSH
terminal. The existing helper then forwards SSH, RDP, HTTP(S) or another TCP
service from that server's loopback interface. This adapter uses the web portal,
not a native FortiClient VPN connection.

## Connect

```sh
guac-bridge ssh https://gateway.example --gateway fortigate --user PORTAL_USER --server-host server.internal --server-user SERVER_USER --server-os windows
```

Use `rdp`, `http` or `tcp` with the same gateway/server options. TCP also requires
`--port` and `--remote-port`. For example:

```sh
guac-bridge rdp https://gateway.example --gateway fortigate --user PORTAL_USER --server-host server.internal --server-user SERVER_USER --server-os windows
mstsc /v:127.0.0.1:13389
```

The server must provide a Windows x86-64 SSH console with PowerShell 5.1+ and allow
the helper to execute from its private cache. The forwarded service must already
be listening on the same server's loopback interface. The helper requires no
service installation or remote internet access.

## Keep the endpoints separate

| Setting | Identifies |
| --- | --- |
| URL, `--user`, gateway password | FortiGate portal |
| `--server-host`, `--server-port`, `--server-user`, server password | SSH terminal opened by the portal |
| `--target-host`, `--remote-port` | Loopback TCP service reached by the helper |
| `--port` | Local loopback listener used by your application |

The server SSH port defaults to 22. Service ports depend on the command: SSH 22,
RDP 3389, HTTP 8000, HTTPS 443; generic TCP requires an explicit service port.
Remote Desktop connects directly to the local RDP listener. No extra SSH client
or SSH `LocalForward` is needed; the gateway SSH session still starts the helper.

Portal and server passwords are prompted separately. Use `--save-password` and
`--save-server-password` to save them to the native credential store, or the
corresponding `--forget-*` options to remove them. `--no-keyring` disables lookup
for both. Automation can provide `GUAC_BRIDGE_PASSWORD` and
`GUAC_BRIDGE_SERVER_PASSWORD`; command-line password arguments are not supported.

Use `--ca-file` for a private gateway CA. These trust settings apply to gateway
HTTPS/WSS, independently of the forwarded application's own TLS or RDP security.

## Login and session protocol

The adapter posts URL-encoded `ajax=1`, username and credential fields to
`/remote/logincheck`, using `text/plain;charset=UTF-8`. It retains scoped cookies
and checks the portal redirect/session result. Login, redirect and renewal errors
are surfaced instead of being treated as an authenticated terminal.

The custom SSH connection opens `/remote/rds?connection=...` with the `hterm`
WebSocket subprotocol. Connection JSON contains an empty custom-session name,
`apptype=ssh`, server host and string-valued port. Initialization sends `resize`,
`user`, `passwd` and `start`; subsequent input uses `str`. Length prefixes count
UTF-16 code units, matching the portal's JavaScript implementation.

Output is a raw terminal stream, not Guacamole's pipe/blob protocol. The client
extracts short OSC/Base64 envelopes incrementally across WebSocket messages.
Reliable records add CRC32, sequence numbers, selective acknowledgments, bounded
retries and duplicate suppression. Invalid records never reach application sockets.
Complete OSC sequences are flushed separately on Windows, including retries and
fatal reports. See [Transport](transport.md).

## Supported scope

The current adapter supports portal password login, custom SSH sessions and
Windows x86-64 helper servers. It does not implement MFA/SSO challenges, endpoint
compliance workflows, bookmark selection, Linux servers through FortiGate or the
portal's native graphical RDP session.

Controlled SSH transfers passed on the reported FortiGate/Windows deployment with
default window and frame size after the complete-envelope write change. This is
limited deployment evidence, not validation across all FortiGate firmware. RDP
negotiation and TCP forwarding also have simulated-gateway integration coverage;
those tests do not authenticate against a real corporate RDP service.

## Troubleshooting

Add paired metadata logs to your command:

```powershell
--debug-log client-debug.log --server-debug-log 'C:\Users\REMOTE_USER\server-debug.log'
```

The client file is local; the server file stays on the remote Windows machine.
Use the same reproduction for both captures. Rejected record counts, ACK sequence
numbers and selective bitmaps distinguish damaged output from lost feedback.
A delivery timeout closes the bridge rather than forwarding incomplete bytes.
Increasing a timeout does not repair repeatably damaged records.

See [Transport debugging](transport-debugging.md) for log collection. Run
`guac-bridge COMMAND --help` for every argument, default and credential option.
