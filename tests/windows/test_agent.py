import asyncio, base64, json, re, struct, unittest, zlib
from pathlib import Path
from conpty import Terminal

ROOT = Path(__file__).resolve().parents[2]
PATTERN = re.compile(rb"\x1b\]482202;2;GBW1:([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)")


class Encoder:
    def __init__(self):
        self.sequence = 0

    def encode(self, kind, ident=0, payload=b""):
        self.sequence += 1
        frame = struct.pack("!2sBBII", b"GB", 5, kind, ident, len(payload)) + payload
        raw = b"RI\x01" + struct.pack("!QI", self.sequence, len(frame)) + frame
        raw += struct.pack("!I", zlib.crc32(raw))
        encoded = bytearray([0])
        code_at, code = 0, 1
        for byte in raw:
            if byte == 0:
                encoded[code_at] = code
                code_at = len(encoded)
                encoded.append(0)
                code = 1
            else:
                encoded.append(byte)
                code += 1
                if code == 255:
                    encoded[code_at] = code
                    code_at = len(encoded)
                    encoded.append(0)
                    code = 1
        encoded[code_at] = code
        wire = b"\x00" + bytes(encoded).replace(b"\x1d", b"\x1d\x3d").replace(b"\x1b", b"\x1d\x3b") + b"\x00"
        return base64.b64encode(wire) + b"\r"


class Frames:
    def __init__(self, terminal):
        self.t = terminal
        self.pos = 0
        self.osc = b""
        self.buffer = b""
        self.escape = False
        self.events = []

    def poll(self):
        data = bytes(self.t.data)
        self.osc += data[self.pos :]
        self.pos = len(data)
        while match := PATTERN.search(self.osc):
            raw = base64.b64decode(match[1])
            self.osc = self.osc[match.end() :]
            for b in raw:
                if self.escape:
                    self.buffer += bytes([b ^ 32])
                    self.escape = False
                elif b == 29:
                    self.escape = True
                else:
                    self.buffer += bytes([b])
            while len(self.buffer) >= 12:
                magic, version, kind, ident, n = struct.unpack(
                    "!2sBBII", self.buffer[:12]
                )
                assert magic == b"GB" and version == 5 and n <= 65536
                if len(self.buffer) < 12 + n:
                    break
                self.events.append((kind, ident, self.buffer[12 : 12 + n]))
                self.buffer = self.buffer[12 + n :]
        if len(self.osc) > 4096:
            self.osc = self.osc[-512:]

    async def next(self):
        async def wait():
            while True:
                self.poll()
                if self.events:
                    event = self.events.pop(0)
                    if event[0] not in (10, 12):
                        return event
                await asyncio.sleep(0.005)

        try:
            return await asyncio.wait_for(wait(), 15)
        except TimeoutError:
            raise AssertionError(repr(bytes(self.t.data)[-2000:]))


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_report_survives_console_session_teardown(self):
        t = Terminal([
            str(ROOT / "target/release/guac-bridge-agent.exe"),
            "--stdio", "--windows-console",
        ])
        try:
            self.assertEqual((await Frames(t).next())[:2], (1, 0))
            # A valid reliable record with an invalid channel ID fails inside
            # serve(), after console handles and the output carrier are opened.
            t.write(Encoder().encode(2, 0))
            async def report():
                while b"GBERR:END" not in t.data:
                    await asyncio.sleep(0.005)
                return bytes(t.data)
            data = await asyncio.wait_for(report(), 15)
            chunks = re.findall(
                rb"\x1b\]482202;2;GBERR:([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)", data
            )
            message = b"".join(base64.b64decode(chunk) for chunk in chunks if chunk != b"END")
            self.assertIn(b"Invalid channel ID", message)
            self.assertIn(b"Recent remote events:", message)
            self.assertEqual(await asyncio.to_thread(t.wait), 1)
        finally:
            t.close()

    async def test_windows_console_binary_echo_and_half_close(self):
        async def echo(reader, writer):
            data = await reader.read()
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        t = Terminal(
            [
                str(ROOT / "target/release/guac-bridge-agent.exe"),
                "--stdio",
                "--windows-console",
                "--target-port",
                str(target.sockets[0].getsockname()[1]),
            ]
        )
        f = Frames(t)
        encode = Encoder().encode
        try:
            ready = await f.next()
            self.assertEqual(ready[:2], (1, 0))
            self.assertEqual(json.loads(ready[2])["version"], 5)
            t.write(encode(2, 1))
            self.assertEqual((await f.next())[:2], (3, 1))
            payload = bytes(range(256)) * 256 + b"\x1b]482203;\x07"
            for offset in range(0, len(payload), 16384):
                t.write(encode(4, 1, payload[offset : offset + 16384]))
            t.write(encode(6, 1))
            received = bytearray()
            eof = False
            while True:
                kind, ident, body = await f.next()
                if kind == 4:
                    received.extend(body)
                    t.write(encode(5, ident))
                elif kind == 6:
                    eof = True
                elif kind == 8:
                    break
                elif kind == 9:
                    self.fail(body.decode())
            self.assertTrue(eof)
            self.assertEqual(bytes(received), payload)
            t.write(encode(11))
            self.assertEqual(await asyncio.to_thread(t.wait), 0)
        finally:
            t.close()
            target.close()
            await target.wait_closed()
