"""Windows process management with a protocol gateway; Linux tests run the real uploaded agent."""
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import struct
import unittest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]


def instruction(*args):
    return ','.join(f'{len(a)}.{a}' for a in args) + ';'


def parse(text):
    pos = 0
    while pos < len(text):
        args = []
        while True:
            dot = text.index('.', pos)
            n = int(text[pos:dot])
            pos = dot + 1
            args.append(text[pos:pos+n])
            pos += n
            sep = text[pos]
            pos += 1
            if sep == ';':
                break
        yield args


@unittest.skipUnless(os.name == 'nt', 'Windows native process management')
class WindowsManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sockets = []
        self.children = []
        self.commands = 0
        app = web.Application()
        async def login(request):
            self.assertEqual((await request.post())['password'],'synthetic-password')
            return web.json_response({'authToken':'synthetic-token'})
        app.router.add_post('/api/tokens',login)
        app.router.add_get('/websocket-tunnel',self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner,'127.0.0.1',0)
        await site.start()
        self.url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/#/client/MQBjAG15c3Fs'
        listener = await asyncio.start_server(lambda r,w:w.close(),'127.0.0.1',0)
        self.port = listener.sockets[0].getsockname()[1]
        listener.close()
        await listener.wait_closed()

    async def asyncTearDown(self):
        with contextlib.suppress(Exception):
            await self.command('--stop')
        for ws in self.sockets:
            await ws.close()
        await self.runner.cleanup()
        for process in self.children:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def websocket(self,request):
        ws = web.WebSocketResponse(protocols=('guacamole',))
        self.sockets.append(ws)
        await ws.prepare(request)
        await ws.send_str(instruction('sync','1'))
        command = bytearray()
        started = False
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            for op,*values in parse(msg.data):
                if op == 'pipe':
                    await ws.send_str(instruction('ack','0','OK','0'))
                elif op == 'blob':
                    data = base64.b64decode(values[1])
                    await ws.send_str(instruction('ack','0','OK','0'))
                    if started:
                        continue
                    command.extend(data)
                    if not command.endswith(b'\n'):
                        continue
                    text = command.decode()
                    nonce = re.search(r'([0-9a-f]{32}):CACHED',text)[1]
                    self.commands += 1
                    await ws.send_str(instruction('pipe','1','text/plain','BOOT'))
                    await ws.send_str(instruction('blob','1',base64.b64encode(f'{nonce}:CACHED\n'.encode()).decode()))
                    await ws.send_str(instruction('end','1'))
                    await ws.send_str(instruction('pipe','2','text/plain','LLM'))
                    body = json.dumps({'package_version':'0.11.0','version':5,'protocol':'tcp','window':32,'frame_size':65536}).encode()
                    frame = struct.pack('!2sBBII',b'GB',5,1,0,len(body))+body
                    frame = frame.replace(b'\x1d',b'\x1d\x3d').replace(b'\x1b',b'\x1d\x3b')
                    await ws.send_str(instruction('blob','2',base64.b64encode(frame).decode()))
                    started = True
        return ws

    async def command(self,*flags,binary='guac-ssh-client.exe'):
        args = [str(ROOT/'target/release'/binary)]
        if binary == 'guac-bridge.exe':
            args.append('ssh')
        args += ['--url',self.url,'--username','synthetic-user','--no-keyring','--startup-delay','0','--port',str(self.port),*flags]
        env = dict(os.environ, GUAC_BRIDGE_PASSWORD='synthetic-password',NO_PROXY='127.0.0.1')
        process = await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
        self.children.append(process)
        output,error = await asyncio.wait_for(process.communicate(),30)
        self.assertEqual(process.returncode,0,error.decode())
        return error.decode()

    async def test_alias_start_reuse_status_stop_and_root_cli(self):
        self.assertIn('Bridge ready',await self.command('--ensure-running'))
        self.assertIn('already running',await self.command('--ensure-running'))
        self.assertEqual(self.commands,1)
        self.assertIn('Bridge running',await self.command('--status',binary='guac-bridge.exe'))
        self.assertIn('stopping',await self.command('--stop'))
        for _ in range(60):
            try:
                _,writer = await asyncio.open_connection('127.0.0.1',self.port)
                writer.close()
                await writer.wait_closed()
            except OSError:
                break
            await asyncio.sleep(.1)
        else:
            self.fail('Background process did not stop')
