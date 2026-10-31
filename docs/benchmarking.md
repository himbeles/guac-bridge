# Benchmarking Guac Bridge

Both bridges use up to 32 Guacamole input blobs and 32 data credits in flight,
with 16 KiB SSH frames. LLM response chunks remain 4 KiB for streaming.
This improves bulk transport utilization; it does not accelerate model inference.
Use matching client and helper versions for meaningful benchmark comparisons;
version mismatches warn by default unless strict checking is requested.

## Measure an existing SSH bridge

Use an SSH alias that already connects through `guac-ssh-client`, including its
`--ensure-running` hook if configured. Start the bridge before timing it. From
a repository checkout, run:

```shell
uv run python -m benchmarks.ssh_probe YOUR_SSH_ALIAS --mib 1
```

The cross-platform probe downloads and uploads synthetic bytes, prints elapsed
time and MiB/s, and checks byte counts. It uses OpenSSH for authentication and
requires `head`, `/dev/zero`, and `wc` on the server. It creates no server files.
Inner SSH compression is disabled to keep synthetic data from distorting the
transfer rate. Increase `--mib` for longer transfers once the small test works.

Each direction includes a new SSH session's authentication and setup. Repeat
the probe at least three times and compare medians, with the bridge already
running. Larger transfers reduce the effect of session setup. Keep the same
client, server, transport, and concurrency when comparing results.

## Measure the LLM bridge

Compare the server-local API with its client-side bridge endpoint. On the
server, a lightweight HTTP timing check is:

```shell
curl -s -o /dev/null -w 'first_http_byte=%{time_starttransfer}s total=%{time_total}s\n' http://127.0.0.1:8000/v1/models
```

On a Windows client, through the LLM bridge:

```powershell
curl.exe -s -o NUL -w "first_http_byte=%{time_starttransfer}s total=%{time_total}s\n" http://127.0.0.1:8000/v1/models
```

Use the actual endpoint ports and normal API authentication when required.
This checks HTTP overhead, not model generation. First HTTP byte may be response
headers and is not necessarily the first generated token.

For generation tests, use the same model, prompt, output limit, streaming mode,
and concurrency on both paths. Warm the model first, record first-content-token
latency and subsequent token delivery, and repeat. Separate GPU prefill and
generation time from bridge overhead; cached prompts and other model users can
otherwise dominate the comparison.

## Controlled local comparison

The repository also includes a simulator using a real Bash PTY and real server
helpers. It needs Linux with Bash and PTY support, and contacts no real Guacamole
host. From a checkout:

```shell
uv run python -m benchmarks.transport --output benchmarks/results/local.json
uv run python -m benchmarks.summarize benchmarks/results/local.json
```

This runs three repeats of SSH uploads/downloads, LLM prompt/response transfers,
and a fixed-rate SSE stream, using WebSocket and HTTP tunnels. It checks payload
integrity and measures wall time, CPU, acknowledgement waits, and stream delivery
lag. Default bulk source size is 256 KiB.

- `baseline` uses the pre-0.5 per-event ACK drain policy in a benchmark subclass.
- `pipeline` uses the actual installed/current production client.
- Both use the current lifecycle/error handling and current server helpers;
  this isolates the send policy rather than running two complete old/new releases.
- Added delays of 0/20/50 ms affect only STDIN acknowledgements. They are not a
  full WAN RTT, loss, or bandwidth simulation.
- Startup and cleanup are excluded from transfer timing. CPU in the parent
  includes the simulated gateway; unavailable helper CPU is reported as null.

A shorter check:

```shell
uv run python -m benchmarks.transport --kib 64 --repeats 1 --delays 0,20 --transports websocket --workloads ssh-upload,ssh-download,llm-paced --output benchmarks/results/smoke.json
```

To collect a local CPU profile (profiling affects timings):

```shell
uv run python -m benchmarks.transport --kib 4096 --repeats 1 --delays 0 --transports websocket --workloads ssh-upload,ssh-download,llm-upload,llm-download --profile /tmp/guac-performance.pstats --output benchmarks/results/profile.json
uv run python -m pstats /tmp/guac-performance.pstats
```

## Interpreting results

| Observation | What to investigate |
| --- | --- |
| Low CPU and strong sensitivity to ACK delay | Network/Guacamole turnaround and flow control |
| High CPU in the client or helper | Profile that process for framing, serialization, scheduling, and copies |
| WebSocket outperforms HTTP fallback | Per-instruction HTTP request overhead |
| Server-local LLM generation is equally slow | Model/GPU workload rather than bridge transport |
| A high bulk rate but delayed token delivery | Buffering and stream delivery latency |

For live testing, record the chosen Guacamole transport, client/helper versions,
transfer size, active connections, client/helper CPU, and guacd/proxy load when
available. The simulator does not include real guacd scheduling, proxies, TLS,
SSH encryption, or your network, so its absolute rates are not deployment
guarantees. See the [original investigation](performance-investigation.md) for
the historical measurements and limitations.

## Measure flow control with simulated RTT

```shell
uv run python -m benchmarks.flow --output benchmarks/results/flow.json
uv run python -m benchmarks.flow --kib 2048 --repeats 3 --rtts 20,50 --output benchmarks/results/flow-large.json
```

This measures the production defaults. The proxy delays application bytes
in both directions by half the selected RTT. Ping and bulk-echo controls validate
the model; it does not model TCP congestion, loss, TLS or real guacd scheduling.
Use `--transport http` to check HTTP fallback, or `--workloads llm-paced` for
stream delivery lag.

The [v0.6.0 results](../benchmarks/results/release-0.6.0-flow.json) are historical
comparisons against smaller limits. The current benchmark has no compatibility
profile. See [the original investigation](https://github.com/himbeles/guac-bridge/blob/investigate/transport-windows/docs/performance-next.md)
for the screening measurements and tradeoffs.

## Protocol encoding comparison

See [the protocol v4 comparison](protocol-v4-results.md) for paired results.
The v4 codec removes whole-event Base64. Payload Base64 and Guacamole blob
Base64 remain. Compare the same transfer sizes, windows and frame sizes; wire
counters exclude TLS/TCP/SSH overhead. Startup bootstrap traffic is outside the
measurement. Historical checked-in results retain their original wire formats.

See [the orjson efficiency measurement plan](orjson-efficiency-plan.md) for a
separate CPU/energy experiment. No alternative JSON library is used by v0.9.0.
