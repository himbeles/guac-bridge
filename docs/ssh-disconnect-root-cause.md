# SSH disconnect investigation

## Finding and confidence

An ignored nonblocking SSH write in Guacamole is a reproducible source of the exact
remote-helper decoder failure seen on the corporate connection. This is now a
verified code-level defect and a reproduced mechanism, rather than only a tuning
hypothesis. It is not yet direct observation of the installed corporate guacd:
that requires its exact build/source or instrumented write results.

The local experiment uses real libssh2 and an SSH server, not a mock of the write
return value. It compares one fixed synthetic wire stream under three conditions.
A TCP proxy briefly stops reading, creating actual socket backpressure; it never
edits or deliberately drops stream bytes. The damaged capture is then supplied to
the decoder probe used in the experiment, which reports the same `Wrong bridge protocol` decoder
failure as the reported crashes. The identical intended stream passes unchanged
through the same helper when delivery is correct.

## Defect in upstream source

In `src/protocols/ssh/ssh.c`, `ssh_input_thread()` reads up to 8192 bytes and calls:

```c
libssh2_channel_write(ssh_client->term_channel, buffer, bytes_read);
```

It ignores the return value, reads new input into the reused buffer, and continues.
The shared SSH session is configured nonblocking elsewhere in the same file.

The API returns the number of bytes accepted, or `LIBSSH2_ERROR_EAGAIN` when the
operation needs to be retried. Ignoring a short positive return discards the tail.
Ignoring EAGAIN advances to new input before the pending operation completes, in
violation of the API's requirement to retry with the same input arguments. Data
can consequently be lost or corrupted before reaching the destination helper.
The observed experiment exercised EAGAIN, not positive short writes.

Verified source locations:

