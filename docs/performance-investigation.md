# Transport performance investigation

Historical baseline: `0d547f8131aedf5b938871376fba749f08c32e53`.
Original investigation: `investigate/transport-performance`, commit
`43c7978b6229243e86fa6b07d45a38b4c927b9a8`.

Version 0.5.0 subsequently promoted bounded input pipelining into both clients,
with ACK deadlines, shutdown/cancellation handling, and matching helper version
checks. The measurements and prototype discussion below record the original
investigation, not a new measurement of the 0.5.0 release. Current reproduction
and live-testing instructions are in [Benchmarking](benchmarking.md).

## Main finding

The largest demonstrated bottleneck on a latency-bearing path is **per-event
waiting for Guacamole STDIN acknowledgements**, rather than the raw speed of
aiohttp WebSockets. There are also meaningful encoding and per-frame CPU costs.
This identifies bottlenecks in our implementation; it does **not** establish the
total throughput limit of a particular corporate Guacamole deployment.

In the current `Bridge.send_input()`, the eight-blob sliding window is local to
one call. Before returning, that call drains every remaining acknowledgement.
Each SSH data frame and each return credit is a separate call, so a small event
effectively waits for a full acknowledgement turnaround before the next event.
`SSHBridge.send_event()` also creates and shields a task for every event to protect
shared framing from cancellation.

This affects both directions. Downloads require return credits, and the local
SSH downloader and LLM proxy await `send_event(credit)` before forwarding more
already-received data. The remote helper has eight data credits, but those credits
do not eliminate the per-event drain of the separate Guacamole acknowledgement
window. Large LLM prompts already use the sliding window within one big event,
so they gain much less from sharing the window across events.

## Measurements

Local Linux x86-64; CPython 3.12.14; aiohttp 3.14.4 with its compiled WebSocket
reader. The managed environment reports an eight-CPU-equivalent quota. All
numbers below are observations from this environment, not expected Windows or
production-server rates.

The matrix contains 180 integrity-checked trials: two transports, two sender
variants, five workloads, three acknowledgement delays, and three repeats.
Bulk tests use a 256 KiB synthetic source. LLM prompt upload sends approximately
341 KiB of JSON derived from that source; reported throughput uses actual JSON
body bytes. The paced stream produces 64 small SSE events at 10 ms intervals.
Startup and cleanup are excluded from trial timing. Variant order alternates
between repeats.

The gateway is a simulator adapted from the integration tests. It uses a real
Bash PTY and the real unmodified server helper. The artificial delay applies
only to STDIN blob acknowledgements, independently and without serializing their
timers. **20 ms ACK delay is not a claim that this models a complete 20 ms WAN
RTT.** It isolates dependence on ACK turnaround. The mock does not implement
Apache guacd, its output framing/ACK behavior, terminal frame scheduling, SSH
encryption, TLS, proxies, bandwidth caps, or loss.

### Bulk throughput

Median WebSocket throughput, in MiB/s. `Pipeline` is the experimental sender
described below; the helpers and wire format are identical.

| Workload | Added ACK delay | Current | Pipeline | Ratio |
| --- | ---: | ---: | ---: | ---: |
| SSH upload | 0 ms | 6.243 | 9.386 | 1.50x |
| SSH download | 0 ms | 8.805 | 9.373 | 1.06x |
| LLM prompt upload | 0 ms | 8.331 | 12.322 | 1.48x |
| LLM response download | 0 ms | 6.614 | 8.273 | 1.25x |
| SSH upload | 20 ms | 0.173 | 0.487 | 2.81x |
| SSH download | 20 ms | 0.178 | 1.450 | 8.16x |
| LLM prompt upload | 20 ms | 0.577 | 0.614 | 1.06x |
| LLM response download | 20 ms | 0.122 | 0.983 | 8.07x |
| SSH upload | 50 ms | 0.072 | 0.203 | 2.80x |
| SSH download | 50 ms | 0.074 | 0.605 | 8.22x |
| LLM prompt upload | 50 ms | 0.239 | 0.257 | 1.07x |
| LLM response download | 50 ms | 0.050 | 0.405 | 8.09x |

