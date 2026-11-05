from pathlib import Path
import base64
import re
import json
import time
from conpty import Terminal

root = Path(__file__).resolve().parents[2]


def capture(*flags):
    start = time.perf_counter()
    t = Terminal([str(root / "target/release/examples/console_probe.exe"), *flags])
    try:
        assert t.wait(seconds=90) == 0
    finally:
        t.close()
    raw = bytes(t.data)
    elapsed = time.perf_counter() - start
    report = re.search(rb'GBPROBE:(\{[^\r\n]+\})', raw)
    assert report, 'Missing console probe timing report'
    metrics = json.loads(report[1])
    metrics.update(flags=flags, total_s=elapsed, end_to_end_mib_s=metrics['payload_bytes']/1048576/elapsed)
    print('Console benchmark:', json.dumps(metrics))
    chunks = re.findall(rb"\x1b\]482202;2;GBW1:([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)", raw)
    result = b"".join(base64.b64decode(chunk) for chunk in chunks)
    print({"flags": flags, "conpty_bytes": len(raw), "chunks": len(chunks),
           "decoded_bytes": len(result)})
    return result


assert capture() == bytes(range(256)) * 256
seed = 0x1794_a5c2
expected = bytearray()
for _ in range(4 * 1048576):
    seed ^= (seed << 13) & ((1 << 64) - 1)
    seed ^= seed >> 7
    seed ^= (seed << 17) & ((1 << 64) - 1)
    expected.append(seed & 255)

# Compare the previous bulk stdout path with explicit envelope flushing. Both
# operate through real ConPTY, using mixed binary data rather than a periodic
# byte pattern. Report bulk behavior without assuming every Windows build loses
# an envelope at a write boundary.
bulk = capture("--stdio", "--mixed", "--bulk")
print({"bulk_preserved": bulk == expected})
assert capture("--stdio", "--mixed", "--envelopes") == expected
print("ConPTY preserves complete short OSC envelopes")

assert capture("--stdio", "--mixed", "--pairs") == expected
assert capture("--mixed", "--pairs") == expected