- [Guacamole 1.5.5 SSH source](https://github.com/apache/guacamole-server/blob/1.5.5/src/protocols/ssh/ssh.c), blob `7c3ac05cd308a7e3ef571fa8cf74b937133a8310`.
- [Guacamole 1.6.0 SSH source](https://github.com/apache/guacamole-server/blob/1.6.0/src/protocols/ssh/ssh.c), blob `199064de6e47b623b087aaf8315b4261ce0713af`.
- Main checked during this investigation: blob `9e3bd806ffd53947028779db8934a7ad1dd7426a`.
- [libssh2 write contract](https://libssh2.org/libssh2_channel_write_ex.html).
- [libssh2 implementation and EAGAIN retry requirement](https://github.com/libssh2/libssh2/blob/libssh2-1.11.1/src/channel.c).

Guacamole's terminal-stream ACK acknowledges writing into its internal stdin pipe.
It is not acknowledgement that the separate SSH input thread forwarded every byte
to the remote process. The bridge cannot treat those gateway ACKs as end-to-end
TCP delivery acknowledgements.

## Effect of flow-control limits

`--window` limits TCP frame credits per channel. `--input-window` independently
limits Guacamole blobs awaiting gateway acknowledgements across the bridge.
At 6048 bytes per full blob, 64 outstanding blobs represent 387072 wire bytes;
eight represent 48384 bytes. Short blobs reduce those upper bounds.

Gateway ACKs can release slots before the helper consumes their bytes, so the
limit does not bound the entire end-to-end queue. A 65536-byte frame limit with
four TCP credits provides a separate 256 KiB per-channel payload budget. Vary one
limit at a time to isolate its effect. The experiment below changes byte bursts
while holding its decoder and synthetic data fixed.

## Repeated experiment

Source: libssh2 tag `libssh2-1.11.1`, commit
`a312b43325e3383c865a87bb1d26cb52e3292641`; its Git-source build reports
`1.11.1_DEV`. SSH peer: AsyncSSH 2.24.1. The proxy pauses consumption once for
200 ms; the producer pauses 250 ms between bursts. Socket send and proxy receive
buffers are set to 32768 bytes to make backpressure reproducible.

The stream consists of twelve correctly encoded Data frames, each with a 65536-byte
synthetic payload: 792668 wire bytes including headers and escaping. An 8192-byte
buffer is reused as in the upstream input worker. The retry control retains input
until writes complete. No production traffic or credentials are captured.

Three trials per condition:

| Forwarding behavior | Burst bytes | Bytes received in each trial | EAGAIN count per trial | Exact equality | Helper decoder |
| --- | ---: | ---: | --- | --- | --- |
| Ignore write results, large burst | 387072 | 487516 | 38, 38, 38 | Fail | Same protocol error, exit 1 |
| Ignore write results, small burst | 48384 | 792668 | 0, 0, 0 | Pass | Exit 0 |
| Retry writes, large burst | 387072 | 792668 | 18, 18, 18 | Pass | Exit 0 |

The first mismatch in the damaged stream was at wire offset 90112 in all trials.
The received stream was 305152 bytes short. The decoder later noticed the next
frame header at the wrong position; it cannot reconstruct lost bytes. The exact
amount received before the error is noticed depends on read boundaries. Its
`buffered` field does not measure the number of lost bytes.

The decoder probe receives Data frames for an otherwise unopened channel.
This isolates frame decoding from target SSH authentication, target socket
buffering and channel credit limits. It is not a full end-to-end SSH workload test.

Raw hashes, byte counts, return counters, and helper errors:
[ssh-write-loss-results.json](ssh-write-loss-results.json).

## Correct repair and deployment confirmation

The SSH forwarding worker must keep the current input buffer until all its bytes
are accepted. It must advance only after positive write returns, retry EAGAIN with
the same arguments, and wait for the socket directions indicated by libssh2.
Waiting must release the shared channel mutex so the receive worker can progress.
Permanent errors and shutdown must terminate cleanly. Merely shrinking input
buffers or increasing timeouts does not correct this write contract violation.

For the corporate deployment, ask the jump-host administrator to:

1. Identify the **guacd / libguac-client-ssh build**, including vendor patches and
   linked libssh2 version. The browser web-application version alone is insufficient.
2. Verify whether `ssh_input_thread()` checks/retries the write return value.
3. If necessary, log requested bytes and write return values around this call
   during a failing reproduction. No SSH plaintext payload needs to be logged.
4. Compare original and retry-corrected forwarding under the same workload.

An EAGAIN/short return followed by advancing to new input directly confirms this
mechanism on the installed host. A retry-corrected A/B run plus exact synthetic
byte equality is stronger confirmation. If the vendor build already retries
correctly, investigate the remaining path instead of assuming this upstream defect
is present there.

The bridge uses reliable records to detect and recover damaged input without
requiring a Guacamole patch. Flow-control tuning reduces pressure but does not
repair an upstream write-contract violation.

## Repeat locally

This investigation does not change production encoding or defaults. It requires a
Linux environment capable of opening loopback TCP sockets;
no root SSH daemon or company server access is required.

```sh
# Optional isolated tools environment.
python -m venv /tmp/guac-write-tools
/tmp/guac-write-tools/bin/pip install asyncssh==2.24.1 cmake

git clone --branch libssh2-1.11.1 --depth 1 https://github.com/libssh2/libssh2.git /tmp/guac-libssh2
/tmp/guac-write-tools/bin/cmake -S /tmp/guac-libssh2 -B /tmp/guac-libssh2/build \
  -DBUILD_SHARED_LIBS=ON -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DCRYPTO_BACKEND=OpenSSL
/tmp/guac-write-tools/bin/cmake --build /tmp/guac-libssh2/build -j 4

/tmp/guac-write-tools/bin/python scripts/diagnostics/reproduce-ssh-write-loss.py \
  --libssh2 /tmp/guac-libssh2/build/src/libssh2.so \
  --output /tmp/guac-write-results.json
```

Building libssh2 also requires a C compiler and OpenSSL development headers. The
capturing SSH server, host key, proxy, and synthetic captures are temporary and
local. The script never launches the received command as shell text. Network
backpressure is timing-sensitive; repeat trials if a host does not reproduce it.
