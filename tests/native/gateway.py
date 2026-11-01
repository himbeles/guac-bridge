"""Guacamole simulator backed by a real bash PTY, with no Python/uv on the remote PATH."""
import asyncio
import base64
import contextlib
import os
from pathlib import Path
import pty
import re
import shutil
from aiohttp import web


def instruction(*values):
    return ','.join(f'{len(v)}.{v}' for v in values) + ';'


def parse(text):
    """Test requests contain ASCII instructions; the Rust parser handles general Unicode."""
    pos = 0
    while pos < len(text):
        values = []
        while True:
            dot = text.index('.', pos)
            size = int(text[pos:dot])
            pos = dot + 1
            values.append(text[pos:pos+size])
            pos += size
            separator = text[pos]
            pos += 1
            if separator == ';':
                break
            assert separator == ','
        yield values


class Terminal:
    def __init__(self, home):
        self.home = home
        self.output = asyncio.Queue()
        self.raw = b''
        self.open = False
        self.stream = 0
        self.upload_bytes = 0
        self.all_input = bytearray()

    async def start(self):
        self.fd, slave = pty.openpty()
        tools = self.home / 'tools'
        tools.mkdir(exist_ok=True)
        for name in ('uname', 'stty', 'head', 'base64', 'sha256sum', 'cut', 'mktemp', 'chmod', 'mv', 'mkdir', 'rm'):
            dest = tools / name
            if not dest.exists():
                dest.symlink_to(shutil.which(name))
        env = dict(os.environ, HOME=str(self.home), XDG_CACHE_HOME=str(self.home/'cache'), PATH=str(tools))
        self.proc = await asyncio.create_subprocess_exec('/bin/bash', '--noprofile', '--norc', '-i', stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        os.set_blocking(self.fd, False)
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.fd, self.read)
        await self.output.put(instruction('sync', '123'))

    def blobs(self, data):
        for start in range(0, len(data), 6048):
            self.output.put_nowait(instruction('blob', str(self.stream), base64.b64encode(data[start:start+6048]).decode()))

    def read(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            self.loop.remove_reader(self.fd)
            return
        self.raw += data
        while self.raw:
            if not self.open:
                match = re.search(rb'\x1b\]482202;2;(BOOT|LLM)\x07', self.raw)
                if not match:
                    return
                self.raw = self.raw[match.end():]
                self.open = True
                self.stream += 1
                self.output.put_nowait(instruction('pipe', str(self.stream), 'text/plain', match[1].decode()))
            close = b'\x1b]482203;\x07'
            if close in self.raw:
                before, self.raw = self.raw.split(close, 1)
                self.blobs(before)
                self.output.put_nowait(instruction('end', str(self.stream)))
                self.open = False
            else:
                cut = self.raw.find(b'\x1b')
                if cut < 0:
                    cut = len(self.raw)
                if cut:
                    self.blobs(self.raw[:cut])
                    self.raw = self.raw[cut:]
                return

    async def feed(self, text):
        for op, *values in parse(text):
            if op == 'pipe':
                assert values == ['0', 'text/plain', 'STDIN']
                await self.output.put(instruction('ack', '0', 'Ready', '0'))
            elif op == 'blob':
                payload = base64.b64decode(values[1])
                assert len(payload) <= 6048
                self.all_input.extend(payload)
                self.upload_bytes += len(payload)
                while payload:
                    try:
                        count = os.write(self.fd, payload)
                        payload = payload[count:]
                    except BlockingIOError:
                        await asyncio.sleep(.001)
                await self.output.put(instruction('ack', '0', 'Written', '0'))

    async def close(self):
        self.loop.remove_reader(self.fd)
        os.close(self.fd)
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), 5)


class Gateway:
    def __init__(self, home):
        self.home = home
        self.terminals = []
        self.ws = None

    async def start(self):
        app = web.Application()
        app.router.add_post('/guacamole/api/tokens', self.login)
        app.router.add_get('/guacamole/websocket-tunnel', self.websocket)
        async def refresh(request):
            return web.json_response({})
        app.router.add_get('/guacamole/api/session/data/{tail:.*}', refresh)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f'http://127.0.0.1:{port}/guacamole/#/client/MQBjAG15c3Fs'

    async def login(self, request):
        data = await request.post()
        if data != {'username': 'test', 'password': 'test'}:
            return web.Response(status=403)
        return web.json_response({'authToken': 'test-token'})

    async def websocket(self, request):
        assert request.query['GUAC_ID'] == '1'
        ws = self.ws = web.WebSocketResponse(protocols=('guacamole',))
        await ws.prepare(request)
        term = Terminal(self.home)
        self.terminals.append(term)
        await term.start()
        async def write():
            while True:
                await ws.send_str(await term.output.get())
        task = asyncio.create_task(write())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await term.feed(msg.data)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ws

    async def close(self):
        if self.ws:
            await self.ws.close()
        await self.runner.cleanup()
        for term in self.terminals:
            await term.close()
