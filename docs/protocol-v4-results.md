# Protocol v4 encoding comparison

Compared v0.8.3 (whole-event Base64) with v0.9.0 (ASCII JSON lines), using the
production defaults: 32-credit windows and 16 KiB SSH frames. Three paired
process runs alternate variant order. Each transfers 2 MiB through the local
PTY/Guacamole simulator at 0/20/50 ms application-byte RTT. All 54 trials check
payload integrity. Startup and cleanup are excluded.

## Results

| Workload | RTT | v3 MiB/s | v4 MiB/s | Throughput ratio | Parent CPU efficiency | Fewer instruction bytes |
|---|---:|---:|---:|---:|---:|---:|
| ssh-upload | 0 ms | 7.250 | 8.005 | 1.10× | 1.27× | 24.93% |
| ssh-upload | 20 ms | 2.140 | 2.633 | 1.23× | 1.22× | 24.93% |
| ssh-upload | 50 ms | 0.906 | 1.115 | 1.23× | 1.21× | 24.93% |
| ssh-download | 0 ms | 15.383 | 18.727 | 1.22× | 1.20× | 24.99% |
| ssh-download | 20 ms | 13.342 | 15.780 | 1.18× | 1.23× | 25.00% |
| ssh-download | 50 ms | 7.229 | 7.329 | 1.01× | 1.17× | 24.98% |
| llm-download | 0 ms | 9.371 | 10.480 | 1.12× | 1.11× | 24.84% |
| llm-download | 20 ms | 3.358 | 3.127 | 0.93× | 1.12× | 24.84% |
| llm-download | 50 ms | 1.483 | 1.494 | 1.01× | 1.07× | 24.79% |

The consistent result is approximately **25% fewer Guacamole instruction bytes**.
Throughput and CPU gains are smaller and workload-dependent; the LLM 20 ms case
is about 7% slower in this run. These short simulator measurements do not prove
that difference is repeatable on a real deployment. They do not establish a
1.5× CPU-efficiency gain or quantify any power saving.

Parent CPU efficiency means old CPU seconds per MiB divided by new CPU seconds
per MiB. It includes the client, simulated gateway and synthetic target in one
process. Helper CPU is unavailable here and remains null. Counters exclude TLS,
TCP/IP and inner SSH overhead; the simulator omits actual guacd scheduling,
network congestion and loss. Actual energy consumption was not measured.

The safety changes also launch helpers with `exec`, ensuring an exited helper
does not leave an interactive shell consuming late JSON frames. The codec
escapes Unicode and controls and validates records with a native byte operation.
Bootstrap version checks/updates keep their separate control encoding.

[Raw paired results](../benchmarks/results/protocol-comparison.json).
See [Benchmarking](benchmarking.md) for reproduction on a deployment and
[the orjson efficiency plan](orjson-efficiency-plan.md) for the separate proposed
CPU/power experiment. No orjson substitution is included in these measurements.
