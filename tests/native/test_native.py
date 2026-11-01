import asyncio
import contextlib
import hashlib
import json
import os
import signal
import shutil
from pathlib import Path
import tempfile
import unittest
import aiohttp
from aiohttp import web
from gateway import Gateway

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / 'target/release/guac-bridge'
AGENT = ROOT / 'crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64'


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.gateway = Gateway(self.home)
        self.url = await self.gateway.start()
        self.children = []
        self.servers = []

    async def asyncTearDown(self):
        for child in self.children:
            if child.returncode is None:
                if child.stdin:
                    with contextlib.suppress(BrokenPipeError,ConnectionResetError):
                        child.stdin.write(b'quit\n')
                        await child.stdin.drain()
                try:
                    await asyncio.wait_for(child.wait(), 5)
                except TimeoutError:
                    child.kill()
                    await child.wait()
        await self.gateway.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.temp.cleanup()

    def env(self):
        return dict(os.environ, GUAC_BRIDGE_PASSWORD='test', NO_PROXY='127.0.0.1,localhost', HOME=str(self.home), XDG_DATA_HOME=str(self.home/'data'))

    async def target(self, handler):
        async def serve(reader, writer):
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def child(self, *extra, mode='ssh', binary=BINARY):
        command = [str(binary)]
        if binary == BINARY:
            command.append(mode)
        command += ['--url', self.url, '--username', 'test', '--no-keyring', '--startup-delay', '0', *map(str, extra)]
        child = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env())
        self.children.append(child)
        return child

    async def bridge(self, target, mode='ssh', *extra):
        child = await self.child('--port', '0', '--target-port', str(target), '--benchmark-control', *extra, mode=mode)
        line = await asyncio.wait_for(child.stdout.readline(), 20)
        if not line:
            self.fail((await child.stderr.read()).decode())
        return child, json.loads(line)['endpoint']

    async def echo(self, reader, writer):
        data = await reader.read()
        writer.write(data)
        await writer.drain()
        writer.write_eof()

    async def roundtrip(self, port, data):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(data)
        await writer.drain()
        writer.write_eof()
        result = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        await writer.wait_closed()
        self.assertEqual(result, data)

    async def finish(self, child):
        child.stdin.write(b'quit\n')
        await child.stdin.drain()
        await asyncio.wait_for(child.wait(), 5)
        self.assertEqual(child.returncode, 0, (await child.stderr.read()).decode())

    async def test_first_upload_cache_reuse_and_repair(self):
        target = await self.target(self.echo)
        payload = bytes(range(256))*4096 + b'\x1b]482203;\x07'
        child, port = await self.bridge(target)
        await self.roundtrip(port, payload)
        await self.finish(child)
        cache = self.home/'cache/guac-bridge/helpers'/hashlib.sha256(AGENT.read_bytes()).hexdigest()
        self.assertEqual(cache.read_bytes(), AGENT.read_bytes())
        self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        self.assertGreater(self.gateway.terminals[0].upload_bytes, AGENT.stat().st_size)
        child, port = await self.bridge(target)
        self.assertLess(self.gateway.terminals[1].upload_bytes, 3900)
        await self.roundtrip(port, b'cached')
        await self.finish(child)
        cache.write_bytes(b'corrupted')
        child, port = await self.bridge(target)
        self.assertEqual(cache.read_bytes(), AGENT.read_bytes())
        await self.roundtrip(port, b'repaired')
        await self.finish(child)

    async def test_four_channels_and_remote_half_close(self):
        results = asyncio.Queue()
        async def target(reader, writer):
            writer.write(b'hello')
            await writer.drain()
            writer.write_eof()
            await results.put(await reader.read())
        child, port = await self.bridge(await self.target(target))
        async def transfer():
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            self.assertEqual(await reader.read(), b'hello')
            writer.write(b'late upload')
            await writer.drain()
            writer.write_eof()
            writer.close()
            await writer.wait_closed()
        await asyncio.wait_for(asyncio.gather(*(transfer() for _ in range(4))), 10)
        for _ in range(4):
            self.assertEqual(await asyncio.wait_for(results.get(),5),b'late upload')
        await self.finish(child)

    async def test_http_streaming_headers_and_binary_body(self):
        received = asyncio.Event()
        release = asyncio.Event()
        data = bytes(range(256))*256
        async def endpoint(request):
            self.assertEqual(await request.read(), data)
            self.assertEqual(request.headers['Authorization'], 'Bearer example')
            response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            await response.write(b'data: first\n\n')
            received.set()
            await release.wait()
            await response.write(b'data: second\n\n')
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_post('/v1/chat/completions', endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        try:
            child, port = await self.bridge(site._server.sockets[0].getsockname()[1], 'llm')
            async with aiohttp.ClientSession() as http:
                async with http.post(f'http://127.0.0.1:{port}/v1/chat/completions',data=data,headers={'Authorization':'Bearer example'}) as response:
                    self.assertEqual(response.status,200)
                    await asyncio.wait_for(received.wait(),5)
                    self.assertEqual(await asyncio.wait_for(response.content.readexactly(13),5), b'data: first\n\n')
                    release.set()
                    self.assertEqual(await response.read(),b'data: second\n\n')
            await self.finish(child)
        finally:
            release.set()
            await runner.cleanup()

    async def test_busy_port_fails_before_login(self):
        target = await self.target(self.echo)
        child = await self.child('--port',target)
        await asyncio.wait_for(child.wait(),5)
        error = (await child.stderr.read()).decode()
        self.assertNotEqual(child.returncode,0)
        self.assertIn('port is already in use or reserved',error)
        self.assertIn('--port',error)
        self.assertEqual(self.gateway.terminals,[])

    async def test_unsupported_architecture_is_reported_immediately(self):
        tools = self.home/'tools'
        tools.mkdir()
        uname = tools/'uname'
        uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo aarch64;; esac\n')
        uname.chmod(0o755)
        child = await self.child('--port','0','--target-port',22,'--benchmark-control')
        await asyncio.wait_for(child.wait(),5)
        self.assertNotEqual(child.returncode,0)
        error = (await child.stderr.read()).decode()
        self.assertIn('Requires Linux x86-64',error)
        self.assertNotIn('did not start',error)

    async def test_checksum_error_after_upload_is_immediate(self):
        tools = self.home/'tools'
        tools.mkdir()
        decoder = tools/'base64'
        decoder.write_text('#!/bin/sh\n/bin/cat >/dev/null\n')
        decoder.chmod(0o755)
        child = await self.child('--port','0','--target-port',22,'--benchmark-control')
        await asyncio.wait_for(child.wait(),8)
        self.assertNotEqual(child.returncode,0)
        error = (await child.stderr.read()).decode()
        self.assertIn('Helper checksum mismatch',error)
        self.assertNotIn('did not start',error)

    async def test_full_bootstrap_command_is_not_echoed(self):
        child, port = await self.bridge(await self.target(self.echo))
        await self.roundtrip(port,b'quiet bootstrap')
        await self.finish(child)
        terminal = self.gateway.terminals[0]
        self.assertIn(b'umask 077',terminal.all_input)
        self.assertNotIn(b'umask 077',terminal.screen)

    async def test_execution_failure_after_upload_is_immediate(self):
        tools = self.home/'tools'
        tools.mkdir()
        chmod = tools/'chmod'
        chmod.write_text('#!/bin/sh\ncase "$2" in */.upload.*) /bin/chmod 600 "$2";; *) /bin/chmod "$@";; esac\n')
        chmod.chmod(0o755)
        child = await self.child('--port','0','--target-port',22,'--benchmark-control')
        await asyncio.wait_for(child.wait(),8)
        self.assertNotEqual(child.returncode,0)
        error = (await child.stderr.read()).decode()
        self.assertIn('Helper cannot execute',error)
        self.assertIn('Permission denied',error)

    async def test_upload_progress_and_clean_cli_wording(self):
        child = await self.child('--port','0','--target-port',await self.target(self.echo))
        lines = []
        async def ready():
            while True:
                line = (await child.stderr.readline()).decode()
                self.assertTrue(line,'Client exited before ready')
                lines.append(line)
                if 'Bridge ready on' in line:
                    return
        await asyncio.wait_for(ready(),10)
        output = ''.join(lines)
        self.assertIn('Helper upload: 100%',output)
        self.assertIn('Verifying helper checksum',output)
        self.assertIn('Starting server helper',output)
        self.assertNotIn('Python',output)
        self.assertNotIn('uv needed',output)
        child.send_signal(signal.SIGINT)
        await asyncio.wait_for(child.wait(),5)
        self.assertEqual(child.returncode,0)

    async def test_stalled_upload_completion_reports_its_stage(self):
        tools = self.home/'tools'
        tools.mkdir()
        head = tools/'head'
        head.write_text('#!/bin/sh\nexec /bin/cat >/dev/null\n')
        head.chmod(0o755)
        child = await self.child('--port','0','--target-port',22,'--connect-timeout','2','--benchmark-control')
        await asyncio.wait_for(child.wait(),8)
        self.assertNotEqual(child.returncode,0)
        self.assertIn('waiting for server to finish helper upload',(await child.stderr.read()).decode())

    async def test_disconnect_ends_client_and_helper(self):
        child, _ = await self.bridge(await self.target(self.echo))
        await self.gateway.ws.close()
        await asyncio.wait_for(child.wait(),5)
        self.assertNotEqual(child.returncode,0)

    @unittest.skipUnless(shutil.which('ssh'),'OpenSSH client required')
    async def test_real_ssh_match_shows_progress_with_redirected_stdio(self):
        import fcntl
        import pty
        import termios
        target = await self.target(self.echo)
        dummy = await asyncio.start_server(lambda r,w:w.close(),'127.0.0.1',0)
        port = dummy.sockets[0].getsockname()[1]
        dummy.close()
        await dummy.wait_closed()
        config = self.home/'ssh-config'
        hook = f'{BINARY} ssh --ensure-running --url {self.url} --username test --no-keyring --startup-delay 0 --port {port} --target-port {target}'
        config.write_text(f'Host hook-test\n    HostName 127.0.0.1\n    Port {port}\nMatch originalhost hook-test exec "{hook}"\nMatch all\n')
        master, slave = pty.openpty()
        def session():
            os.setsid()
            fcntl.ioctl(slave,termios.TIOCSCTTY,0)
        output = bytearray()
        process = None
        try:
            process = await asyncio.create_subprocess_exec('ssh','-F',str(config),'-G','hook-test',stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,env=self.env(),pass_fds=(slave,),preexec_fn=session)
            self.children.append(process)
            os.close(slave)
            slave = None
            os.set_blocking(master,False)
            async def drain():
                while process.returncode is None:
                    with contextlib.suppress(BlockingIOError,OSError):
                        output.extend(os.read(master,65536))
                    await asyncio.sleep(.02)
                with contextlib.suppress(BlockingIOError,OSError):
                    output.extend(os.read(master,65536))
            await asyncio.wait_for(drain(),20)
            await process.wait()
            self.assertEqual(process.returncode,0,output.decode(errors='replace'))
            self.assertIn(b'Checking server helper',output)
            self.assertIn(b'Helper upload: 100%',output)
            self.assertIn(b'Bridge ready on',output)
        finally:
            stop = await self.child('--stop','--port',port)
            await asyncio.wait_for(stop.wait(),5)
            os.close(master)
            if slave is not None:
                os.close(slave)

    async def test_managed_alias_start_reuse_status_and_stop(self):
        target = await self.target(self.echo)
        # Reserve then release an available client port.
        dummy = await asyncio.start_server(lambda r,w:w.close(),'127.0.0.1',0)
        port = dummy.sockets[0].getsockname()[1]
        dummy.close()
        await dummy.wait_closed()
        binary = BINARY.with_name('guac-ssh-client')
        async def command(*flags):
            child = await self.child('--port',port,'--target-port',target,*flags,binary=binary)
            await asyncio.wait_for(child.wait(),25)
            error = (await child.stderr.read()).decode()
            self.assertEqual(child.returncode,0,error)
            return error
        try:
            self.assertIn('Bridge ready',await command('--ensure-running'))
            await self.roundtrip(port,b'managed')
            self.assertIn('already running',await command('--ensure-running'))
            self.assertIn('Bridge running',await command('--status'))
        finally:
            await command('--stop')
        for _ in range(50):
            try:
                _,w = await asyncio.open_connection('127.0.0.1',port)
                w.close()
                await w.wait_closed()
            except OSError:
                break
            await asyncio.sleep(.05)
        else:
            self.fail('Managed process did not release its listener')