At 20 ms added ACK delay, baseline bulk trials spend about 98–99% of their wall
time inside acknowledgement waits. Parent-process CPU consumes about 4–7% of
one core. This parent includes the simulated gateway as well as the local
client, so the figure is not client-only CPU. Helper CPU is unavailable in this
managed runtime because process-tree `/proc` entries are hidden; null readings
in the results mean unavailable, not zero CPU usage.

The SSH result is consistent with a roughly 4 KiB-per-turnaround limit in the
current sender. The prototype's upstream gain is smaller than its downstream
gain because a 4 KiB SSH data event consumes three 3072-byte input blobs; a small
credit event consumes just one. The prototype still has a bounded window and
therefore still becomes latency-limited. It does not make the network free.

### Stream delivery latency

Median of each trial's p95 event delivery lag, measured using a producer timestamp
in every SSE event. There is no model inference in this experiment.

| Added STDIN ACK delay | Current p95 lag | Pipeline p95 lag |
| --- | ---: | ---: |
| 0 ms | 0.79 ms | 0.87 ms |
| 20 ms | 643.74 ms | 0.85 ms |
| 50 ms | 2461.97 ms | 1.01 ms |

The baseline accumulates a backlog when it waits for a credit's acknowledgement
after each small response event. Pipelining removes that **ACK-induced** backlog
in this simulator. The sub-millisecond figures do not predict real deployment
latency: the experiment deliberately adds no delay to the output data path.
This change cannot accelerate model inference or GPU prefill.

### WebSocket, HTTP, and CPU controls

Three local 8 MiB download controls have median rates of approximately **957
MiB/s for direct TCP** and **447 MiB/s for binary aiohttp WebSocket**. The
WebSocket control has 4 KiB messages, no Guacamole framing, no PTY, no TLS, no
authentication, no return credits, and no artificial delay. These controls show
that the local WebSocket implementation has considerable headroom; they do not
measure a real Guacamole web server or its network path.

With no artificial delay, the HTTP fallback is slower: baseline SSH upload is
1.704 MiB/s versus 6.243 for WebSocket; LLM response download is 2.109 versus
6.614. The fallback makes a separate HTTP POST for each instruction, including
ACKs. At 20/50 ms injected ACK delay, the drain pattern dominates both transports,
and switching to WebSocket alone does not remove it.

Microbenchmarks, expressed as logical input MiB/s:

| Operation | Local rate |
| --- | ---: |
| One Base64 encode/decode round trip | 373 |
| JSON encode/decode of a 4 KiB data event | 200 |
| Guacamole parser, complete instructions per feed | 975 |
| Full three-layer codec encode/decode | 51 |

These are isolated CPU controls, not end-to-end rates. The full codec validates
payload equality. The parser control does not exercise arbitrary network
fragmentation. Base64 already uses CPython's native `binascii` implementation;
changing JSON libraries cannot remove protocol turnaround waits.

A separate cProfile run over 4 MiB, zero-added-delay transfers plus controls
records 11.1 million calls in 8.83 seconds. Selected self times include parser
`feed` 0.413 s, Base64 decode/encode 0.334/0.265 s, JSON encode/decode
0.220/0.124 s, and PTY `write` 0.317 s. Event-loop scheduling and numerous small
socket writes also contribute. This profile includes the simulated gateway,
controls, startup, and both variants; its timings are instrumented wall times,
not a clean production CPU-share attribution. No single serializer accounts for
the entire zero-delay limit. See the raw profile summary below.

Other structural costs worth measuring next are the SSH helper's thread-to-
event-loop handoff for every input line and the helpers' per-event stdout flush.
Their CPU shares were not measured here. A large LLM request also holds the
shared input lock for its entire frame, delaying credits for other requests.
Improving that fairness would require chunked request framing; dropping the lock
would corrupt interleaved frames.

## Encoding overhead

A 4096-byte SSH payload becomes:

| Stage | Bytes |
| --- | ---: |
| Original data | 4096 |
| JSON event containing a Base64 body | 5529 |
| Base64 event plus terminal newline | 7373 |
| Guacamole Base64 blobs and instruction syntax | 9883 |

That is **2.413x** the payload before WebSocket/HTTP, TCP/IP, TLS, and SSH framing.
There are three Base64 layers: binary body, terminal event, and Guacamole blob.
The outer event encoding could be replaced by ASCII JSON lines in a negotiated
new protocol, retaining the Base64 body and required Guacamole blob encoding.
The byte expansion calculation predicts approximately 25% fewer instruction
bytes for this frame. This alternative has not been implemented or benchmarked;
it needs explicit compatibility negotiation and malformed-frame tests.

