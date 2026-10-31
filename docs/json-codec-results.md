# JSON codec CPU experiment

Compare Python's JSON codec with `orjson` and `msgspec` on Guac Bridge 0.9.0.
The adapters are benchmark-only; package dependencies and production code are
unchanged. Results measure CPU work, not electrical power or battery life.

## Method

Linux x86-64, Intel Xeon Platinum 8573C, Python 3.12.14, orjson 3.13.0 and
msgspec 0.22.0. The source baseline is commit `1ddd2d8` (protocol v4).

- **Codec:** prebuilt credit messages, 4/16/32 KiB Base64 payloads and Unicode
  metadata. Five alternating rounds, 150 ms of process CPU per measurement;
  encoding, decoding and round trips measured separately. Inputs and warmup
  are outside timing.
- **Bridge:** a separate client worker uses the real bridge with only its codec
  replaced. A simulated WebSocket Guacamole gateway connects through a real
  Bash PTY to an unchanged helper. Gateway, target and helper CPU are excluded
  from the client's process CPU counter. Startup, warmup and teardown are excluded.
- **Equal work:** SSH transfers deliver 4 MiB over approximately four seconds;
  LLM streams deliver 400 events at 100 events/second. Five alternating rounds
  per workload and codec. Defaults remain window 32 and SSH frames 16 KiB.
  SSH bytes/hashes and SSE event order, text and count are checked each time.
  SSH workloads exercise the bridge's opaque TCP path; OpenSSH encryption and
  authentication are outside this test.

This is a short screening experiment rather than the earlier proposed
60-second energy study. Shared-host variation limits precision. CPU efficiency
is baseline CPU time divided by candidate CPU time for matching work;
1.5× means at least one-third less CPU. It is not a throughput speedup.

## Results

Codec-only round-trip gains (ratios of median CPU times):

| Event | orjson | msgspec |
|---|---:|---:|
| Credit | 1.70× | 1.72× |
| 4 KiB data | 2.13× | 1.98× |
| 16 KiB data | 2.42× | 1.96× |
| 32 KiB data | 2.19× | 2.32× |
| Unicode metadata | 1.13× | 1.12× |

Whole-client CPU efficiency, median of five paired ratios:

| Workload | Standard CPU, median | orjson | msgspec |
|---|---:|---:|---:|
| SSH upload | 277 ms | 1.13× | 1.08× |
| SSH download | 418 ms | 1.26× | 1.71× |
| LLM SSE | 198 ms | 0.97× | 1.12× |

**The apparent 1.71× download gain did not hold up in a larger check.**
Repeating standard JSON versus msgspec with 16 MiB over eight seconds,
five paired rounds, gave **1.08×** median CPU efficiency
(individual ratios 0.91–1.26×). Median client CPU was 1.053 s for the
standard codec and 0.939 s for msgspec. The original download baseline
varied from 256 to 560 ms across identical 4 MiB transfers.

Median per-run p95 SSE delivery lag was 3.1 ms (standard), 6.1 ms
(orjson) and 2.6 ms (msgspec). All integrity checks passed.
The four-second idle control consumed less than 1 ms of client CPU with
each codec.

**Recommendation: keep the standard codec.** Neither candidate demonstrated
a repeatable 1.5× whole-client CPU gain. Faster JSON routines help one
component; Base64, Guacamole parsing, I/O and scheduling still consume CPU.

## Framing and library costs

Neither candidate offers the standard encoder's `ensure_ascii=True` behavior.
The adapters retain ASCII JSON lines and fall back to the standard encoder for
Unicode, DEL, floats and other values outside a conservative fast subset.
Candidate output is checked with `isascii()` and a DEL search; JSON serializers
already escape C0 controls. Incoming records retain the complete printable-byte
check. Large numbers and candidate decoding errors use the standard decoder.
All validation and fallback costs are included in the measurements.

A first adapter scanned every encoded byte with `bytes.translate`; this hid
much of the encoder improvement. The reported runs use the faster output
check. These adapters cover tested protocol shapes, not every possible Python
object graph. The existing seven framing/fragmentation regressions pass with
each codec, as do 54 additional conformance checks across all three codecs.

## Reproduce

From a checkout on Linux with Bash and PTY support, run each command separately
so measurements do not compete for CPU:

```shell
uv run --with orjson==3.13.0 --with msgspec==0.22.0 python -m benchmarks.check_json_codecs
uv run --with orjson==3.13.0 --with msgspec==0.22.0 python -m benchmarks.json_codecs --output benchmarks/results/json-codecs.json
uv run --with orjson==3.13.0 --with msgspec==0.22.0 python -m benchmarks.json_bridge_cpu --seconds 4 --repeats 5 --output benchmarks/results/json-bridge-cpu.json
uv run --with orjson==3.13.0 --with msgspec==0.22.0 python -m benchmarks.json_bridge_cpu --workloads ssh-download --codecs stdlib,msgspec --mib 16 --seconds 8 --repeats 5 --output benchmarks/results/json-download-confirmation.json
uv run --with orjson==3.13.0 --with msgspec==0.22.0 python -m benchmarks.json_bridge_cpu --workloads ssh-idle --seconds 4 --repeats 1 --output benchmarks/results/json-idle-cpu.json
```

Raw measurements: [codec](../benchmarks/results/json-codecs.json),
[client transfers](../benchmarks/results/json-bridge-cpu.json),
[download confirmation](../benchmarks/results/json-download-confirmation.json),
[idle client](../benchmarks/results/json-idle-cpu.json).
Unavailable helper CPU counters remain null. These tests use local simulation,
not the deployed Windows client or a corporate gateway. No joules or watts
were measured; physical-device energy measurements remain a separate step.

Library references: [orjson](https://github.com/ijl/orjson),
[msgspec](https://jcristharif.com/msgspec/usage.html).
