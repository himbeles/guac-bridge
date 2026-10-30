"""Integration tests: actual helper in a PTY, simulated Guacamole, mock vLLM."""
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

import aiohttp
from aiohttp import web
from guac_bridge.client import Bridge, BridgeError, Parser, bootstrap, instruction, login, parse_url


async def start_app(app):
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, 'http://127.0.0.1:' + str(port) + '/'


class Terminal:
    def __init__(self):
        self.output = asyncio.Queue()
        self.parser = Parser()
        self.raw = b''
        self.open = False
        self.task = None
        self.proc = None

    async def start(self):
        import pty
        self.fd, slave = pty.openpty()
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
        self.proc = await asyncio.create_subprocess_exec(
            '/bin/bash', '--noprofile', '--norc', '-i', stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        os.set_blocking(self.fd, False)
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.fd, self.read)
        await self.output.put(instruction('sync', '123'))

    def read(self):
        try:
            data = os.read(self.fd, 65536)
        except (OSError, BlockingIOError):
            return
        self.raw += data
        if not self.open:
            marker = b'\x1b]482202;2;LLM\x07'
            if marker not in self.raw:
                return
            self.raw = self.raw.split(marker, 1)[1]
            self.open = True
            self.output.put_nowait(instruction('pipe', '1', 'text/plain', 'LLM'))
        close = b'\x1b]482203;\x07'
        if close in self.raw:
            before, self.raw = self.raw.split(close, 1)
            if before:
                self.output.put_nowait(instruction('blob', '1', base64.b64encode(before).decode()))
            self.output.put_nowait(instruction('end', '1'))
            self.open = False
        else:
            # Preserve any partial OSC sequence at the end of a PTY read.
            cut = self.raw.find(b'\x1b')
            if cut < 0:
                cut = len(self.raw)
            if cut:
                self.output.put_nowait(instruction('blob', '1', base64.b64encode(self.raw[:cut]).decode()))
                self.raw = self.raw[cut:]

    async def feed(self, data):
        for opcode, *values in self.parser.feed(data):
            if opcode == 'pipe':
                assert values == ['0', 'text/plain', 'STDIN']
                await self.output.put(instruction('ack', '0', 'Ready', '0'))
            elif opcode == 'blob':
                payload = base64.b64decode(values[1])
                while payload:
                    try:
                        written = os.write(self.fd, payload)
                        payload = payload[written:]
                    except BlockingIOError:
                        await asyncio.sleep(.001)
                await self.output.put(instruction('ack', '0', 'Written', '0'))

    async def close(self):
        self.loop.remove_reader(self.fd)
        os.close(self.fd)
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()


