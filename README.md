# 🐸 Guac Bridge

Access remote TCP services through a gateway SSH terminal using your normal local
applications: SSH, web clients, model APIs and Remote Desktop.

## Install

- **Windows x86-64:** run the installer from [Releases](https://github.com/himbeles/guac-bridge/releases).
- **Linux x86-64:** extract the native release archive.
- **Python/uv:** install the same native CLI with:

```shell
uv tool install guac-bridge
uv tool update-shell
```

The remote helper is deployed automatically. See [Connections](docs/connections.md)
for server requirements.

## Usage

```shell
guac-bridge tcp CONNECTION_URL --port 5432
```

Connect your application to `127.0.0.1:5432` to reach port 5432 on the remote
server. Add `--remote-port` if the remote service uses a different port.

For **Apache Guacamole** (the default), use the SSH terminal's full browser URL as
`CONNECTION_URL`. For **FortiGate**, use the SSL-VPN portal origin, select
`--gateway fortigate` and supply the server SSH settings. See
[Connections](docs/connections.md) and the [FortiGate setup guide](docs/fortinet-integration.md).

The bridge starts in the background and returns when ready. Connect your local
application to start using it.

### Port presets

Or use a preset with default ports:

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

Override the local port with `--port` or the service port with `--remote-port`.
Multiple bridges can run on different local ports.

### Manage bridges

```shell
guac-bridge list
guac-bridge stop --port 5432
guac-bridge stop all
```

`list` shows active bridges. Stop one by its local port, or stop them all.

On Windows, `guac-bridge tray` opens the manager with live counters and stop
controls. Closing the manager leaves bridges running. See
[Windows bridge manager](docs/tray.md).

## Update

The Windows installer stops bridges before updating. For a uv installation:

```shell
guac-bridge prepare-update
uv tool upgrade guac-bridge
```

Preparation closes active connections and the manager. Upgrade after it succeeds,
then restart your bridges.

## Reference

For all commands and arguments, run `guac-bridge --help` or
`guac-bridge COMMAND --help`.

| Guide | Contents |
| --- | --- |
| [Connections](docs/connections.md) | Server requirements, authentication and helper deployment |
| [SSH config integration](docs/ssh-config.md) | Start bridges automatically from SSH aliases |
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
