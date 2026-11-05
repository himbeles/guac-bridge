# 🐸 Guac Bridge

TCP forwarding through Guacamole and FortiGate, with SSH, LLM and RDP presets.

Expose a TCP service on a remote server through its gateway SSH terminal. Connect
your usual application to the local listener. The bridge forwards bytes unchanged,
including SSH, HTTP, TLS and RDP security negotiation.

## Install

On Windows x86-64, use the installer from [Releases](https://github.com/himbeles/guac-bridge/releases).
On Linux x86-64, extract the native archive. Alternatively:

```shell
uv tool install guac-bridge
uv tool update-shell
```

The client uploads the matching bundled helper and reuses it on later connections.
Linux servers need a shell and core utilities. Windows servers need PowerShell
5.1+ and a Windows SSH console. No remote runtime installation, administrator
access or internet connection is required. The helper cache must allow execution.

## TCP forwarding

For Guacamole, copy the full browser URL of the desired SSH terminal and use it
as `CONNECTION_URL`:

```shell
guac-bridge tcp CONNECTION_URL --port 5432
```

This forwards local `127.0.0.1:5432` to port 5432 on the helper server's loopback
interface. For `tcp`, a single `--port` or `--remote-port` value selects both ends.
For different local and remote ports:

```shell
guac-bridge tcp CONNECTION_URL --port 15432 --remote-port 5432
```

TCP is the main command. No application protocol is parsed or translated. HTTP
paths, streamed responses, arbitrary binary data and end-to-end TLS all pass
through the same transport. For example, HTTPS can use a TCP bridge to remote
port 443, while your HTTP client supplies the service hostname and certificate
verification. UDP is not forwarded.

The command starts or reuses a background bridge and returns when ready. Use the
same local port to check or stop it:

```shell
guac-bridge tcp --status --port 5432
guac-bridge tcp --stop --port 5432
```

Use `--foreground` to stay attached until Ctrl+C. Every public argument and tuning
option is documented in `guac-bridge COMMAND --help`.

## Presets

These commands use the same TCP forwarding engine and select default ports and
connection labels:

| Preset | Local port | Remote port | Application connection |
| --- | ---: | ---: | --- |
| `ssh` | 2222 | 22 | `ssh -p 2222 USER@127.0.0.1` |
| `llm` | 8000 | 8000 | Connect your model client to `127.0.0.1:8000` |
| `rdp` | 33899 | 3389 | `mstsc /v:127.0.0.1:33899` |

```shell
guac-bridge ssh CONNECTION_URL
guac-bridge llm CONNECTION_URL
guac-bridge rdp CONNECTION_URL
```

Preset port overrides are independent: `ssh --port 2225` keeps remote port 22.
Use `--remote-port` to change the service port. The `http` command and `--https`
option have been removed; use `tcp` for those services. The RDP local default is
now 33899; existing listeners on other ports can still be controlled by supplying
their original `--port`.

Multiple bridges of any preset can run at once on distinct local ports. Each has
its own gateway/helper session and can be stopped independently. RDP forwarding
connects directly to the service through the helper, without an additional SSH
client or `LocalForward`; the gateway SSH session still starts the helper.

### SSH config integration

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-bridge ssh CONNECTION_URL --save-password --user GATEWAY_USERNAME --port %p"
Match all
```

Connect with `ssh guac-server`. The hook starts or reuses the bridge and relays
startup progress. The bridge remains running after SSH exits.

## FortiGate gateway

Use the portal origin and identify the Windows SSH server opened by the gateway:

```shell
guac-bridge tcp https://gateway.example --port 5432 --gateway fortigate --user PORTAL_USER --server-host server.internal --server-user SERVER_USER --server-os windows
```

The presets accept the same gateway options. Portal and server SSH logins are
separate from the forwarded application's login. Use command help for credential
storage, environment variables and private-CA options. Passwords are not accepted
as command-line arguments.

FortiGate currently supports portal password login, custom SSH sessions and
Windows x86-64 helper servers. MFA/SSO, compliance challenges, bookmarks and Linux
servers through FortiGate are not implemented. See the [FortiGate guide](docs/fortinet-integration.md)
for setup, protocol details and deployment validation.

## Manage and update

```shell
guac-bridge list
guac-bridge tray
guac-bridge prepare-update
```

Connection lists show TCP or the preset name, gateway type, status, user and route.
The Windows tray/manager provides live traffic counters and stop controls. Closing
the manager leaves bridges running. See [Windows bridge manager](docs/tray.md).

The Windows installer stops bridges before updating. Before a uv upgrade:

```shell
guac-bridge prepare-update
uv tool upgrade guac-bridge
```

Run the upgrade after preparation succeeds, then restart bridges. Active sessions
close during update preparation. Guac Bridge does not update itself.

The Windows installer includes a private WinUI runtime. The manager requires
Windows 11 or Server 2025 or newer. CLI forwarding and the tray also work on older
Windows versions. For uv or portable installations, the manager additionally needs
[Windows App Runtime 2.4+ x64](https://learn.microsoft.com/windows/apps/windows-app-sdk/downloads).

Stable releases include native Windows/Linux archives, the Windows installer and
Python packages. Beta releases contain only the Windows installer and checksums.

## Development and documentation

Run `cargo xtask build` and `cargo xtask check` from the repository root.
The application and helper live in [crates/guac-bridge](crates/guac-bridge).

See [Architecture](docs/architecture.md), [Building](docs/building.md),
[Testing](docs/testing.md), [Connections](docs/connections.md),
[Transport](docs/transport.md), [Transport debugging](docs/transport-debugging.md)
and [Benchmarking](docs/benchmarking.md).
