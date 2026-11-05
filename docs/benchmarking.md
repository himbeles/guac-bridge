# Benchmarking

Build release binaries first; debug builds are not representative. On Linux, install the test-only `aiohttp` dependency and run:

```shell
cargo xtask build
python scripts/benchmark.py --mib 32 --repeats 3 --output results.json
```

The benchmark starts the real native client, a simulated Guacamole gateway with a real Bash PTY, the uploaded static helper, and a TCP echo target. It verifies every payload by SHA-256 and reports client CPU seconds, peak RSS and throughput in each direction. Provisioning and startup are outside the measured interval. The supervisor and gateway are separate processes from the Rust client, but share the machine; results are not a measurement of production guacd capacity or electrical energy.

Compare the same payload, concurrency, machine and transport settings. Use fixed-rate production workloads when comparing CPU efficiency: saturation throughput alone changes how much work each version performs. Record the client binary digest, CPU time per transferred MiB, wall time, and several repeated runs. RSS is a process-lifetime peak, not an interval delta. Client CPU excludes the helper, gateway, SSH and API server; use process counters on the server for their costs.

The most useful tuning controls are:

| Option | Default | Effect |
| --- | --- | --- |
| `--frame-size` | 65536 bytes | Maximum bytes per TCP frame. Larger frames reduce overhead; partial reads still send immediately. |
| `--window` | 4 frames | TCP data credits per connection. Increasing this does not change the Guacamole input window. |
| `--input-window` | 8 blobs | Global per-bridge limit on Guacamole stdin blobs awaiting gateway ACKs; independent of TCP credits. |

The conservative profile is `--window 4 --frame-size 65536 --input-window 8`. Increase one limit at a time: for example compare `--window 4` and `--window 8` with `--input-window 8` held fixed. Then vary `--input-window` separately if needed. Allowed production frame sizes are 4096, 8192, 16384, 32768 and 65536. Live input blobs remain bounded at 6048 bytes. Eight outstanding full blobs represent up to 48384 wire bytes awaiting gateway ACKs, shared by all channels. These ACKs confirm gateway acceptance, not remote-helper consumption; the limit is mitigation for burst-sensitive paths, not end-to-end loss recovery.

For the real path, run your usual SSH transfer or streamed API request through a foreground bridge (`--foreground`). Measure total bytes and elapsed time, plus client process CPU, with the same server workload before and after. A proxy, guacd, PTY, remote SSH service or upstream application can set a throughput ceiling even if client CPU falls. Measure idle and interactive use separately from bulk transfers. Lower CPU time is useful evidence of reduced processing; it does not by itself establish watts or battery savings.

The benchmark script accepts all three controls and records them in every trial:

```sh
python scripts/benchmark.py --window 4 --frame-size 65536 --input-window 8 \
  --mib 32 --repeats 3 --output conservative-results.json
```

Changing the gateway window also bounds stdin traffic during bootstrap. The Linux
helper-upload block size and its remote acknowledgements remain independently
controlled by `--upload-batch-size`; their defaults and encoding are unchanged.

## FortiGate and Windows console measurements

Run the local FortiGate/hterm simulator separately from the Guacamole benchmark:

```sh
python scripts/benchmark_fortigate.py --direction download --mib 32 --repeats 3 --window 4 16
python scripts/benchmark_fortigate.py --direction download --mib 8 --repeats 1 --window 4 16 --rtt-ms 250
```

This requires Linux, release client/helper builds and `aiohttp`. It uses the real
helper's printable carrier but pipes instead of a Windows console. Each transfer
is checked by SHA-256. Output is JSON lines with throughput, wall time and separate
client/helper/simulator CPU times. The Python simulator shares the benchmark
process; its CPU can limit throughput, especially uploads. Added RTT is applied
in both directions with scheduled delivery, without a per-message bandwidth cap.
These numbers do not measure a deployed FortiGate's limits.

On 2026-11-05, local 32 MiB downloads reached 38.6–44.8 MiB/s across windows 4 and
16. At an injected 250 ms RTT, an 8 MiB download reached 0.90 MiB/s with window 4
and 3.48 MiB/s with window 16. Four full 64 KiB frames provide 256 KiB in flight,
so a high-latency connection can be credit-limited even with low CPU use.

The real ConPTY probe also reports console write time, encoding time, write/flush
counts and byte-verified throughput. For 4 MiB of mixed binary input on Windows
Server 2022 and the current Windows runner, respectively:

| Output path | Write calls / flushes | Server 2022 MiB/s | Current Windows MiB/s |
| --- | ---: | ---: | ---: |
| v5.0.0: separate stdout write/flush per OSC sequence | 69,906 / 69,906 | 0.67 | 0.69 |
| stdout: one complete data-envelope/close pair per write/flush | 34,953 / 34,953 | 1.35 | 1.35 |
| direct console handle: one complete pair per write/flush | 34,953 / 34,953 | 2.89 | 3.06 |

Encoding those 4 MiB took 10–13 ms; the current console writes took 5.79–6.00 s.
The carrier expanded 4,194,304 payload bytes to 6,536,139 wire bytes. All paired
variants preserved every payload byte in the ConPTY comparison. Production
forwarding is unchanged by these measurement tools. The paired/direct variants
need integration and affected-gateway verification before deployment.

[Windows measurement run](https://github.com/himbeles/guac-bridge/actions/runs/42073709197).
The prior large bulk stdout path measured 5.8–7.1 MiB/s in this console-only test,
but is not a replacement for bounded complete-envelope writes: its integrity on
ConPTY alone does not establish integrity through the affected FortiGate path.
