# Larger flow-control windows and SSH frames

Investigation branch: `investigate/transport-windows`, based on v0.5.2.
Production defaults are unchanged. The goal was to find improvements of at least
**1.5×**, rather than accumulate small optimizations.

## Findings

The strongest next candidate is increasing both the Guacamole input-ACK window
and the helper's data-credit window from 8 to 32. SSH additionally benefits from
16 KiB data frames instead of 4 KiB. Changing Python libraries is not justified
by these results: throughput on delayed paths is primarily constrained by how
much data the protocol allows in flight.

Median throughput in MiB/s for 2 MiB transfers, three repeats. Parentheses show
speedup over the current defaults in the same condition.

| RTT | Workload | Current | Windows 32 | Windows 32 + SSH 16 KiB |
|---|---|---:|---:|---:|
| 20 ms | ssh-upload | 0.461 | 1.806 (3.92×) | 2.135 (4.63×) |
| 20 ms | ssh-download | 1.326 | 5.166 (3.90×) | 15.941 (12.02×) |
| 20 ms | llm-download | 0.927 | 3.166 (3.42×) | 3.485 (3.76×) |
| 50 ms | ssh-upload | 0.197 | 0.762 (3.87×) | 0.910 (4.62×) |
| 50 ms | ssh-download | 0.585 | 2.219 (3.79×) | 7.252 (12.39×) |
| 50 ms | llm-download | 0.392 | 1.502 (3.83×) | 1.493 (3.81×) |


These are bulk-transfer results. Ordinary generated text may be too small or too
slowly produced to benefit; faster bulk forwarding does not mean faster model
inference, time to first token, or interactive SSH keystrokes.

## What was tested

The screening matrix has 216 trials: eight profiles, three workloads, 0/20/50 ms
application-byte RTT, three repeats, and 512 KiB random payloads. A second matrix
uses 2 MiB payloads, three repeats, and the current, larger-window, and combined
profiles at 20/50 ms. Each trial checks delivered payload integrity. Profile
order alternates between repeats; experiments run sequentially.

| Profile | Input ACKs in flight | Data credits | SSH frame | Batch input writes |
|---|---:|---:|---:|---|
| current | 8 | 8 | 4 KiB | no |
| input32 | 32 | 8 | 4 KiB | no |
| credit32 | 8 | 32 | 4 KiB | no |
| windows32 | 32 | 32 | 4 KiB | no |
| chunk16k | 8 | 8 | 16 KiB | no |
| windows32-chunk16k | 32 | 32 | 16 KiB | no |
| batch | 8 | 8 | 4 KiB | yes |
| batch-windows32-chunk16k | 32 | 32 | 16 KiB | yes |

The LLM helper continues reading 4 KiB chunks in every profile. Larger SSH frames
have no direct effect on the LLM workload.

Input-window-only and data-credit-only changes do not improve downloads by
1.5×: the other window remains the bottleneck. At 20/50 ms, increasing both
windows improves all three workloads substantially. Larger frames alone improve
SSH downloads about 3.4×, but SSH uploads only about 1.2×. Combining the two is
the strongest SSH download candidate.

Write batching fails the 1.5× threshold on the tested WebSocket path, both alone
and compared with the same enlarged windows/frames. It is not a priority. HTTP
fallback batching was not measured here. Removing just one redundant Base64
layer saves roughly 25% of encoded bytes (an ideal bandwidth-only gain near
1.33×), below the requested threshold; other codec changes remain untested.

## Tradeoffs before production

- Near-zero-delay results are mixed. Larger windows alone reduce SSH upload
  throughput from 9.78 to 6.34 MiB/s in the screening matrix. The combined profile
  reaches 8.32 MiB/s. A universal larger default needs investigation; an adaptive
  window or explicit negotiated performance mode is worth considering.
- Raw SSH data credits grow from 32 KiB to 512 KiB per direction per channel for
  the combined profile. Eight channels can therefore hold 4 MiB per direction,
  before encoded buffers and queues. The input-ACK budget grows from 24 to 96 KiB.
- Frame/window limits must be negotiated with the helper. Package-version
  warnings alone cannot establish support for larger frames. Preserve current
  limits for old helpers, and preserve bounded queues and cancellation behavior.
- Larger bursts can affect fairness and cancellation latency on slow links.
  Before changing defaults, check simultaneous channels, paced LLM output, real
  Guacamole/guacd buffering, and a Windows client on the actual deployment.

## Method and limits

The harness runs actual Python helpers through a real Bash PTY and a simulated
Guacamole WebSocket gateway. A TCP proxy gives application bytes independent
one-way delivery deadlines of RTT/2 in both directions. It preserves ordering
and bounded queues; it does not sleep serially per chunk. Ping and 512 KiB bulk
echo controls validate that it does not introduce an artificial per-chunk rate
limit. Configured 20/50 ms yielded approximately 20.4/50.6 ms ping RTT in the
screening run; bulk echo completed in approximately 22/53 ms.

This is not a full WAN emulator. It excludes real guacd scheduling, TLS, inner
SSH encryption, packet loss, TCP congestion behavior and bandwidth caps. Client
and simulated gateway share a process; parent CPU includes both. Helper process
CPU is unavailable in this environment and is recorded as null. Timings exclude
helper startup and teardown but include local channel opening. These observations
identify candidates, not guaranteed gains on a particular deployment.

## Reproduce

From this branch on a host with Bash and PTYs:

```shell
uv run python -m benchmarks.windows --output benchmarks/results/windows-matrix.json
uv run python -m benchmarks.windows --kib 2048 --repeats 3 --rtts 20,50 --profiles current,windows32,windows32-chunk16k --output benchmarks/results/windows-confirm.json
```

`benchmarks/windows.py` injects matching experimental settings into both local
endpoints. These are not supported deployment options. Raw results are in
[windows-matrix.json](../benchmarks/results/windows-matrix.json) and
[windows-confirm.json](../benchmarks/results/windows-confirm.json).
The result files include environment details, wall time, CPU and wire counters.
CI checks integrity and lifecycle, with no machine-dependent speed assertions.
