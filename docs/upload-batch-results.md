# Helper upload batch comparison

Local unpublished change: use 24576 Base64 bytes per remote acknowledgement, retaining 1024-byte input blobs and SHA-256 verification. The test uses a real Bash PTY and a simulated Guacamole gateway; only remote upload acknowledgements have an injected 20 ms delay. This is not a real-network or corporate-guacd measurement.

Helper size: 1,371,088 bytes. Three fresh-cache runs per setting:

| Base64 batch | Remote acknowledgements | Median seconds | Run range, seconds |
|---|---:|---:|---:|
| 3 KiB | 596 | 12.813 | 12.718–12.858 |
| 12 KiB | 149 | 3.263 | 3.221–3.318 |
| 24 KiB | 75 | 1.682 | 1.666–1.689 |
| 48 KiB | 38 | 0.889 | 0.882–0.896 |

All 12 uploads reconstructed the helper exactly. An additional input-loss run for each setting acknowledged an input blob at the gateway but discarded it before the PTY; every setting detected the missing remote acknowledgement and failed in about one second with the test's one-second timeout. Default production timeout remains unchanged.

24 KiB is about 7.6 times faster in this simulation. It leaves less data outstanding than the fastest 48 KiB candidate; use the upload-only CLI option to tune or fall back to 3 KiB. No result here proves that larger bursts cannot be dropped by the user's actual gateway. Existing live SSH/HTTP framing and flow control are unchanged.

Raw measurements: `../upload-batch-results.json`. Reproduce with `python scripts/benchmark-upload.py --delay-ms 20 --repeats 3`.
