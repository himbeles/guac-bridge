# Python and Rust with identical binary transport

The Python experiment implements the native prototype's GR1 framing without
JSON or payload Base64. It uses the same 12-byte header, numeric channel IDs,
ESC/GS escaping, raw TCP bodies, credits and half-closes. Python escaping uses
`bytes.replace` and a compiled validation pattern, not a Python byte loop.

The Python client and helper are available as `guac-ssh-client-binary` and
`guac-ssh-server-binary` on this branch. They are experimental entry points;
the existing SSH commands keep their production protocol. Python's Guacamole
login, keychain, TLS and HTTP/WebSocket tunnel support are reused. This binary
client currently runs in the foreground; managed background startup and server
updates are not part of the experiment.

Both mixed-language pairs pass the same byte transfer: Python client/Rust helper
and Rust client/Python helper. Additional tests cover every byte, adjacent
escapes, partial/coalesced frames, malformed records, concurrent channels,
both half-close directions, one-byte interactive writes, target refusal,
Guacamole disconnect, stalled ACKs and Python HTTP fallback.

## Results

Python recovers the bandwidth and throughput gains without a Rust rewrite.
Rust retains a CPU and memory advantage with the same binary protocol.
These are local Linux/PTY simulations, not measurements of a corporate gateway
or electrical power. Python 3.12.14 and optimized Rust 1.99.1 were used.

### Throughput

Median MiB/s, three alternating 8 MiB saturation transfers at 20 ms simulated RTT:

| Direction | Production Python | Binary Python, tuned | Rust, identical binary settings |
|---|---:|---:|---:|
| Upload | 2.75 | 14.35 | 13.68 |
| Download | 19.74 | 36.85 | 36.69 |

The tuned Python pair reduces instruction bytes by about 25%, just as Rust
does. Both approach the same throughput limits in this fixture. Its scheduling
and flow limits can hide a client CPU difference.

### Client CPU

Five alternating rounds, 16 MiB per direction paced over four seconds.
Figures are median paired CPU-efficiency ratios for identical work:

| Direction | Python protocol-only vs original | Python binary+tuning vs original | Rust vs identical binary Python |
|---|---:|---:|---:|
| Upload | 1.32× | 1.58× | 2.67× |
| Download | 1.22× | 1.14× | 3.27× |

CPU gains depend on traffic shape. At saturation with 20 ms RTT, tuned
Python achieves 3.25× upload and 1.70× download CPU efficiency over the original
Python bridge. The remaining Rust advantage includes runtime, scheduling and
data-structure differences, not just the JSON/Base64 work.

Python process peak RSS remains about 30 MiB; Rust is about 9 MiB under the
benchmark launcher. The Python prototype reuses the production client imports.
These are process high-water counters, not minimum possible Python memory.

### Latency and idle

Median per-run p95 one-byte echo latency at 20 ms RTT is 21.82 ms (original
Python), 21.69 ms (binary Python), and 21.04 ms (Rust). These tests have no
competing bulk transfer. In one 10-second idle control, both Python profiles
used about 0.5 ms client CPU and Rust about 1.7 ms; these tiny counters cannot
establish an energy saving.

All 109 transfer/control trials passed integrity and clean-shutdown checks.
[CI](https://github.com/himbeles/guac-bridge/actions/runs/41333100178) passes
Python framing/entry-point checks on Windows and Linux, and mixed-language
PTY tests with GNU and static musl helpers.

Raw results: [paced CPU](../benchmarks/results/python-binary-paced.json),
[saturation](../benchmarks/results/python-binary-saturation.json),
[latency](../benchmarks/results/python-binary-latency.json),
[idle](../benchmarks/results/python-binary-idle.json).

This branch freezes the GR1 experiment. The production Python release ports
the binary design to both SSH and LLM using protocol v5 and the normal CLI
commands, with production version/update metadata retained.

## Comparison

The benchmark uses four fixed profiles rather than a tuning matrix:

| Profile | Framing | TCP frame maximum | Channel credits | Input blob | Input ACK window |
|---|---|---:|---:|---:|---:|
| `python` | Production JSON/Base64 | 16 KiB | 32 | 3072 B | 32 |
| `python-binary-matched` | GR1 binary | 16 KiB | 32 | 3072 B | 32 |
| `python-binary` | GR1 binary | 64 KiB | 32 | 6048 B | 64 |
| `rust` | GR1 binary | 64 KiB | 32 | 6048 B | 64 |

The first two isolate the Python protocol change with sizes held constant.
The last two compare languages with identical protocol and configured flow
limits. They still use different runtimes and data structures (asyncio versus
Tokio); any remaining difference is not an intrinsic language-only constant.

Client CPU uses a separate worker process. Gateway, target and measurement
supervisor CPU are excluded. Timed transfers exclude startup, warmup and shutdown.
Helper and combined lifetime counters include those phases and are reported
separately. Production Python has an extra helper preflight; binary prototypes
do not, so lifetime startup costs also differ. No electrical energy is measured.

## Reproduce

From a checkout of this branch on Linux with Bash/PTY support, build the Rust
programs once and run measurements sequentially:

```shell
cargo build --release --locked --manifest-path rust-ssh/Cargo.toml
uv run python -m unittest discover -s tests -p test_binary_framing.py -v
uv run python -m unittest discover -s tests -p test_python_binary_ssh.py -v
uv run python -m benchmarks.rust_ssh --variants python,python-binary-matched,python-binary,rust --mib 16 --seconds 4 --repeats 5 --output benchmarks/results/python-binary-paced.json
uv run python -m benchmarks.rust_ssh --variants python,python-binary-matched,python-binary,rust --mib 8 --seconds 0 --repeats 3 --rtts 0,20 --output benchmarks/results/python-binary-saturation.json
```

The real helpers run in Bash PTYs behind a simulated Guacamole WebSocket gateway.
The fixture enforces 8192-byte instruction and 6048-byte blob limits. Delay is an
application-byte RTT proxy, not a complete WAN model. Source settings are fixed;
all measured transfers compare payload bytes/hashes and verify clean shutdown.
