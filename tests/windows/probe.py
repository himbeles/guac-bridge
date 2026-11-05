from pathlib import Path
import base64
import re
from conpty import Terminal

root = Path(__file__).resolve().parents[2]


def capture(*flags):
    t = Terminal([str(root / "target/release/examples/console_probe.exe"), *flags])
    try:
        assert t.wait() == 0
    finally:
        t.close()
    raw = bytes(t.data)
    chunks = re.findall(rb"\x1b\]482202;2;GBW1:([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)", raw)
    result = b"".join(base64.b64decode(chunk) for chunk in chunks)
    print({"flags": flags, "conpty_bytes": len(raw), "chunks": len(chunks),
           "decoded_bytes": len(result)})
    return result


assert capture() == bytes(range(256)) * 256
seed = 0x1794_a5c2
expected = bytearray()
for _ in range(524288):
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