class Gateway:
    def __init__(self, websocket=True):
        self.websocket = websocket
        self.terminals = []
        self.tokens = []
        self.ws = None

    def app(self):
        app = web.Application()
        app.router.add_post('/guacamole/api/tokens', self.login)
        app.router.add_get('/guacamole/websocket-tunnel', self.ws_handler)
        app.router.add_route('*', '/guacamole/tunnel', self.http_handler)
        return app

    async def login(self, request):
        data = await request.post()
        if data != {'username': 'test', 'password': 'test'}:
            return web.Response(status=403)
        self.tokens.append('test-token')
        return web.json_response({'authToken': 'test-token'})

    async def ws_handler(self, request):
        if not self.websocket:
            return web.Response(status=404)
        assert request.query['GUAC_ID'] == '1'
        assert request.query['GUAC_DATA_SOURCE'] == 'mysql'
        ws = self.ws = web.WebSocketResponse(protocols=('guacamole',))
        await ws.prepare(request)
        term = Terminal()
        self.terminals.append(term)
        await term.start()

        async def writer():
            while True:
                await ws.send_str(await term.output.get())
        task = asyncio.create_task(writer())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await term.feed(msg.data)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ws

    async def http_handler(self, request):
        query = request.query_string
        if query == 'connect':
            data = await request.post()
            assert data['GUAC_ID'] == '1'
            term = Terminal()
            self.terminals.append(term)
            await term.start()
            return web.Response(text=str(uuid.uuid4()))
        term = self.terminals[-1]
        if query.startswith('write:'):
            await term.feed(await request.text())
            return web.Response()
        if query.startswith('read:'):
            text = await term.output.get()
            while not term.output.empty():
                text += term.output.get_nowait()
            # Deliberately fragment instructions across HTTP response chunks.
            response = web.StreamResponse()
            await response.prepare(request)
            raw = (text + '0.;').encode()
            await response.write(raw[:7])
            await response.write(raw[7:])
            await response.write_eof()
            return response
        return web.Response(status=400)

    async def close(self):
        if self.ws:
            await self.ws.close()
        for terminal in self.terminals:
            await terminal.close()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runners, self.bridges, self.gateways = [], [], []
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.temp = tempfile.TemporaryDirectory()
        self.helper = Path(self.temp.name) / 'guac-llm-server'
        self.helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                               ' -m guac_bridge.server "$@"\n')
        self.helper.chmod(0o755)
        app = web.Application()
        app.router.add_get('/v1/models', self.models)
        app.router.add_post('/v1/chat/completions', self.chat)
        runner, self.upstream = await start_app(app)
        self.runners.append(runner)
        self.upstream_done = asyncio.Event()

    async def models(self, request):
        return web.json_response({'data': [{'id': 'test-model'}]})

    async def chat(self, request):
        body = await request.json()
        if body.get('error'):
            return web.json_response({'error': 'bad model'}, status=400)
        if not body.get('stream'):
            return web.json_response({'echo': body, 'auth': request.headers.get('Authorization')})
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        await response.write(b'data: {"token":"first"}\n\n')
        await asyncio.sleep(.4)
        try:
            for i in range(body.get('chunks', 2)):
                await response.write(('data: ' + json.dumps({'token': 'x' * body.get('size', 1)}) + '\n\n').encode())
            await response.write(b'data: [DONE]\n\n')
            await response.write_eof()
        except ConnectionError:
            pass
        self.upstream_done.set()
        return response

    async def connect(self, websocket=True, mode='auto', **overrides):
        gateway = Gateway(websocket)
        self.gateways.append(gateway)
        runner, base = await start_app(gateway.app())
        self.runners.append(runner)
        base += 'guacamole/'
        token = await login(self.session, base, 'test', 'test', 5)
        values = dict(upstream=self.upstream.rstrip('/'), request_timeout=10, max_requests=4,
                      transport=mode, startup_delay=.1, connect_timeout=5,
                      server_command=str(self.helper), local_api_key=None, upstream_api_key=None)
        values.update(overrides)
        bridge = Bridge(SimpleNamespace(**values), self.session, base, '1', 'c', 'mysql', token)
        self.bridges.append(bridge)
        await bridge.start()
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get('/v1/models', bridge.proxy)
        app.router.add_post('/v1/chat/completions', bridge.proxy)
        runner, local = await start_app(app)
        self.runners.append(runner)
        return bridge, local, gateway

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.close()
        for gateway in self.gateways:
            await gateway.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        await self.session.close()
        self.temp.cleanup()

    async def test_websocket_models_and_unicode_large_request(self):
        _, local, _ = await self.connect()
        async with self.session.get(local + 'v1/models') as r:
            self.assertEqual((await r.json())['data'][0]['id'], 'test-model')
        payload = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Grüße 😀\n' * 20000}]}
        async with self.session.post(local + 'v1/chat/completions', json=payload) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())['echo'], payload)

    async def test_different_helper_package_version_fails_during_real_handshake(self):
        command = ("import guac_bridge,runpy;guac_bridge.__version__='0.0.0';"
                   "runpy.run_module('guac_bridge.server',run_name='__main__',alter_sys=True)")
        self.helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(command) + ' "$@"\n')
        with self.assertRaisesRegex(BridgeError, 'server, run: uv tool install --force guac-bridge=='):
            await self.connect()
        self.assertFalse(self.bridges[-1].ready.is_set())

    async def test_streaming_arrives_before_completion_and_flow_control(self):
        _, local, _ = await self.connect()
        async with self.session.post(local + 'v1/chat/completions',
                                     json={'stream': True, 'chunks': 32, 'size': 4096}) as r:
            first = await asyncio.wait_for(r.content.readline(), 2)
            self.assertIn(b'first', first)
            self.assertFalse(self.upstream_done.is_set(), 'SSE was buffered until completion')
            remaining = await r.read()
            self.assertIn(b'[DONE]', remaining)
            self.assertEqual(remaining.count(b'"token"'), 32)

    async def test_http_fallback_and_upstream_error(self):
        bridge, local, _ = await self.connect(websocket=False)
        self.assertEqual(bridge.tunnel.mode, 'http')
        async with self.session.get(local + 'v1/models') as r:
            self.assertEqual(r.status, 200)
        async with self.session.post(local + 'v1/chat/completions', json={'error': True}) as r:
            self.assertEqual(r.status, 400)
            self.assertEqual(await r.json(), {'error': 'bad model'})

    async def test_concurrent_requests_and_keys(self):
        _, local, _ = await self.connect(local_api_key='local', upstream_api_key='upstream')
        async with self.session.get(local + 'v1/models') as r:
            self.assertEqual(r.status, 401)

        async def one(i):
            async with self.session.post(local + 'v1/chat/completions', json={'i': i},
                                         headers={'Authorization': 'Bearer local'}) as r:
                self.assertEqual(r.status, 200)
                data = await r.json()
                self.assertEqual(data['echo']['i'], i)
                self.assertEqual(data['auth'], 'Bearer upstream')
        await asyncio.gather(*(one(i) for i in range(4)))

    async def test_tunnel_disconnect_fails_active_request(self):
        bridge, local, gateway = await self.connect()
        response = await self.session.post(local + 'v1/chat/completions', json={'stream': True})
        await response.content.readline()
        await gateway.ws.close()
        with self.assertRaises((aiohttp.ClientPayloadError, aiohttp.ClientConnectionError)):
            await response.read()
        self.assertTrue(bridge.failed.is_set())
        response.close()

    async def test_client_disconnect_cancels_and_releases_request(self):
        bridge, local, _ = await self.connect(max_requests=1)
        response = await self.session.post(local + 'v1/chat/completions',
                                           json={'stream': True, 'chunks': 100, 'size': 4096})
        await response.content.readline()
        response.close()
        for _ in range(30):
            if not bridge.requests:
                break
            await asyncio.sleep(.1)
        self.assertFalse(bridge.requests)
        async with self.session.get(local + 'v1/models') as r:
            self.assertEqual(r.status, 200)


class ProtocolTests(unittest.TestCase):
    def test_url_and_safe_command(self):
        client_id = base64.b64encode(b'42\x00c\x00mysql').decode()
        base, ident, kind, source = parse_url('https://example.org/#/client/' + client_id)
        self.assertEqual((base, ident, kind, source), ('https://example.org/', '42', 'c', 'mysql'))
        self.assertEqual(parse_url('https://example.org/guacamole/', '7', 'ldap')[0],
                         'https://example.org/guacamole/')
        args = SimpleNamespace(server_command='~/.local/bin/guac-llm-server',
                               upstream='http://127.0.0.1:8000', request_timeout=30, max_requests=4)
        self.assertTrue(bootstrap(args).startswith(b'"$HOME"/'))
        with self.assertRaises(BridgeError):
            parse_url('https://example.org/')

    def test_fragmentation(self):
        parser = Parser()
        wire = instruction('pipe', 1, 'text/plain', 'LLM') + instruction('sync', 123) + '0.;'
        result = []
        for char in wire:
            result.extend(parser.feed(char))
        self.assertEqual(result, [['pipe', '1', 'text/plain', 'LLM'], ['sync', '123'], ['']])


if __name__ == '__main__':
    unittest.main()
