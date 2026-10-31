# Transport performance experiments

See [the initial findings](../docs/performance-investigation.md) and
[larger-window/frame experiments](../docs/performance-next.md).
The production clients use the bounded pipeline by default. The benchmark also
retains the legacy per-event drain policy for comparison. No extra dependencies
are required. See [Benchmarking](../docs/benchmarking.md) for deployment tests.

From a checkout on Linux with Bash and PTY support:

```shell
uv run python -m benchmarks.transport --output benchmarks/results/local.json
uv run python -m benchmarks.summarize benchmarks/results/local.json
```

The default matrix uses 256 KiB payloads, three repeats, both WebSocket and HTTP
tunnels, five workloads, and 0/20/50 ms of synthetic STDIN acknowledgement delay.
It normally takes several minutes. The delay is **not** a full network RTT
simulation. No real Guacamole host, model, password, or key is used.

A short integrity and latency check:

```shell
uv run python -m benchmarks.transport --kib 64 --repeats 1 --delays 0,20 --transports websocket --workloads ssh-upload,ssh-download,llm-paced --output benchmarks/results/smoke.json
```

For cProfile (timings will be affected):

```shell
uv run python -m benchmarks.transport --kib 4096 --repeats 1 --delays 0 --transports websocket --workloads ssh-upload,ssh-download,llm-upload,llm-download --profile /tmp/guac-performance.pstats --output benchmarks/results/profile.json
uv run python -m pstats /tmp/guac-performance.pstats
```

The `baseline` selects the legacy per-event ACK drain policy. `pipeline` uses
the current production bridge. Both use the current lifecycle/error handling
and server helpers, isolating the send policy rather than comparing entire
old/new releases. Payload byte counts/hashes and streamed-event counts are
checked during every trial. Helper startup and teardown are outside timed trials.
The fixed-rate stream checks delivery latency separately from model inference.

Parent CPU includes the client **and** simulated gateway in one process. Helper
CPU is collected from Linux `/proc` only when available; `null` means unavailable.
Wire counters cover Guacamole instruction payloads, excluding WebSocket framing,
HTTP headers, TCP/IP, TLS, and the underlying SSH transport. The HTTP fixture's
response fragmentation is intentional, not a claim about a real gateway.

The checked-in results are local observations, not performance guarantees. CI
only checks integrity, completion, and pipeline safety; it has no speed thresholds.

## Measure an existing deployment

On Windows, macOS, or Linux, use an existing SSH alias through the normal bridge:

```shell
uv run python -m benchmarks.ssh_probe perf --mib 1
```

The probe sends and discards synthetic bytes using `head`, `/dev/zero`, and `wc`
on the server. It writes no server files, disables inner SSH compression, and
keeps authentication with your OpenSSH client. Results include SSH session setup
and any bridge startup, so start the bridge first and repeat the probe when
comparing sustained throughput. It has not been run against your network.

For context, record the selected Guacamole transport, client/helper CPU use, and
Guacamole/guacd CPU load during the transfer. A real model/API latency test is
separate: first compare its server-local performance with the forwarded endpoint.

## Larger windows and frames

On `investigate/transport-windows`, compare the current defaults with isolated
window, SSH frame, and write-batching experiments:

```shell
uv run python -m benchmarks.windows --output benchmarks/results/windows.json
uv run python -m benchmarks.windows --kib 2048 --repeats 3 --rtts 20,50 --profiles current,windows32,windows32-chunk16k --output benchmarks/results/windows-confirm.json
```

The first command runs 216 integrity-checked trials (512 KiB, three repeats).
This model delays application bytes in **both directions**, unlike the original
ACK-only experiment. It includes a ping and bulk-echo check of the delay proxy.
Experimental settings are injected into both local simulator endpoints; they
are not production CLI options or automatically negotiated capabilities. Do not
use these overrides against an existing server helper.
