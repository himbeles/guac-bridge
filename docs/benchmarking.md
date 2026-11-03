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
