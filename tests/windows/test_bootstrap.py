"""Native client -> simulated Guacamole -> real Windows ConPTY -> TCP target."""

import asyncio, base64, contextlib, hashlib, json, os, re, sys, tempfile, unittest
from pathlib import Path
from aiohttp import web
from conpty import Terminal as Console

ROOT = Path(__file__).resolve().parents[2]


# Reuse only platform independent Guacamole framing, without importing POSIX PTY code.
def instruction(*values):
    return ",".join(f"{len(v)}.{v}" for v in values) + ";"


def parse(text):
    pos = 0
    while pos < len(text):
        values = []
        while True:
            dot = text.index(".", pos)
            size = int(text[pos:dot])
            pos = dot + 1
            values.append(text[pos : pos + size])
            pos += size
            sep = text[pos]
            pos += 1
            if sep == ";":
                break
            assert sep == ","
        yield values


PATTERN = re.compile(rb"\x1b\]482202;2;((?:GBWB|GBW1):[A-Za-z0-9+/=]+)(?:\x07|\x1b\\)")


class Gateway:
    def __init__(self):
        self.terminals = []
        self.tasks = []
        self.inputs = []

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/tokens", self.login)
        app.router.add_get("/websocket-tunnel", self.websocket)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/#/client/MQBjAG15c3Fs"

    async def login(self, request):
        return web.json_response({"authToken": "test-token"})

    async def websocket(self, request):
        ws = web.WebSocketResponse(protocols=("guacamole",))
        await ws.prepare(request)
        shell = os.environ.get("GUAC_TEST_SHELL", "powershell.exe")
        argv = (
            [shell, "-NoLogo", "-NoProfile"] if "powershell" in shell else [shell, "/Q"]
        )
        t = Console([str(ROOT / "target/release/examples/console_shell.exe"), *argv])
        self.terminals.append(t)
        self.inputs.append(bytearray())
        captured = self.inputs[-1]

        async def output():
            pos = 0
            buf = b""
            stream = 0
            await ws.send_str(instruction("sync", "123"))
            while not ws.closed:
                data = bytes(t.data)
                buf += data[pos:]
                pos = len(data)
                while match := PATTERN.search(buf):
                    stream += 1
                    buf = buf[match.end() :]
                    await ws.send_str(
                        instruction(
                            "pipe", str(stream), "text/plain", match[1].decode()
                        )
                    )
                    await ws.send_str(instruction("end", str(stream)))
                if len(buf) > 16384:
                    buf = buf[-512:]
                await asyncio.sleep(0.002)

        task = asyncio.create_task(output())
        self.tasks.append(task)
        try:
            async for msg in ws:
                for op, *values in parse(msg.data):
                    if op == "pipe":
                        await ws.send_str(instruction("ack", "0", "Ready", "0"))
                    elif op == "blob":
                        data = base64.b64decode(values[1])
                        captured.extend(data)
                        await asyncio.to_thread(t.write, data)
                        await ws.send_str(instruction("ack", "0", "Written", "0"))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ws

    async def close(self):
        for t in self.terminals:
            await asyncio.to_thread(t.close)
        await self.runner.cleanup()


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_cache_repair_ssh_and_http(self):
        gateway = Gateway()
        url = await gateway.start()
        children = []

        async def echo(r, w):
            data = await r.read()
            w.write(data)
            await w.drain()
            w.close()
            await w.wait_closed()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        helper = ROOT / "crates/guac-bridge/assets/guac-bridge-agent-windows-x86_64.exe"
        cache = (
            Path(os.environ["LOCALAPPDATA"])
            / "guac-bridge/helpers"
            / (hashlib.sha256(helper.read_bytes()).hexdigest() + ".exe")
        )
        if cache.exists():
            cache.unlink()

        async def bridge(mode, port):
            child = await asyncio.create_subprocess_exec(
                str(ROOT / "target/release/guac-bridge.exe"),
                mode,
                "--guacamole-url",
                url,
                "--guacamole-user",
                "test",
                "--server-os",
                "windows",
                "--no-keyring",
                "--startup-delay",
                "0",
                "--local-port",
                "0",
                "--remote-port",
                str(port),
                "--benchmark-control",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(
                    os.environ,
                    GUAC_BRIDGE_PASSWORD="test",
                    NO_PROXY="127.0.0.1,localhost",
                ),
            )
            children.append(child)
            try:
                line = await asyncio.wait_for(child.stdout.readline(), 60)
            except TimeoutError:
                child.kill()
                await child.wait()
                raise AssertionError(
                    (await child.stderr.read()).decode()
                    + "\nCONSOLE:"
                    + bytes(gateway.terminals[-1].data).decode(errors="replace")
                )
            if not line:
                raise AssertionError(
                    (await child.stderr.read()).decode()
                    + "\nCONSOLE:"
                    + bytes(gateway.terminals[-1].data).decode(errors="replace")
                )
            return child, json.loads(line)["endpoint"]

        async def finish(c):
            c.stdin.write(b"quit\n")
            await c.stdin.drain()
            await asyncio.wait_for(c.wait(), 15)
            self.assertEqual(c.returncode, 0, (await c.stderr.read()).decode())

        try:
            for i in range(3):
                if i == 2:
                    cache.write_bytes(b"corrupted")
                c, port = await bridge("ssh", target.sockets[0].getsockname()[1])
                self.assertEqual(cache.read_bytes(), helper.read_bytes())

                async def roundtrip(seed):
                    r, w = await asyncio.open_connection("127.0.0.1", port)
                    payload = (
                        bytes(range(256)) * 1024 + bytes([seed]) + b"\x1b]482203;\x07"
                    )
                    try:
                        w.write(payload)
                        await w.drain()
                        w.write_eof()
                        self.assertEqual(await asyncio.wait_for(r.read(), 30), payload)
                    finally:
                        w.close()
                        await w.wait_closed()

                await asyncio.gather(*(roundtrip(seed) for seed in range(3)))
                await finish(c)
            self.assertLess(len(gateway.inputs[1]), len(gateway.inputs[0]))

            async def http(r, w):
                await r.readuntil(b"\r\n\r\n")
                w.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: one\n\n"
                )
                await w.drain()
                await asyncio.sleep(0.05)
                w.write(b"data: [DONE]\n\n")
                await w.drain()
                w.close()

            server = await asyncio.start_server(http, "127.0.0.1", 0)
            try:
                c, endpoint = await bridge("llm", server.sockets[0].getsockname()[1])
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"http://127.0.0.1:{endpoint}/v1/chat/completions",
                        json={"stream": True},
                    ) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(
                            await asyncio.wait_for(
                                response.content.readuntil(b"\n\n"), 10
                            ),
                            b"data: one\n\n",
                        )
                        self.assertEqual(
                            await asyncio.wait_for(
                                response.content.readuntil(b"\n\n"), 10
                            ),
                            b"data: [DONE]\n\n",
                        )
                        self.assertEqual(await response.read(), b"")
                await finish(c)
            finally:
                server.close()
                await server.wait_closed()
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            await gateway.close()
            target.close()
            await target.wait_closed()
