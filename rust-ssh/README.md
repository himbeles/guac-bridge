# Native Guacamole SSH bridge experiment

Independent Rust executables expose a server's loopback SSH port on the
client through a Guacamole terminal. Neither executable needs Python.

This foreground prototype implements Guacamole username/password login over
HTTP(S), a WebSocket tunnel, a loopback listener, multiple concurrent TCP
channels, half-closes, bounded buffering, timeouts and Ctrl+C shutdown.
It uses its own binary protocol: install the native helper alongside the
Python helper and point the native client at it.

## Install and connect

Download the artifacts from the
[successful native build](https://github.com/himbeles/guac-bridge/actions/runs/41310978731):

- `native-ssh-Windows`: use `guac-ssh-client-rs.exe` on the client.
- `native-ssh-linux-x86_64-musl-helper`: use `guac-ssh-server-rs` on the server.

Put the helper in `~/.local/bin/` on the server and make it executable:

```shell
chmod +x ~/.local/bin/guac-ssh-server-rs
```

On the Windows client run:

```powershell
.\guac-ssh-client-rs.exe --url https://guacamole.example/guacamole/#/client/CONNECTION
```

Enter the Guacamole username and password when prompted. In another terminal:

```shell
ssh -p 2222 user@127.0.0.1
```

Use `--username`, `--port`, `--server-command` and `--target-port` when needed.
The listener and remote target are limited to loopback. Ctrl+C closes the bridge
and all its channels. It stays in the foreground until stopped, independently
of individual SSH sessions. TLS verification uses the system roots; add a
corporate CA with `--ca-file` if needed. `GUAC_BRIDGE_PASSWORD` is an optional
password input for automation; no password argument is accepted or logged.

The [Native SSH prototype workflow](../.github/workflows/rust-prototype.yaml)
builds Windows executables and a static Linux x86-64 musl helper. Prefer the
musl helper on older Linux distributions such as RHEL 8; an executable built
against a newer glibc may not run there. Ordinary Linux builds should be made
on the target distribution or one with a compatible runtime.

## Efficient transport

The bridge uses numeric channel IDs and compact length-prefixed binary frames.
It escapes ESC and its escape byte, allowing arbitrary bytes through the
terminal pipe without introducing terminal commands. This removes JSON and
the inner Base64 encoding. Guacamole's own Base64 blobs remain.

Defaults are 64 KiB maximum TCP frames, 32 credits per channel, and 64 input
blobs in flight. Input blobs stay within Guacamole's 6048-byte limit. Larger
frames/windows reduce overhead and keep delayed links busy, with more buffering
(up to roughly 2 MiB per channel/direction for data credits, plus I/O queues).
Slow receivers still apply backpressure. No SSH payload compression is used.

## Scope

This is a performance prototype, not the production Python CLI feature set.
It does not implement HTTP-tunnel fallback, WebSocket proxy tunneling, keychain
storage, managed background/SSH Match startup, server package updates, SSO or MFA.
Use it where direct WebSocket access is available. The binaries do not require
Guacamole changes. Keep using your normal SSH client for SSH authentication.

## Build, verify and benchmark

To build from the repository checkout:

```shell
cargo build --release --locked --manifest-path rust-ssh/Cargo.toml
cargo test --release --locked --manifest-path rust-ssh/Cargo.toml
uv run python -m unittest discover -s tests -p test_rust_ssh.py -v
uv run python -m benchmarks.rust_ssh --output benchmarks/results/rust-ssh-paced.json
```

The Python harness only orchestrates local fixtures and measures processes; it
is not part of the native bridge. Findings and full reproduction instructions
are in [the comparison](../docs/rust-ssh-results.md).
