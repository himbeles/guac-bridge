# Native SSH bridge comparison

The independent Rust client and helper use binary multiplexing and escape only
ESC and the escape byte. This removes JSON and the inner Base64 encoding while
preserving a local SSH listener, concurrent channels, half-closes, bounded
buffering, timeouts and clean shutdown.

Build and usage are described in [the native prototype README](../rust-ssh/README.md).

## Results

Linux x86-64 (Xeon Platinum 8573C), Python 3.12.14 and optimized Rust 1.99.1.
The Python baseline is Guac Bridge 0.9.0. Five alternating rounds transfer
16 MiB at roughly 4 MiB/s in each direction after a full warmup transfer.
Efficiency ratios are medians of paired runs; absolute CPU columns are
separate medians. Higher efficiency means less CPU for identical work.

| Direction | Python client CPU | Native client CPU | Native CPU efficiency | Paired range |
|---|---:|---:|---:|---:|
| Upload | 702 ms | 187 ms | 3.52× | 3.20–4.75× |
| Download | 747 ms | 211 ms | 3.78× | 3.08–4.00× |

**The native implementation clears the 1.5× target:** about 72–74% less
client CPU in the paced tests. Combined client/helper lifetime CPU efficiency
is 3.29× for upload and 4.03× for download, including startup, warmup and exit.
Guacamole instruction bytes fall by 25.1% and 24.7%, respectively.
These gains combine Rust, binary framing and transport tuning; they do not
isolate language choice alone.

The conservative native profile still gives 3.22× upload and 3.88× download
client CPU efficiency with the Python frame/window/blob sizes.

Observed client peak RSS is roughly 9 MiB versus 30 MiB for Python; helper
RSS is roughly 9 MiB versus 15 MiB. These are lifetime high-water counters
under a fork/exec launcher, which can overestimate small native processes.

### Bulk throughput

Median MiB/s from three alternating rounds, 8 MiB transfers at saturation:

| Direction | Simulated RTT | Python | Native conservative sizes | Native defaults |
|---|---:|---:|---:|---:|
| Upload | 0 ms | 8.02 | 21.47 | 14.49 |
| Upload | 20 ms | 2.76 | 3.67 | 13.22 |
| Download | 0 ms | 31.81 | 40.76 | 43.86 |
| Download | 20 ms | 19.80 | 20.53 | 40.11 |

At 20 ms simulated RTT, native defaults deliver roughly **4.8× upload** and
**2.0× download throughput**. At zero delay, conservative input sizes win
upload in this fixture. Larger blobs/windows are not universally faster;
PTY scheduling and the simulator affect these results.

### Interactive latency and idle

Three rounds of 50 sequential one-byte echoes, after 20 warmup echoes.
Median per-run p95 latency:

| Simulated RTT | Python | Native defaults |
|---|---:|---:|
| 0 ms | 0.86 ms | 0.40 ms |
| 20 ms | 22.94 ms | 21.73 ms |

These checks run without competing bulk transfers; they do not establish
latency under saturation. Larger queued frames can delay another channel.
All transfer hashes, byte comparisons, echo contents and shutdown checks pass.

The single 10-second idle control used 0.9 ms CPU with Python and 2.3 ms
with native code. Idle CPU did not improve; the native deadline timer wakes
once per second. Absolute CPU cost is tiny. No watts or joules were measured.

Windows compilation/unit tests and Linux integration tests pass in
[CI](https://github.com/himbeles/guac-bridge/actions/runs/41310978731), including
the static musl helper. Performance measurements above are local Linux
simulation, not measurements of a deployed corporate gateway or Windows power.

Raw results: [paced CPU](../benchmarks/results/rust-ssh-paced.json),
[saturation](../benchmarks/results/rust-ssh-saturation.json),
[latency](../benchmarks/results/rust-ssh-latency.json),
[idle](../benchmarks/results/rust-ssh-idle.json).

## Reproduce

From a Linux checkout with Bash and PTY support, build optimized binaries and
run the commands sequentially:

```shell
cargo build --release --locked --manifest-path rust-ssh/Cargo.toml
cargo test --release --locked --manifest-path rust-ssh/Cargo.toml
uv run python -m unittest discover -s tests -p test_rust_ssh.py -v
uv run python -m benchmarks.rust_ssh --mib 16 --seconds 4 --repeats 5 --output benchmarks/results/rust-ssh-paced.json
uv run python -m benchmarks.rust_ssh --mib 8 --seconds 0 --repeats 3 --rtts 0,20 --output benchmarks/results/rust-ssh-saturation.json
uv run python -m benchmarks.rust_ssh --directions latency --mib 1 --seconds 0 --repeats 3 --rtts 0,20 --output benchmarks/results/rust-ssh-latency.json
uv run python -m benchmarks.rust_ssh --directions idle --variants python,rust --mib 1 --seconds 10 --repeats 1 --output benchmarks/results/rust-ssh-idle.json
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
