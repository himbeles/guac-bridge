# 🐸 Guac Bridge

Access remote TCP services through a gateway SSH terminal using your normal local
applications. One transport carries SSH, web traffic, model APIs and Remote
Desktop, preserving the application's authentication and encryption.

## Gateways

| Gateway | Connection URL | Setup |
| --- | --- | --- |
| **Apache Guacamole** (default) | Full browser URL of the SSH terminal | Copy the connection URL from your browser |
| **FortiGate** | SSL-VPN portal origin | Select `--gateway fortigate` and supply the server SSH settings |

Both options start the matching helper inside the gateway's SSH terminal. The
helper connects to a service on that server's loopback interface. See
[Connections](docs/connections.md) and the [FortiGate setup guide](docs/fortinet-integration.md)
for gateway-specific configuration.

## Install

- **Windows x86-64:** run the installer from [Releases](https://github.com/himbeles/guac-bridge/releases).
- **Linux x86-64:** extract the native release archive.
- **Python/uv:** install the same native CLI with:

```shell
uv tool install guac-bridge
uv tool update-shell
```

No manual installation is needed on the helper server. Linux servers need a shell
and core utilities; Windows servers need PowerShell 5.1+ and a Windows SSH console.
The helper requires no remote runtime installation, administrator access or
internet connection. Its private cache must allow execution.

### Windows desktop manager

The installer includes a private UI runtime. The Windows manager requires Windows
11 or Server 2025 or newer. CLI forwarding and the tray also work on older Windows
versions. For uv or portable installations, the manager additionally needs
[Windows App Runtime 2.4+ x64](https://learn.microsoft.com/windows/apps/windows-app-sdk/downloads).

## Usage

### Forward a TCP service

```shell
guac-bridge tcp CONNECTION_URL --port 5432
```

Connect your application to `127.0.0.1:5432`. The bridge forwards to port 5432 on
the helper server's loopback interface. A single `--port` or `--remote-port` value
selects both ends. For different ports:

```shell
guac-bridge tcp CONNECTION_URL --port 15432 --remote-port 5432
```

The bridge starts or reuses a background process and returns when ready. Use
`--foreground` to stay attached until Ctrl+C. TLS, authentication, application
headers and streamed responses pass unchanged. UDP is not forwarded.

Every public argument and transport option is documented in
`guac-bridge COMMAND --help`.

### Use a port preset

Presets use the same TCP engine and select default ports and connection labels.

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

Preset overrides are independent: `ssh --port 2225` keeps remote port 22. Use
`--remote-port` to change the service port. Multiple bridges of any preset can run
on distinct local ports, with separate gateway/helper sessions and stop controls.

RDP forwarding connects your Remote Desktop client directly to the service through
the helper. The gateway SSH session starts the helper; an additional SSH client
or `LocalForward` is not needed.

### Manage bridges

```shell
guac-bridge list
guac-bridge stop --port 5432
guac-bridge stop all
```

`list` shows the preset, gateway, status, user and route. `stop --port` selects a
bridge by its local listener, regardless of preset. `stop all` stops all registered
running bridges. Neither stop command needs a gateway URL or login.

On Windows, `guac-bridge tray` opens the manager with live counters and stop
controls. Closing the manager leaves bridges running. See
[Windows bridge manager](docs/tray.md).

### Integrate with SSH config

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-bridge ssh CONNECTION_URL --save-password --user GATEWAY_USERNAME --port %p"
Match all
```

Connect with `ssh guac-server`. The hook starts or reuses the bridge and relays
startup progress. The bridge stays running after SSH exits. Each SSH alias can use
a different local port while retaining remote port 22.

## Update

The Windows installer stops bridges before updating. For a uv installation:

```shell
guac-bridge prepare-update
uv tool upgrade guac-bridge
```

Upgrade after preparation succeeds, then restart bridges. Preparation closes
active connections, the tray and manager. Guac Bridge does not update itself.

## Reference

For all commands and arguments, run `guac-bridge --help` or
`guac-bridge COMMAND --help`.

| Guide | Contents |
| --- | --- |
| [Connections](docs/connections.md) | Authentication, helper deployment and bridge lifetime |
| [Windows bridge manager](docs/tray.md) | Tray controls and desktop manager |
| [Architecture](docs/architecture.md) | Components and data flow |
| [Transport](docs/transport.md) | Framing and reliable delivery |
| [Transport debugging](docs/transport-debugging.md) | Logs and troubleshooting |
| [Benchmarking](docs/benchmarking.md) | Measuring throughput |

## Development

Run these commands from the repository root:

```shell
cargo xtask build
cargo xtask check
```

The application and helper live in [crates/guac-bridge](crates/guac-bridge).
See [Building](docs/building.md) and [Testing](docs/testing.md) for requirements
and validation workflows.
