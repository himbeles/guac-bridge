# 🐸 Guac Bridge

SSH and OpenAI-compatible endpoints through Guacamole.

Expose a remote server’s SSH service or OpenAI-compatible API on your computer through its Guacamole web terminal. Use your usual SSH tools or LLM applications with the local port.

## First-time setup

On **Windows x86-64**, run the installer from [Releases](https://github.com/himbeles/guac-bridge/releases). Select **Add Guac Bridge to my user PATH** if desired. On **Linux x86-64**, extract the native release archive.

Alternatively, install on either client with uv:

```shell
uv tool install guac-bridge
uv tool update-shell
```

No installation is needed on the server. The client uploads the matching bundled x86-64 helper through the Guacamole SSH terminal and reuses it on later connections. Linux servers need a shell and core utilities; Windows servers need PowerShell 5.1+ and a Windows SSH console. No runtime installation, administrator access or internet connection is required there. The user’s helper cache must allow execution.

Open the desired Guacamole terminal in your browser and copy its full URL. Use it as `CONNECTION_URL` below.

## SSH bridge

### Connect

```shell
guac-bridge ssh CONNECTION_URL
```

For a Windows server, add `--server-os windows` to either bridge command or the SSH Match hook. The default is Linux. RDP connections are not supported.

Enter your Guacamole username and password. Once the bridge is ready, connect:

```shell
ssh -p 2222 SERVER_USERNAME@127.0.0.1
```

The command starts or reuses a background bridge and returns when ready. It keeps running after this terminal or SSH closes. Use `guac-bridge ssh --stop` to stop it. Add `--foreground` to run in the current terminal until **Ctrl+C**.

### Use an SSH config

Add to `~/.ssh/config`, replacing the URL and both usernames:

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-bridge ssh CONNECTION_URL --save-password --user GUACAMOLE_USERNAME --local-port %p"
Match all
```

Connect with `ssh guac-server`. The hook starts or reuses the background bridge and shows startup progress in your calling console. On first login it prompts in the calling console and saves your password to the system credential store after successful authentication.

The bridge stays running after SSH exits. Check or stop it with:

```shell
guac-bridge ssh --status --local-port 2222
guac-bridge ssh --stop --local-port 2222
```

Use a dedicated local port for each destination. Windows users can add `--show-console` to see the background bridge in a separate console.

## LLM bridge

```shell
guac-bridge llm CONNECTION_URL
```

Enter your Guacamole credentials. The default local port is **8000**, forwarding the server’s HTTP endpoint at **127.0.0.1:8000**, including streamed responses. Use `--remote-port` for a different server port and `--local-port` for a different local port.

The bridge runs in the background by default. Stop it with `guac-bridge llm --stop`. Use `--foreground` to keep it attached to this terminal until **Ctrl+C**.

`guac-bridge` is the CLI entry point. Use its `ssh` and `llm` subcommands. Run `guac-bridge --help` or a subcommand with `--help` for credential storage, certificates, background operation and transport tuning.

## List bridges

```shell
guac-bridge list
```

Lists registered SSH and LLM bridges with status, Guacamole username and the route
**local URL → Guacamole jump-host URL → service on the remote server**.
The server endpoint is loopback on the remote machine. Unresponsive registrations
are marked explicitly. URL credentials and authentication queries are omitted.

`ssh` and `llm` start or reuse a managed background bridge by default. Use
`--foreground` for an attached process, including `--local-port 0` for an
automatically assigned local port.

## Contribution

The application and helper live in [crates/guac-bridge](crates/guac-bridge).
Build and check them on either platform with `cargo xtask build` and `cargo xtask check`.
[Architecture](docs/architecture.md) describes module ownership and behavior to preserve.
The uv package installs the same native client.

See [Building](docs/building.md), [Testing](docs/testing.md), [Transport debugging](docs/transport-debugging.md) and [Benchmarking](docs/benchmarking.md). Submit issues and pull requests on [GitHub](https://github.com/himbeles/guac-bridge).

## Documentation

See [Transport](docs/transport.md) for flow control and reliability, and
[Connections](docs/connections.md) for deployment and process lifetimes.

On Windows, running bridges appear in the tray. Use `guac-bridge tray` to open
the manager with SSH/LLM tags, live rates, session totals and stop controls.
See [Windows bridge manager](docs/tray.md).

The Windows installer stops running bridges and the tray before updates.
For a uv installation, stop them before upgrading:

```shell
guac-bridge prepare-update
uv tool upgrade guac-bridge
```

Run the upgrade only after preparation succeeds. Active sessions disconnect;
restart bridges afterwards. Guac Bridge does not update itself.

The Windows manager uses native WinUI 3 via Rust Reactor. The Windows installer
includes a private UI runtime, so no separate runtime installation is needed.
For uv and portable installations, install the shared
[Windows App Runtime 2.4+ (x64)](https://learn.microsoft.com/windows/apps/windows-app-sdk/downloads).
Command-line bridges and the tray icon work without it.

The WinUI manager requires Windows 11 or Windows Server 2025 or newer because Reactor uses the OS package dependency APIs. Command-line bridges and the native tray host remain available on earlier Windows versions.
