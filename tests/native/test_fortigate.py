"""FortiGate portal/hterm simulator; real helper framing and TCP forwarding.

The Linux helper runs its existing Windows printable/OSC carrier over pipes.
This validates protocol adaptation, not a deployed FortiGate or Windows console.
"""
import asyncio
import base64
import contextlib
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from urllib.parse import parse_qs
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT/'target/release/guac-bridge'
AGENT = ROOT/'crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64'


def parse(text):
    pos, values = 0, []
    while pos < len(text):
        dot = text.index('.', pos)
        units = int(text[pos:dot])
        start = pos = dot+1
        while units:
            units -= len(text[pos].encode('utf-16-le'))//2
            pos += 1
        if units != 0:
            raise ValueError('Invalid UTF-16 length')
        values.append(text[start:pos])
        separator = text[pos]
        pos += 1
        if separator == ';':
            return values
        if separator != ',':
            raise ValueError('Invalid separator')
    raise ValueError('Incomplete instruction')


def osc(prefix, data):
    return ''.join('\x1b]482202;2;'+prefix+base64.b64encode(data[n:n+120]).decode()+'\x07\x1b]482203;\x07'
                   for n in range(0, len(data), 120))


@unittest.skipUnless(os.name == 'posix', 'Linux helper fixture')
class FortigateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.agent = self.home/'fixture-agent'
        shutil.copyfile(AGENT,self.agent)
        self.agent.chmod(0o700)
        self.children, self.helpers, self.sockets, self.servers = [], [], [], []
        self.fail_login = False
        self.fail_server = False
        self.compliance = False
        self.fail_keepalive = False
        self.logins = self.connections = 0
        self.connection = None
        self.target_port = 22
        self.manual = True
        self.upload_digest = None
        app = web.Application()
        app.router.add_post('/remote/logincheck', self.login)
        app.router.add_get('/remote/hostcheck_install', self.hostcheck)
        async def index(request):
            return web.Response(text='<html>portal</html>')
        app.router.add_get('/sslvpn/portal/index.html', index)
        app.router.add_get('/remote/portal', self.portal)
        app.router.add_get('/remote/rds', self.terminal)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        for child in self.children:
            if child.returncode is None:
                child.terminate()
                await child.wait()
        for ws in self.sockets:
            await ws.close()
        for helper in self.helpers:
            if helper.returncode is None:
                helper.kill()
                await helper.wait()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def login(self, request):
        form = parse_qs(await request.text())
        self.assertEqual(request.content_type, 'text/plain')
        self.assertEqual(form, {'ajax':['1'], 'username':['portal-user'], 'credential':['portal-secret']})
        self.logins += 1
        if self.fail_login:
            return web.Response(text='ret=2,tokeninfo=challenge')
        response = web.Response(text='ret=1,redir=/remote/hostcheck_install?auth_type=1&user=portal-user')
        response.set_cookie('fixture_session', 'session-secret', path='/remote', httponly=True)
        return response

    async def hostcheck(self, request):
        self.assertEqual(request.cookies.get('fixture_session'), 'session-secret')
        return web.Response(text='<html>Compliance challenge</html>' if self.compliance else
                            '<html><script>document.location="/sslvpn/portal/index.html";</script></html>')

    async def portal(self, request):
        if request.cookies.get('fixture_session') != 'session-secret':
            return web.Response(status=401)
        if self.fail_keepalive and request.query.get('access') == 'keepalive':
            return web.Response(status=401)
        return web.json_response({'user':'portal-user', 'fgt_sslvpn_sid':'session-id'})

    async def terminal(self, request):
        self.assertEqual(request.cookies.get('fixture_session'), 'session-secret')
        self.connection = json.loads(request.query['connection'])
        self.assertEqual(self.connection, {'name':'', 'apptype':'ssh', 'host':'server.internal', 'port':'2222'})
        self.connections += 1
        ws = web.WebSocketResponse(protocols=['hterm'])
        self.sockets.append(ws)
        await ws.prepare(request)
        expected = [('resize','120:40'), ('user','server-😀'), ('passwd','server-secret'), ('start','')]
        for op, value in expected:
            message = await ws.receive()
            self.assertEqual(parse(message.data), [op,value])
        if self.fail_server:
            await ws.close()
            return ws
        await ws.send_str('\x1b[HWindows PowerShell\r\nPS C:\\> ')
        command = ''
        while not command.endswith('\r'):
            message = await ws.receive()
            op, value = parse(message.data)
            self.assertEqual(op, 'str')
            command += value
        encoded = command.split('-EncodedCommand ')[1].strip()
        script = base64.b64decode(encoded).decode('utf-16-le')
        nonce = re.search(r"'([0-9a-f]{32}):'", script)[1]
        wire = osc('GBWB:', f'{nonce}:PREPARED\n'.encode())
        await ws.send_str(wire[:7])
        await ws.send_str(wire[7:])
        if not self.manual:
            await ws.send_str(osc('GBWB:', f'{nonce}:UPLOAD\n'.encode()))
            count = int(re.search(r'New-Object byte\[\] (\d+)', script)[1])
            encoded_agent = ''
            while len(encoded_agent) < count:
                message = await ws.receive()
                op, data = parse(message.data)
                self.assertEqual(op,'str')
                encoded_agent += data
            self.assertEqual(len(encoded_agent),count)
            payload = base64.b64decode(encoded_agent,validate=True)
            self.upload_digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(self.upload_digest,re.search(r"\$h='([0-9a-f]{64})'",script)[1])
            await ws.send_str(osc('GBWB:', f'{nonce}:VERIFY\n'.encode()))
        await ws.send_str(osc('GBWB:', f'{nonce}:STARTING\n'.encode()))
        window = re.search(r'--window (\d+)', script)[1]
        frame_size = re.search(r'--frame-size (\d+)', script)[1]
        target_port = re.search(r'--target-port (\d+)', script)[1]
        helper = await asyncio.create_subprocess_exec(str(self.agent), '--stdio', '--windows-console', '--target-port', target_port,
            '--window', window, '--frame-size', frame_size,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.helpers.append(helper)

        async def output():
            while data := await helper.stdout.read(65536):
                # Real helper's printable carrier, fragmented at arbitrary WebSocket boundaries.
                await ws.send_str(data[:7].decode('ascii'))
                await ws.send_str(data[7:].decode('ascii'))
        pump = asyncio.create_task(output())
        pending = ''
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    break
                op, value = parse(message.data)
                self.assertEqual(op, 'str')
                helper.stdin.write(value.encode('ascii'))
                await helper.stdin.drain()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        return ws

    def env(self):
        return dict(os.environ, GUAC_BRIDGE_PASSWORD='portal-secret', GUAC_BRIDGE_SERVER_PASSWORD='server-secret',
                    NO_PROXY='127.0.0.1,localhost', HOME=str(self.home), XDG_DATA_HOME=str(self.home/'data'))

    def args(self, mode='ssh', foreground=True, port='0'):
        return [str(BINARY), mode, self.url, '--gateway','fortigate','--user','portal-user',
                '--server-host','server.internal','--server-port','2222','--server-user','server-😀',
                '--server-os','windows',*(['--server-command','fixture-agent'] if self.manual else []),'--no-keyring',
                '--startup-delay','0','--connect-timeout','3','--port',port,'--remote-port',str(self.target_port),
                *(['--foreground'] if foreground else [])]

    async def launch(self, mode='ssh', foreground=True, port='0'):
        child = await asyncio.create_subprocess_exec(*self.args(mode,foreground,port),env=self.env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.children.append(child)
        if foreground:
            async def ready():
                lines = []
                while line := await child.stderr.readline():
                    lines.append(line)
                    match = re.search(rb'Bridge ready on 127.0.0.1:(\d+)',line)
                    if match:
                        return int(match[1])
                self.fail(b''.join(lines).decode())
            return child, await asyncio.wait_for(ready(),15)
        return child, None

    @unittest.skipUnless((ROOT/'crates/guac-bridge/assets/guac-bridge-agent-windows-x86_64.exe').is_file(),
                         'Windows helper is staged by CI; local Linux source builds can omit it')
    async def test_bundled_windows_helper_upload_checksum(self):
        self.manual = False
        await self.launch()
        payload = (ROOT/'crates/guac-bridge/assets/guac-bridge-agent-windows-x86_64.exe').read_bytes()
        self.assertEqual(self.upload_digest,hashlib.sha256(payload).hexdigest())

    async def test_binary_transfer_concurrency_and_half_close(self):
        async def echo(reader, writer):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.write_eof()
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(echo, '127.0.0.1',0)
        self.servers.append(server)
        self.target_port = server.sockets[0].getsockname()[1]
        _, port = await self.launch()
        async def exchange():
            reader, writer = await asyncio.open_connection('127.0.0.1',port)
            payload = bytes(range(256))*1024
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await asyncio.wait_for(reader.read(),15), payload)
            writer.close()
            await writer.wait_closed()
        await asyncio.gather(exchange(),exchange())

    async def test_llm_streaming_preserves_headers_and_body(self):
        release = asyncio.Event()
        async def http(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            self.assertIn(b'Authorization: Bearer service-secret', request)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n9\r\ndata: a\n\n\r\n')
            await writer.drain()
            await release.wait()
            writer.write(b'9\r\ndata: b\n\n\r\n0\r\n\r\n')
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(http,'127.0.0.1',0)
        self.servers.append(server)
        self.target_port = server.sockets[0].getsockname()[1]
        _, port = await self.launch('llm')
        reader, writer = await asyncio.open_connection('127.0.0.1',port)
        writer.write(b'GET /v1/events HTTP/1.1\r\nHost: service\r\nAuthorization: Bearer service-secret\r\n\r\n')
        await writer.drain()
        first = await asyncio.wait_for(reader.readuntil(b'data: a\n\n\r\n'),5)
        self.assertIn(b'text/event-stream',first)
        release.set()
        self.assertIn(b'data: b\n\n', await asyncio.wait_for(reader.read(),5))
        writer.close()
        await writer.wait_closed()

    async def test_managed_reuse_gateway_listing_and_secret_handoff(self):
        listener = await asyncio.start_server(lambda r,w: w.close(),'127.0.0.1',0)
        port = str(listener.sockets[0].getsockname()[1])
        listener.close()
        await listener.wait_closed()
        first, _ = await self.launch(foreground=False,port=port)
        out, err = await asyncio.wait_for(first.communicate(),15)
        self.assertEqual(first.returncode,0,err.decode())
        self.assertIn(b'Bridge ready',err)
        state_path = self.home/'data'/'guac-bridge'/f'ssh-{port}.json'
        state_text = state_path.read_text()
        state = json.loads(state_text)
        self.assertEqual(state['gateway'],'fortigate')
        self.assertEqual(state['server_user'],'server-😀')
        for secret in ['portal-secret','server-secret','session-secret']:
            self.assertNotIn(secret,state_text)
            self.assertNotIn(secret,(Path('/proc')/str(state['pid'])/'cmdline').read_text())
        async def command(*args):
            process = await asyncio.create_subprocess_exec(str(BINARY),*args,env=self.env(),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            return process, await asyncio.wait_for(process.communicate(),10)
        try:
            second, _ = await self.launch(foreground=False,port=port)
            _, err = await asyncio.wait_for(second.communicate(),10)
            self.assertEqual(second.returncode,0,err.decode())
            self.assertIn(b'already running',err)
            self.assertEqual(self.logins,1)
            process, (out,err) = await command('list')
            self.assertEqual(process.returncode,0,err.decode())
            self.assertIn(b'FortiGate',out)
            self.assertIn(b'GATEWAY',out)
            self.assertIn(b'server.internal:2222',out)
            changed = self.args(foreground=False,port=port)
            changed[changed.index('server.internal')] = 'different.internal'
            child = await asyncio.create_subprocess_exec(*changed,env=self.env(),
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            _,err = await child.communicate()
            self.assertNotEqual(child.returncode,0)
            self.assertIn(b'different settings',err)
        finally:
            process, (_,err) = await command('ssh','--stop','--port',port)
            self.assertEqual(process.returncode,0,err.decode())
            for _ in range(100):
                if not state_path.exists(): break
                await asyncio.sleep(.05)
            self.assertFalse(state_path.exists())

    async def test_authentication_and_compliance_failures(self):
        for failure, expected in [('fail_login','MFA/SSO'),('compliance','compliance'),('fail_server','terminal closed')]:
            with self.subTest(failure=failure):
                setattr(self, failure, True)
                child = await asyncio.create_subprocess_exec(*self.args(),env=self.env(),
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                self.children.append(child)
                out, err = await asyncio.wait_for(child.communicate(),10)
                self.assertNotEqual(child.returncode,0)
                self.assertIn(expected,err.decode())
                self.assertNotIn(b'portal-secret',out+err)
                self.assertNotIn(b'server-secret',out+err)
                setattr(self, failure, False)
