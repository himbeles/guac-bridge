# Native SSH bridge comparison

The independent Rust client and helper use binary multiplexing and escape only
ESC and the escape byte. This removes JSON and the inner Base64 encoding while
preserving a local SSH listener, concurrent channels, half-closes, bounded
buffering, timeouts and clean shutdown.

Build and usage are described in [the native prototype README](../rust-ssh/README.md).

## Reproduce

From a Linux checkout with Bash and PTY support, build optimized binaries and
run the commands sequentially:

```shell
cargo build --release --locked --manifest-path rust-ssh/Cargo.toml
cargo test --release --locked --manifest-path rust-ssh/Cargo.toml
uv run python -m unittest discover -s tests -p test_rust_ssh.py -v
uv run python -m benchmarks.rust_ssh --mib 16 --seconds 4 --repeats 5 --output benchmarks/results/rust-ssh-paced.json
uv run python -m benchmarks.rust_ssh --mib 8 --seconds 0 --repeats 3 --rtts 0,20 --output benchmarks/results/rust-ssh-saturation.json
```

`python` is the production Python bridge. `rust-matched` uses the native binary
protocol with the Python bridge's frame/window/blob sizes. `rust` uses native
defaults: 64 KiB frames, 32 channel credits, 6048-byte input blobs and 64 input
ACKs in flight. Neither native profile is a language-only comparison: both
change framing as well as implementation.

The harness runs each client in a separate process and an actual helper inside
a Bash PTY. A simulated Guacamole WebSocket gateway enforces 8192-byte instruction
and 6048-byte blob limits. No external hosts, model or user credentials are used.
The synthetic delay is an application-byte RTT proxy, not a complete WAN model.

Measured client CPU excludes startup, warmup and shutdown. Whole-lifetime
client/helper CPU comes from `wait4`, includes those phases and covers identical
work: one warmup transfer plus one measured transfer. These totals exclude
gateway, target and measurement-supervisor CPU. Peak RSS is per-process high-water
memory, not a claim about worst-case buffering. CPU work is not electrical energy.

Larger frames/windows can increase buffering. Small interactive writes are sent
immediately without waiting to fill a frame; TCP_NODELAY is enabled on native TCP
and WebSocket connections. Tests cover every byte value and OSC close markers,
both half-close directions, concurrent connections, small writes, target refusal,
Guacamole disconnect and stalled input acknowledgements.

The prototype uses existing Guacamole terminal pipe support without server
modifications. References:
[pipe byte handling](https://github.com/apache/guacamole-server/blob/main/src/terminal/terminal-handlers.c),
[input pipe](https://github.com/apache/guacamole-server/blob/main/src/terminal/terminal-stdin-stream.c),
[instruction size](https://guacamole.apache.org/doc/1.6.0/libguac/parser-constants_8h_source.html),
[blob size](https://guacamole.apache.org/doc/1.6.0/libguac/protocol-constants_8h_source.html).
