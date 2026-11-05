# Maintaining Guac Bridge

Two entry points have distinct jobs:

- `guac-bridge`: the installed application. `guac_bridge::run(argv)` is its Rust entry point.
- `cargo xtask`: repository builds, checks, packaging and release preparation.

The standalone `guac-bridge-agent` is an embedded server helper, built explicitly
with `--no-default-features --features agent`. It is not another public client CLI.
Python/uv only distribute and launch the native executable.

## Ownership

| Location | Responsibility |
| --- | --- |
| `src/cli.rs` | Parse application commands and own runtime/exit-code dispatch |
| `src/options.rs` | Bridge options, defaults and validation |
| `src/client.rs` | Forwarding lifecycle, channels, backpressure and shutdown |
| `src/client/guacamole.rs` | Guacamole destination, login and session refresh |
| `src/client/fortigate.rs` | Experimental FortiGate portal login, hterm sessions and bounded OSC decoding |
| `src/client/tls.rs` | Shared gateway HTTP/TLS trust configuration |
| `src/background.rs` | Authenticated registrations, inspection and stop controls |
| `src/background/startup.rs` | Child launch, password pipe and startup progress relay |
| `src/deploy.rs`, `windows_bootstrap.ps1` | Remote helper deployment and checksum verification |
| `src/lib.rs`, `reliable.rs`, `terminal_wire.rs` | Shared wire formats and reliable input |
| `src/tray.rs`, `manager.rs` | Native tray lifetime and WinUI presentation |
| `xtask/src/build.rs` | Helper-first build order and test execution |
| `xtask/src/distribution.rs` | Portable archives, including wheel extraction |
| `xtask/src/installer.rs` | Private runtime, manifest, licenses and Inno Setup |
| `xtask/src/release.rs` | Version validation, release notes and checksums |

## Behavior to preserve

A normal SSH or HTTP command starts or reuses a background bridge and returns only
when its helper is ready. `--foreground` stays attached to the current terminal.
The local listener binds before remote login so a busy port fails promptly.

Managed startup passes the password through the child's stdin, never arguments.
Control sockets require their random token. Registrations are not PID-kill lists.
Update preparation blocks new forwarding startup and waits for authenticated
bridges, the manager and tray to exit.

The tray starts when the local listener opens. Closing the manager keeps the tray
and bridges alive. UI polling runs off its rendering thread.

Client and embedded helper versions must match. Helper upload progress follows
remote acknowledgements and checksum verification. Guacamole input ACKs alone
are not proof of remote shell consumption.

Forwarding is byte-transparent, bounded and half-close aware. Keep HTTP streaming,
channel credits, reliable-input windows, PTY framing and session-refresh behavior
covered when touching the data path. See `transport.md` for the protocol.

## Working on a change

Run `cargo xtask check`, then `cargo xtask integration --prepared` on the relevant
platform. Windows UI/installer checks require Windows; manylinux packaging stays
in CI. The optional Python test supervisors simulate Guacamole but exercise real
client/helper processes and real terminal transports. They do not validate a
particular deployed corporate gateway.

Change `workspace.package.version` only when preparing an actual release. The
Python manifest declares a dynamic version, and the installer receives its version
from xtask. Build tools never publish by themselves.
