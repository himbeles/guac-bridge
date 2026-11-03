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
import tomllib
import aiohttp
from aiohttp import web
from gateway import Gateway

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / 'target/release/guac-bridge'
VERSION = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']['package']['version']
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

    async def child(self, *extra, mode='ssh', foreground=True):
        command = [str(BINARY), mode]
        if foreground and not any(flag in extra for flag in ('--stop', '--status')):
            command += ['--foreground']
        command += ['--guacamole-url', self.url, '--guacamole-user', 'test', '--no-keyring', '--startup-delay', '0', *map(str, extra)]
        child = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env())
        self.children.append(child)
        return child

    async def bridge(self, target, mode='ssh', *extra):
        child = await self.child('--local-port', '0', '--remote-port', str(target), '--benchmark-control', *extra, mode=mode)
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
        error = (await child.stderr.read()).decode()
        self.assertEqual(child.returncode, 0, error)
        return error

    async def test_list_empty_and_legacy_unresponsive(self):
        async def listing():
            child = await asyncio.create_subprocess_exec(str(BINARY), 'list',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env())
            output, error = await asyncio.wait_for(child.communicate(), 5)
            self.assertEqual(child.returncode, 0, error.decode())
            return output.decode()
        self.assertIn('No registered bridges', await listing())
        directory = self.home/'data'/'guac-bridge'
        directory.mkdir(parents=True, exist_ok=True)
        # Closed socket: an old registration cannot establish a live bridge.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        (directory/'ssh-2222.json').write_text(json.dumps(dict(
            pid=1, control=port, token='private-control-token', identity='old', username='old-user')))
        text = await listing()
        self.assertIn('Unresponsive', text)
        self.assertIn('restart bridge', text)
        self.assertIn('old-user', text)
        self.assertIn('ssh://127.0.0.1:2222', text)
        self.assertNotIn('private-control-token', text)
        self.assertTrue((directory/'ssh-2222.json').exists())

    async def test_prepare_update_stops_all_foreground_bridges(self):
        target = await self.target(self.echo)
        for mode in ('ssh', 'llm'):
            child = await self.child('--local-port', '0', '--remote-port', target, mode=mode)
            while b'Bridge ready on ' not in await asyncio.wait_for(child.stderr.readline(), 20):
                self.assertIsNone(child.returncode)
        command = await asyncio.create_subprocess_exec(str(BINARY), 'prepare-update',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env())
        output, error = await asyncio.wait_for(command.communicate(), 25)
        self.assertEqual(command.returncode, 0, (output+error).decode())
        self.assertIn(b'Bridges and tray stopped', output+error)
        for child in self.children:
            await asyncio.wait_for(child.wait(), 5)
            self.assertEqual(child.returncode, 0)
        self.assertEqual(list((self.home/'data'/'guac-bridge').glob('*.json')), [])

    async def test_update_gate_blocks_new_bridge_start(self):
        import fcntl
        directory = self.home/'data'/'guac-bridge'
        directory.mkdir(parents=True)
        with (directory/'update.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = await self.child('--local-port', '0', '--remote-port', 22)
            await asyncio.wait_for(child.wait(), 5)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('update is in progress', (await child.stderr.read()).decode())

    async def test_tray_registration_usage_and_authenticated_stop(self):
        import re
        target = await self.target(self.echo)
        for mode in ('ssh', 'llm'):
            with self.subTest(mode=mode):
                child = await self.child('--local-port', '0', '--remote-port', target, mode=mode)
                while True:
                    line = await asyncio.wait_for(child.stderr.readline(), 20)
                    self.assertTrue(line, 'Bridge exited before ready')
                    match = re.search(rb'Bridge ready on 127.0.0.1:(\d+)', line)
                    if match:
                        port = int(match[1])
                        break
                registration = self.home/'data'/'guac-bridge'/f'{mode}-{port}.json'
                state = json.loads(registration.read_text())
                self.assertEqual(state['mode'], mode)
                self.assertEqual(state['port'], port)
                self.assertEqual(state['pid'], child.pid)
                self.assertTrue(state['instance'])
                self.assertEqual(state['jump_url'], self.url)
                listing = await asyncio.create_subprocess_exec(str(BINARY), 'list',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env())
                output, error = await asyncio.wait_for(listing.communicate(), 5)
                self.assertEqual(listing.returncode, 0, error.decode())
                text = output.decode()
                self.assertIn(mode.upper(), text)
                self.assertIn(self.url, text)
                self.assertIn('test', text)
                self.assertIn(f'127.0.0.1:{port}', text)
                self.assertIn(f'127.0.0.1:{target} (on server)', text)
                self.assertNotIn(state['token'], text)

                async def control(command, token=state['token']):
                    reader, writer = await asyncio.open_connection('127.0.0.1', state['control'])
                    writer.write((json.dumps(dict(command=command, token=token))+'\n').encode())
                    await writer.drain()
                    response = await asyncio.wait_for(reader.readline(), 3)
                    writer.close()
                    await writer.wait_closed()
                    return response

                self.assertEqual(await control('stop', 'incorrect'), b'')
                self.assertIsNone(child.returncode)
                self.assertEqual(await control('status'), b'OK\n')
                initial = json.loads(await control('inspect'))
                self.assertEqual(initial['uploaded'], 0)
                payload = bytes(range(256))*512
                await self.roundtrip(port, payload)
                for _ in range(100):
                    final = json.loads(await control('inspect'))
                    if final['connections'] == 0:
                        break
                    await asyncio.sleep(.02)
                self.assertEqual(final['uploaded'], len(payload))
                self.assertEqual(final['downloaded'], len(payload))
                self.assertEqual(final['connections'], 0)
                self.assertEqual(final['total_connections'], 1)
                self.assertIsNotNone(final['idle_ms'])
                self.assertNotIn('token', final)
                self.assertEqual(await control('stop'), b'OK\n')
                await asyncio.wait_for(child.wait(), 5)
                self.assertEqual(child.returncode, 0, (await child.stderr.read()).decode())
                self.assertFalse(registration.exists())

    async def test_reliable_recovers_loss_corruption_and_duplicates_for_both_clients(self):
        target = await self.target(self.echo)
        for mode in ('ssh', 'llm'):
            with self.subTest(mode=mode):
                child, port = await self.bridge(target, mode)
                term = self.gateway.terminals[-1]
                term.live_faults = ['drop', 'corrupt', 'duplicate']
                await asyncio.wait_for(asyncio.gather(
                    self.roundtrip(port, bytes(range(256))*8192),
                    self.roundtrip(port, b'second-stream'*120000)), 30)
                error = await self.finish(child)
                self.assertEqual(term.applied_faults, ['drop', 'corrupt', 'duplicate'])
                self.assertIn('reliable input recovery', error)
                self.assertIn('--input-window', error)

    async def test_reliable_permanent_loss_has_bounded_actionable_error(self):
        child, port = await self.bridge(await self.target(self.echo), 'ssh',
                                        '--connect-timeout', '2')
        self.gateway.terminals[-1].drop_live_data = True
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(bytes(range(256))*2048)
        with contextlib.suppress(ConnectionResetError):
            await writer.drain()
        await asyncio.wait_for(child.wait(), 10)
        error = (await child.stderr.read()).decode()
        self.assertNotEqual(child.returncode, 0, error)
        self.assertIn('--input-window', error)
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()

    async def test_terminal_upload_uses_one_progress_row(self):
        import pty
        master, slave = pty.openpty()
        child = await asyncio.create_subprocess_exec(str(BINARY), 'ssh', '--foreground', '--guacamole-url', self.url,
            '--guacamole-user', 'test', '--no-keyring', '--startup-delay', '0', '--local-port', '0',
            '--remote-port', str(await self.target(self.echo)), stderr=slave,
            stdout=asyncio.subprocess.DEVNULL, env=self.env())
        self.children.append(child)
        os.close(slave)
        os.set_blocking(master, False)
        output = bytearray()
        try:
            async def ready():
                while b'Bridge ready on' not in output:
                    with contextlib.suppress(BlockingIOError):
                        output.extend(os.read(master, 65536))
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), 15)
            self.assertEqual(sum(b'Helper upload:' in row for row in output.split(b'\n')), 1)
            self.assertIn(b'100% [========================]', output)
            self.assertIn(f'v{VERSION}'.encode(), output)
            self.assertNotIn(b'remote confirmed', output)
            child.send_signal(signal.SIGINT)
            await asyncio.wait_for(child.wait(), 5)
            self.assertEqual(child.returncode, 0)
        finally:
            os.close(master)

    async def test_debug_logs_exclude_payload_and_preserve_transfer(self):
        target = await self.target(self.echo)
        local_log = self.home/'client.log'
        remote_log = self.home/'helper.log'
        child, port = await self.bridge(target, 'ssh', '--debug-log', local_log,
                                        '--server-debug-log', remote_log)
        secret = b'PAYLOAD_MUST_NEVER_APPEAR_IN_LOG'
        await self.roundtrip(port, secret * 40000)
        await self.finish(child)
        for log in (local_log, remote_log):
            data = log.read_text()
            self.assertIn('kind=Opened', data)
            self.assertIn('tx_bytes=', data)
            self.assertNotIn(secret.decode(), data)
            self.assertNotIn('kind=Data', data)
            self.assertNotIn('kind=Credit', data)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    async def test_remote_debug_log_setup_error_reaches_client(self):
        child = await self.child('--local-port', '0', '--server-debug-log',
                                 self.home/'missing-directory/log.txt')
        await asyncio.wait_for(child.wait(), 15)
        self.assertNotEqual(child.returncode, 0)
        error = (await child.stderr.read()).decode()
        self.assertIn('Remote helper failed:', error)
        self.assertIn('Cannot initialize remote debug log', error)

    async def test_remote_console_noise_recovers_without_closing_channels(self):
        child, port = await self.bridge(await self.target(self.echo))
        sockets = [await asyncio.open_connection('127.0.0.1', port) for _ in range(2)]
        await asyncio.sleep(.1)
        os.write(self.gateway.terminals[0].fd, b'\x1d\x00')
        for reader, writer in sockets:
            writer.write(b'survives console noise')
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await asyncio.wait_for(reader.read(), 10), b'survives console noise')
            writer.close()
            await writer.wait_closed()
        error = await self.finish(child)
        self.assertIn('rejected_records=1', error)
        self.assertIn('--input-window', error)

    async def test_gateway_input_window_is_independent_of_tcp_credits(self):
        target = await self.target(self.echo)
        for window, input_window in [(32, 2), (1, 8)]:
            with self.subTest(window=window, input_window=input_window):
                child, port = await self.bridge(target, 'ssh', '--window', window,
                                               '--input-window', input_window)
                self.gateway.hold_input_acks = True
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                payload = bytes(range(256))*4096
                writer.write(payload)
                await writer.drain()
                async def paused():
                    while len(self.gateway.held_input_acks) < input_window:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(paused(), 5)
                await asyncio.sleep(.1)
                self.assertEqual(len(self.gateway.held_input_acks), input_window)
                # Release real gateway ACKs and verify that the exact stream resumes.
                self.gateway.hold_input_acks = False
                for ack in self.gateway.held_input_acks:
                    await self.gateway.terminals[-1].output.put(ack)
                self.gateway.held_input_acks.clear()
                writer.write_eof()
                self.assertEqual(await asyncio.wait_for(reader.read(), 10), payload)
                writer.close()
                await writer.wait_closed()
                await self.finish(child)

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
        child = await self.child('--local-port',target)
        await asyncio.wait_for(child.wait(),5)
        error = (await child.stderr.read()).decode()
        self.assertNotEqual(child.returncode,0)
        self.assertIn('port is already in use or reserved',error)
        self.assertIn('--local-port',error)
        self.assertEqual(self.gateway.terminals,[])

    async def test_unsupported_architecture_is_reported_immediately(self):
        tools = self.home/'tools'
        tools.mkdir()
        uname = tools/'uname'
        uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo aarch64;; esac\n')
        uname.chmod(0o755)
        child = await self.child('--local-port','0','--remote-port',22,'--benchmark-control')
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
        child = await self.child('--local-port','0','--remote-port',22,'--benchmark-control')
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
        child = await self.child('--local-port','0','--remote-port',22,'--benchmark-control')
        await asyncio.wait_for(child.wait(),8)
        self.assertNotEqual(child.returncode,0)
        error = (await child.stderr.read()).decode()
        self.assertIn('Helper cannot execute',error)
        self.assertIn('Permission denied',error)

    async def test_upload_progress_and_clean_cli_wording(self):
        child = await self.child('--local-port','0','--remote-port',await self.target(self.echo))
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

    async def test_guacamole_ack_without_remote_consumption_fails_at_exact_block(self):
        # Gateway accepts/ACKs one input blob, but never writes it to the PTY.
        # The client must not claim 100% or wait indefinitely for helper readiness.
        self.gateway.drop_upload_after = 12000
        child = await self.child('--local-port', '0', '--remote-port', 22,
                                 '--connect-timeout', '2', '--benchmark-control')
        await asyncio.wait_for(child.wait(), 10)
        self.assertNotEqual(child.returncode, 0)
        error = (await child.stderr.read()).decode()
        self.assertIn('Remote shell did not acknowledge helper upload block', error)
        self.assertGreater(self.gateway.terminals[0].dropped_upload_bytes, 0)

    async def test_remote_decoder_stderr_reaches_client(self):
        tools = self.home/'tools'
        tools.mkdir()
        decoder = tools/'base64'
        decoder.write_text('#!/bin/sh\necho synthetic-decoder-failure >&2\nexit 2\n')
        decoder.chmod(0o755)
        child = await self.child('--local-port','0','--remote-port',22,'--connect-timeout','3','--benchmark-control')
        await asyncio.wait_for(child.wait(),10)
        self.assertNotEqual(child.returncode,0)
        error=(await child.stderr.read()).decode()
        self.assertIn('synthetic-decoder-failure',error)
        self.assertIn('decode failed',error)

    async def test_stalled_upload_completion_reports_its_stage(self):
        tools = self.home/'tools'
        tools.mkdir()
        head = tools/'head'
        head.write_text('#!/bin/sh\nexec /bin/cat >/dev/null\n')
        head.chmod(0o755)
        child = await self.child('--local-port','0','--remote-port',22,'--connect-timeout','2','--benchmark-control')
        await asyncio.wait_for(child.wait(),8)
        self.assertNotEqual(child.returncode,0)
        self.assertIn('waiting for remote upload block acknowledgement',(await child.stderr.read()).decode())

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
        hook = f'{BINARY} ssh --guacamole-url {self.url} --guacamole-user test --no-keyring --startup-delay 0 --local-port {port} --remote-port {target}'
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
            self.assertIn(b'100%',output)
            self.assertIn(b'Bridge ready on',output)
        finally:
            stop = await self.child('--stop','--local-port',port)
            await asyncio.wait_for(stop.wait(),5)
            os.close(master)
            if slave is not None:
                os.close(slave)

    async def test_managed_start_reuse_status_and_stop(self):
        await self.managed_lifetime('ssh')

    async def test_llm_defaults_to_background_and_reuses_bridge(self):
        await self.managed_lifetime('llm')

    async def managed_lifetime(self, mode):
        target = await self.target(self.echo)
        # Reserve then release an available client port.
        dummy = await asyncio.start_server(lambda r,w:w.close(),'127.0.0.1',0)
        port = dummy.sockets[0].getsockname()[1]
        dummy.close()
        await dummy.wait_closed()
        async def command(*flags, input_window=3, expected=0):
            child = await self.child('--local-port',port,'--remote-port',target,'--upload-batch-size','12288',
                                     '--input-window',input_window,*flags, foreground=False, mode=mode)
            await asyncio.wait_for(child.wait(),25)
            error = (await child.stderr.read()).decode()
            self.assertEqual(child.returncode,expected,error)
            return error
        try:
            startup = await command()
            self.assertIn('Bridge ready', startup)
            self.assertIn(f'v{VERSION}', startup)
            self.assertNotIn('remote confirmed', startup)
            encoded_size=((AGENT.stat().st_size+2)//3)*4
            self.assertEqual(self.gateway.upload_ack_count,(encoded_size+12287)//12288)
            await self.roundtrip(port,b'managed')
            self.assertIn('already running',await command())
            self.assertIn('different settings',await command(input_window=4,expected=1))
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
