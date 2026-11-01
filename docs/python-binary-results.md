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
