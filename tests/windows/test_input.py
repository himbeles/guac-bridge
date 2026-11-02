import asyncio, base64, re, unittest
from pathlib import Path
from conpty import Terminal

ROOT = Path(__file__).resolve().parents[2]


class InputTest(unittest.IsolatedAsyncioTestCase):
    async def test_raw_console_input(self):
        t = Terminal([str(ROOT / "target/release/examples/console_input_probe.exe")])
        try:

            async def ready():
                while b"INPUT:READY" not in t.data:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(ready(), 10)
            t.write(b"ABC\r")
            self.assertEqual(await asyncio.to_thread(t.wait, 10), 0)
            self.assertRegex(bytes(t.data), rb"INPUT:QUJD")
        finally:
            print("input probe output", repr(bytes(t.data)))
            t.close()
