from pathlib import Path
import base64
import re
from conpty import Terminal

root = Path(__file__).resolve().parents[2]
t = Terminal([str(root / "target/release/examples/console_probe.exe")])
try:
    assert t.wait() == 0
finally:
    t.close()
raw = bytes(t.data)
chunks = re.findall(rb"\x1b\]482202;2;GBW1:([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)", raw)
result = b"".join(base64.b64decode(chunk) for chunk in chunks)
expected = bytes(range(256)) * 256
print(
    {
        "conpty_bytes": len(raw),
        "chunks": len(chunks),
        "decoded_bytes": len(result),
        "expected_bytes": len(expected),
    }
)
assert result == expected, repr(raw[:500])
print("ConPTY preserves complete short OSC envelopes")
