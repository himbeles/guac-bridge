# 🐸 Guac Bridge

SSH, RDP, HTTP(S) and TCP bridges through Guacamole and FortiGate.

Expose a remote server’s SSH, RDP, HTTP(S) or other TCP service through its gateway web terminal. Use your usual SSH tools, Remote Desktop, browser or HTTP client with the local port.

## First-time setup

On **Windows x86-64**, run the installer from [Releases](https://github.com/himbeles/guac-bridge/releases). Select **Add Guac Bridge to my user PATH** if desired. On **Linux x86-64**, extract the native release archive.

Alternatively, install on either client with uv:

```shell
uv tool install guac-bridge
uv tool update-shell
```

No installation is needed on the server. The client uploads the matching bundled x86-64 helper through the gateway SSH terminal and reuses it on later connections. Linux servers need a shell and core utilities; Windows servers need PowerShell 5.1+ and a Windows SSH console. No runtime installation, administrator access or internet connection is required there. The user’s helper cache must allow execution.

For Guacamole, open the desired SSH terminal in your browser and copy its full URL. Use it as `CONNECTION_URL` below. For FortiGate, use the portal origin and the [gateway options](#fortigate-gateway).

## SSH bridge

### Connect

```shell
guac-bridge ssh CONNECTION_URL
```

For a Windows server, add `--server-os windows` to any bridge command or the SSH Match hook. The default helper OS is Linux. The gateway must provide an SSH terminal to start the helper.

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

Match originalhost guac-server exec "guac-bridge ssh CONNECTION_URL --save-password --user GUACAMOLE_USERNAME --port %p"
Match all
```

Connect with `ssh guac-server`. The hook starts or reuses the background bridge and shows startup progress in your calling console. On first login it prompts in the calling console and saves your password to the system credential store after successful authentication.

The bridge stays running after SSH exits. Check or stop it with:

```shell
guac-bridge ssh --status --port 2222
guac-bridge ssh --stop --port 2222
```

Use a dedicated local port for each destination.

## RDP bridge

```shell
guac-bridge rdp CONNECTION_URL
mstsc /v:127.0.0.1:13389
```

This forwards local **127.0.0.1:13389** directly to the remote server's
**127.0.0.1:3389**. Use `--port` and `--remote-port` to choose other ports.
Remote Desktop connects directly to the local listener; no extra SSH client,
SSH key or `LocalForward` configuration is needed.

For FortiGate:

```shell
guac-bridge rdp https://gateway.example --gateway fortigate --user PORTAL_USER --server-host server.internal --server-user SERVER_USER --server-os windows
```

The gateway SSH session still starts the existing helper. Remote Desktop performs
its own Windows authentication and RDP security negotiation through the bridge.
The remote RDP service must already be running on the helper server. This forwards
RDP over TCP; UDP transport is not forwarded.

RDP bridges run in the background and appear as **RDP** in the CLI and Windows
manager. Stop the default listener with `guac-bridge rdp --stop`, or supply the
same `--port` used when starting it.

## General TCP bridge

```shell
guac-bridge tcp CONNECTION_URL --port 15432 --remote-port 5432
```

This forwards local **127.0.0.1:15432** to **127.0.0.1:5432 on the helper server**.
Both ports are required. TCP bytes pass unchanged, including binary protocols and
end-to-end TLS. Connect your service's normal client to the local listener.
`--target-host` can select the remote loopback address; other network hosts are
not supported.

The same gateway and credential options apply. For example, add `--gateway
fortigate --server-host server.internal --server-user SERVER_USER --server-os
windows` for FortiGate. The gateway's terminal session still starts the helper.

Use `guac-bridge tcp --status --port 15432` or `guac-bridge tcp --stop --port 15432`
for background control. TCP bridges appear as **TCP** in connection lists.

## HTTP bridge

```shell
guac-bridge http CONNECTION_URL
```

The default local port is **8000**, forwarding the server’s HTTP service at
**127.0.0.1:8000**. All paths, methods, headers, binary bodies, streaming responses
and protocol upgrades pass through without endpoint filtering. Use `--remote-port`
for another server service port and `--port` for another local listener.

For HTTPS:

```shell
guac-bridge http CONNECTION_URL --https
```

This defaults the remote port to **443** and shows HTTPS URLs in connection lists.
TLS passes unchanged between your HTTP client and the remote service; the bridge
does not terminate TLS. Preserve the service's hostname for SNI and certificate
verification. For example:

```shell
curl --connect-to service.example:443:127.0.0.1:8000 https://service.example/any/path
```

`--ca-file` configures trust for the gateway connection. Remote HTTPS certificate
trust is configured in your HTTP client as usual.

The bridge runs in the background by default. Stop it with `guac-bridge http --stop`.
Use `--foreground` to keep it attached until **Ctrl+C**. Start and stop commands
must select the same `--port`.

`guac-bridge` is the CLI entry point. Use its `ssh`, `rdp`, `http` and `tcp` subcommands. Run
`guac-bridge --help` or a subcommand with `--help` for credentials, certificates,
background operation and transport tuning.

Compatibility: `llm` is deprecated. Use `http`, which shares its port-8000 defaults. Existing invocations and managed bridge registrations remain supported.

## FortiGate gateway

The FortiGate adapter opens a custom SSH terminal through the SSL-VPN
web portal. SSH, RDP, HTTP and TCP bridges use this same connection path. Existing
Guacamole commands are unchanged; `--gateway` defaults to `guacamole`.

```sh
guac-bridge ssh https://gateway.example --gateway fortigate --user PORTAL_USER --server-host server.internal --server-port 22 --server-user SERVER_USER --server-os windows

guac-bridge http https://gateway.example --gateway fortigate --user PORTAL_USER --server-host server.internal --server-user SERVER_USER --server-os windows
```

Portal and server SSH logins are separate. Run `guac-bridge rdp --help` (or the
help for your chosen command) for credential storage, endpoint options and TLS
configuration. Passwords are prompted or supplied through the documented
environment variables; they are not command-line arguments.

Beta releases distribute the Windows installer only, through GitHub prereleases.
They do not publish Python wheels, Linux clients or portable archives. Stable releases
retain the existing distribution formats.

FortiGate supports portal password login, custom SSH sessions and Windows x86-64
helper servers. MFA/SSO, endpoint compliance challenges, bookmarks and Linux servers
through FortiGate are not implemented. Controlled SSH transfers have been verified
on the reported Windows/FortiGate deployment. See the [FortiGate guide](docs/fortinet-integration.md)
for setup, protocol details and validation limits.

CLI connection lists and the Windows manager label each bridge **Guacamole** or
**FortiGate**, including older Guacamole registrations.

## List bridges

```shell
guac-bridge list
```

Lists registered SSH, RDP, HTTP and TCP bridges in an aligned table with gateway type, status, gateway username and the route
**local URL → gateway → service on the remote server**.
The server endpoint is loopback on the remote machine. Unresponsive registrations
are marked explicitly. URL credentials and authentication queries are omitted.

`ssh`, `rdp`, `http` and `tcp` start or reuse a managed background bridge by default. Use
`--foreground` for an attached process, including `--port 0` for an
automatically assigned local port.

## Contribution

The application and helper live in [crates/guac-bridge](crates/guac-bridge).
Build and check them on either platform with `cargo xtask build` and `cargo xtask check`.
[Architecture](docs/architecture.md) describes module ownership and behavior to preserve.
The uv package installs the same native client.

See [Building](docs/building.md), [Testing](docs/testing.md), [Transport debugging](docs/transport-debugging.md) and [Benchmarking](docs/benchmarking.md). Submit issues and pull requests on [GitHub](https://github.com/himbeles/guac-bridge).

## Documentation

Use `guac-bridge --help` or `guac-bridge COMMAND --help` for all arguments and defaults.
See [Transport](docs/transport.md) for flow control and reliability, and
[Connections](docs/connections.md) for deployment and process lifetimes.

On Windows, running bridges appear in the tray. Use `guac-bridge tray` to open
the manager with SSH/RDP/HTTP/TCP tags, live rates, session totals and stop controls.
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
