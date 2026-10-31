# Plan: measure orjson CPU and energy efficiency

This is a measurement plan, not an implemented dependency change or evidence of
power savings. Keep the protocol, window sizes and SSH frame sizes identical.

## Preserve the wire contract

Protocol v4 requires ASCII JSON. `orjson` emits UTF-8 and has no `ensure_ascii`
equivalent. Compare a conforming adapter: use its output only when it passes the
same byte checks; otherwise use the standard encoder. Include validation and
fallback costs. Decoding must retain the same strict ASCII/object checks.
Exercise Unicode and terminal-control metadata as well as ordinary ASCII frames.

## Small experiment

1. **Isolated codec:** small credit messages and 4/16/32 KiB Base64 data bodies.
   Prebuild inputs, warm both implementations, then run five paired measurements
   in alternating order. Measure encoding, decoding and complete round trips.
2. **Equal-rate live test:** SSH upload, SSH download and fixed-rate LLM SSE.
   Use 60-second runs at a rate both implementations sustain. Start with only
   the client changed, keeping the helper and actual Guacamole path unchanged.
3. **Saturation test:** only if equal-rate savings look substantial. Report
   throughput separately; higher throughput answers a different question.

Use identical work, concurrency and transfer sizes. Check byte integrity and
p95 SSE delivery lag so savings cannot come from dropping data or buffering
responses longer. Record an idle baseline separately.

## Metrics and decision

| Metric | Calculation |
|---|---|
| CPU work | Change in process user time plus system time |
| Normalized bulk cost | CPU seconds per GiB delivered |
| Normalized SSE cost | CPU milliseconds per 1,000 delivered events |
| CPU load | `100 × CPU seconds / elapsed seconds`; 100% is one full core |
| CPU efficiency gain | Baseline CPU time divided by candidate CPU time for identical work |

Use process CPU-time counters rather than occasional Task Manager samples.
Require at least **1.5× CPU efficiency**, i.e. at least 33% less CPU time for the
same work, to justify further optimization. Compare paired medians and variation.

The current simulator's `parent_cpu_s` includes client, gateway and target work.
Before attributing savings to the client, run it in a separate worker process
and collect its own CPU counters. Report helper CPU separately; unavailable
measurements remain null.

## Actual energy

CPU time is a proxy, not watts or battery consumption. For energy measurements,
repeat equal-work/equal-rate runs on physical hardware using supported Linux
CPU-package energy counters or a whole-machine power meter. Keep charging state,
power profile, background work and temperature comparable. Account for energy
counter wraparound; package energy does not cover the whole machine.

References: [orjson](https://github.com/ijl/orjson),
[process CPU counters](https://psutil.readthedocs.io/en/latest/#psutil.Process.cpu_times),
[Linux powercap](https://docs.kernel.org/power/powercap/powercap.html).