## What is implemented on this branch

Only an opt-in **benchmark subclass** in
[`benchmarks/pipeline.py`](../benchmarks/pipeline.py). It keeps at most eight
unacknowledged input blobs across event boundaries, retaining the same 3072-byte
blob size, event encoding, and helper flow control. It observes late rejected
ACKs, marks interrupted partial frames as failed, keeps frames atomic under
concurrent callers, and drains input before shutdown.

Five safety tests cover the bound, concurrent framing, cancellation, late ACK
errors, and shutdown drain. All 68 tests pass locally except three environment-
specific skips (native Windows vault, Windows visible console, real OpenSSH).
The branch CI runs the suite and integrity-checked benchmark smoke tests on
Python 3.11 and 3.14. There are no timing thresholds, no new runtime dependencies,
and no production CLI changes or publishing trigger for this branch.

This is a proof of concept, not a merged optimization. Before promotion, it
needs live Guacamole validation, broader shutdown/disconnect testing under
delayed/failed ACKs, sustained concurrency/fairness tests, and validation on
Windows clients. Synthetic cleanup drains ACKs before closing the terminal to
avoid an ACK-only-delay artifact where an end marker overtakes a shutdown ACK.

## Suggested priorities

| Priority | Change or measurement | Reason / limitation |
| --- | --- | --- |
| 1 | Validate a bounded cross-event ACK window against real Guacamole | Largest measured benefit; same wire format |
| 2 | Measure the remaining live path | Guacd/proxy/network/frame scheduling can then be the limiting stage |
| 3 | Negotiate removal of the redundant outer Base64 layer | About 25% fewer instruction bytes; protocol compatibility work required |
| 4 | Consider larger negotiated windows or frame/batch sizes | More throughput per turnaround, with larger buffers and fairness risks |
| 5 | Profile and reduce tasks, copies, and flushes where still material | Zero-delay CPU work is spread across framing, codecs, PTY, and asyncio |
| 6 | Compare serializers or event loops only after the above | A faster JSON library does not fix ACK waiting; no alternative library was benchmarked |

Do not remove backpressure or indiscriminately buffer SSE to increase a bulk
number: bounded memory and timely token delivery remain requirements. Do not
change Guacamole's output autoflush without measuring token latency. Apache's
terminal implementation flushes output pipes through its terminal frame loop;
the documented frame budget/idle wait are 40/10 ms in version 1.6.0. The simulator
does not implement that loop. The deployed version and its settings are unknown.

## Reproduce and measure the real deployment

See [`benchmarks/README.md`](../benchmarks/README.md) for commands. Main results:

- [180-trial matrix and local controls](../benchmarks/results/local-matrix.json)
- [Instrumented 4 MiB profile trials](../benchmarks/results/profile-run.json)
- [cProfile top functions](../benchmarks/results/profile-top.txt)
- [Reproducible benchmark and integrity checks](../benchmarks/transport.py)
- [Existing-SSH-alias probe](../benchmarks/ssh_probe.py)

The cross-platform alias probe uses the existing bridge and OpenSSH configuration
to send/discard a known amount of synthetic data. It does not install anything on
the server or collect credentials. It has not been run against the user's hosts.
To identify the remaining live bottleneck, combine its throughput with client,
helper, and guacd CPU measurements, and compare the API's server-local latency
against its forwarded latency. Low CPU plus scaling with ACK turnaround points
to waiting; sustained high CPU identifies the process to profile next.

Primary references:

- [Guacamole protocol streams, blobs, and acknowledgements](https://guacamole.apache.org/doc/1.6.0/gug/protocol-reference.html)
- [Apache terminal autoflush and frame constants](https://guacamole.apache.org/doc/1.6.0/libguac-terminal/terminal_8h.html)
- [Apache terminal source, version 1.6.0](https://github.com/apache/guacamole-server/blob/1.6.0/src/terminal/terminal.c)
- [aiohttp WebSocket client options](https://docs.aiohttp.org/en/v3.14.3/client_reference.html)
- [CPython native binary codecs](https://docs.python.org/3.12/library/binascii.html)
