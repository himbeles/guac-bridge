"""Binary TCP integration through the actual SSH helper in a Guacamole PTY."""
import asyncio
import base64
import contextlib
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest

import aiohttp

from guac_bridge.client import login
from guac_bridge.errors import BridgeError
from guac_bridge.ssh_client import SSHBridge
from test_bridge import Gateway, start_app


class SSHTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.helper = Path(self.temp.name) / 'guac-ssh-server'
        self.helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                               ' -m guac_bridge.ssh_server "$@"\n')
        self.helper.chmod(0o755)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.bridges, self.gateways, self.runners, self.servers = [], [], [], []
        self.handlers = set()

    async def target(self, handler):
        async def wrapped(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                await handler(reader, writer)
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                self.handlers.discard(task)
        server = await asyncio.start_server(wrapped, '127.0.0.1', 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def connect(self, target_port, websocket=True, **overrides):
        gateway = Gateway(websocket)
        self.gateways.append(gateway)
        runner, base = await start_app(gateway.app())
        self.runners.append(runner)
        base += 'guacamole/'
        token = await login(self.session, base, 'test', 'test', 5)
        values = dict(target_host='127.0.0.1', target_port=target_port, max_connections=8,
                      server_command=str(self.helper), startup_delay=.1, connect_timeout=5)
        values.update(overrides)
        bridge = SSHBridge(SimpleNamespace(**values), self.session, base, '1', 'c', 'mysql', token)
        self.bridges.append(bridge)
        await bridge.start()
        listener = await asyncio.start_server(bridge.accept, '127.0.0.1', 0)
        self.servers.append(listener)
        return bridge, listener.sockets[0].getsockname()[1], gateway

    async def echo(self, reader, writer):
        while data := await reader.read(8192):
            writer.write(data)
            await writer.drain()

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
        for bridge in self.bridges:
            await bridge.close()
        for gateway in self.gateways:
            await gateway.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for server in self.servers:
            await server.wait_closed()
        await self.session.close()
        self.temp.cleanup()

    async def transfer(self, local, data):
        reader, writer = await asyncio.open_connection('127.0.0.1', local)

        async def send():
            writer.write(data)
            await writer.drain()
            writer.write_eof()
        try:
            task = asyncio.create_task(send())
            result = await asyncio.wait_for(reader.read(), 15)
            await task
            self.assertEqual(result, data)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_two_nondefault_transport_profiles(self):
        target = await self.target(self.echo)
        for window, frame in ((8, 4096), (64, 32768)):
            with self.subTest(window=window, frame=frame):
                bridge, local, _ = await self.connect(target, window=window, frame_size=frame)
                self.assertEqual(bridge.data_window, window)
                self.assertEqual(bridge.tcp_chunk, frame)
                await self.transfer(local, os.urandom(256 * 1024))
                self.assertFalse(bridge.failed.is_set())

    async def test_malformed_binary_ends_helper_cleanly(self):
        target = await self.target(self.echo)
        bridge, _, _ = await self.connect(target)
        await bridge.send_input(b'{"type":"open","id":"\x1b"}\n')
        await asyncio.wait_for(bridge.failed.wait(), 3)
        self.assertFalse(bridge.requests)

    async def test_binary_roundtrip_and_client_half_close(self):
        target = await self.target(self.echo)
        bridge, local, _ = await self.connect(target)
        self.assertEqual((bridge.input_window, bridge.data_window, bridge.tcp_chunk), (64, 32, 65536))
        await self.transfer(local, bytes(range(256)) * 2048)
        self.assertFalse(bridge.failed.is_set())

    async def test_different_helper_package_version_fails_during_real_handshake(self):
        command = ("import guac_bridge,runpy;guac_bridge.__version__='0.0.0';"
                   "runpy.run_module('guac_bridge.ssh_server',run_name='__main__',alter_sys=True)")
        self.helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(command) + ' "$@"\n')
        target = await self.target(self.echo)
        with self.assertRaisesRegex(BridgeError, 'server, run: uv tool install --force.*guac-bridge=='):
            await self.connect(target, require_server_version=True)
        self.assertFalse(self.bridges[-1].ready.is_set())

    async def test_managed_startup_reports_version_mismatch_before_listening(self):
        command = ("import guac_bridge,runpy;guac_bridge.__version__='0.0.0';"
                   "runpy.run_module('guac_bridge.ssh_server',run_name='__main__',alter_sys=True)")
        self.helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(command) + ' "$@"\n')
        gateway = Gateway(True)
        self.gateways.append(gateway)
        runner, base = await start_app(gateway.app())
        self.runners.append(runner)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            local = sock.getsockname()[1]
        env = dict(os.environ, GUAC_PASSWORD='test', XDG_CACHE_HOME=self.temp.name,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'guac_bridge.ssh_client',
            '--ensure-running', '--require-server-version', '--url', base + 'guacamole/', '--connection-id', '1', '--data-source', 'mysql',
            '--username', 'test', '--no-keyring', '--port', str(local), '--server-command', str(self.helper),
            '--startup-delay', '.1', '--ready-timeout', '10', env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 1, stderr.decode())
        self.assertIn(b'version: 0.0.0', stderr)
        self.assertIn(b'uv tool install --force --python 3.XX guac-bridge==', stderr)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', local))
        self.assertFalse(list(Path(self.temp.name).rglob('*.startup-*.json')))

    async def test_single_remote_slot_can_be_reused_after_closed_channels(self):
        target = await self.target(self.echo)
        bridge, local, _ = await self.connect(target, max_connections=1)
        for index in range(8):
            await self.transfer(local, bytes([index]) * 16384)
            async with asyncio.timeout(5):
                while bridge.requests:
                    await asyncio.sleep(.005)
            self.assertFalse(bridge.failed.is_set())

    async def test_background_cli_starts_bridge_exits_and_reuses_listener(self):
        target = await self.target(self.echo)
        gateway = Gateway(True)
        self.gateways.append(gateway)
        runner, base = await start_app(gateway.app())
        self.runners.append(runner)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            local = sock.getsockname()[1]
        env = dict(os.environ, GUAC_PASSWORD='test',
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'),
                   XDG_CACHE_HOME=self.temp.name)
        command = [sys.executable, '-m', 'guac_bridge.ssh_client', '--ensure-running',
                   '--url', base + 'guacamole/', '--connection-id', '1', '--data-source', 'mysql',
                   '--username', 'test', '--no-keyring', '--port', str(local),
                   '--target-port', str(target), '--server-command', str(self.helper),
                   '--startup-delay', '.1', '--ready-timeout', '15',
                   '--background-log', str(Path(self.temp.name) / 'background.log')]
        pid = None
        try:
            process = await asyncio.create_subprocess_exec(*command, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 25)
            self.assertEqual(process.returncode, 0, stderr.decode())
            match = re.search(rb'PID (\d+)', stderr)
            self.assertIsNotNone(match, stderr.decode())
            pid = int(match[1])
            self.assertEqual(stdout, b'')
            await self.transfer(local, bytes(range(256)) * 256)
            process = await asyncio.create_subprocess_exec(*command, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertIn(b'Using existing listener', stderr)
            self.assertEqual(len(gateway.tokens), 1)
            reader, writer = await asyncio.open_connection('127.0.0.1', local)
            writer.write(b'active')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(6), 5), b'active')
            try:
                stop = await asyncio.create_subprocess_exec(sys.executable, '-m', 'guac_bridge.ssh_client',
                    '--stop', '--port', str(local), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr = await asyncio.wait_for(stop.communicate(), 15)
                self.assertEqual(stop.returncode, 0, stderr.decode())
                self.assertIn(b'Stopped bridge', stderr)
                self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
                pid = None
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            if pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)

    async def test_unavailable_websocket_fails_without_http_tunnel(self):
        target = await self.target(self.echo)
        with self.assertRaisesRegex(BridgeError, 'WebSocket tunnel failed'):
            await self.connect(target, websocket=False)
        self.assertNotIn('/guacamole/tunnel', self.gateways[-1].request_paths)

    async def test_concurrent_connections_and_close_isolation(self):
        target = await self.target(self.echo)
        bridge, local, _ = await self.connect(target)
        reader, writer = await asyncio.open_connection('127.0.0.1', local)
        writer.write(b'cancel this connection')
        await writer.drain()
        writer.transport.abort()
        await asyncio.gather(*(self.transfer(local, os.urandom(65536)) for _ in range(4)))
        self.assertFalse(bridge.failed.is_set())

    async def test_target_half_close_keeps_upload_open(self):
        received = asyncio.get_running_loop().create_future()

        async def half_close(reader, writer):
            writer.write(b'SSH-2.0-TestServer\r\n')
            await writer.drain()
            writer.write_eof()
            data = await reader.read()
            if not received.done():
                received.set_result(data)
        target = await self.target(half_close)
        _, local, _ = await self.connect(target)
        reader, writer = await asyncio.open_connection('127.0.0.1', local)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'SSH-2.0-TestServer\r\n')
        writer.write(b'client can still send after server EOF')
        await writer.drain()
        writer.write_eof()
        self.assertEqual(await asyncio.wait_for(received, 5), b'client can still send after server EOF')
        writer.close()
        await writer.wait_closed()

    async def test_refused_target_does_not_break_bridge(self):
        temporary = await asyncio.start_server(self.echo, '127.0.0.1', 0)
        port = temporary.sockets[0].getsockname()[1]
        temporary.close()
        await temporary.wait_closed()
        bridge, local, _ = await self.connect(port)
        reader, writer = await asyncio.open_connection('127.0.0.1', local)
        try:
            with contextlib.suppress(ConnectionResetError):
                self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()
        self.assertFalse(bridge.failed.is_set())

    async def test_tunnel_loss_closes_tcp(self):
        target = await self.target(self.echo)
        bridge, local, gateway = await self.connect(target)
        reader, writer = await asyncio.open_connection('127.0.0.1', local)
        writer.write(b'before disconnect')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(17), 5), b'before disconnect')
        await gateway.ws.close()
        with contextlib.suppress(ConnectionResetError):
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
        self.assertTrue(bridge.failed.is_set())
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()

    async def test_connection_limit(self):
        target = await self.target(self.echo)
        bridge, local, _ = await self.connect(target, max_connections=1)
        reader, writer = await asyncio.open_connection('127.0.0.1', local)
        writer.write(b'first')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 5), b'first')
        other_reader, other_writer = await asyncio.open_connection('127.0.0.1', local)
        self.assertEqual(await asyncio.wait_for(other_reader.read(), 5), b'')
        other_writer.close()
        await other_writer.wait_closed()
        writer.write(b'still works')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 5), b'still works')
        writer.close()
        await writer.wait_closed()
        self.assertFalse(bridge.failed.is_set())

    async def test_client_cli_ctrl_c_closes_active_connections(self):
        target_port = await self.target(self.echo)
        gateway = Gateway()
        self.gateways.append(gateway)
        runner, base = await start_app(gateway.app())
        self.runners.append(runner)
        reserve = await asyncio.start_server(self.echo, '127.0.0.1', 0)
        local_port = reserve.sockets[0].getsockname()[1]
        reserve.close()
        await reserve.wait_closed()
        env = dict(os.environ, GUAC_PASSWORD='test', PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'guac_bridge.ssh_client', '--url',
            base + 'guacamole/#/client/' + base64.b64encode(b'1\x00c\x00mysql').decode(), '--username', 'test',
            '--server-command', str(self.helper), '--startup-delay', '.1',
            '--target-port', str(target_port), '--port', str(local_port),
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        writer = None
        try:
            while True:
                line = await asyncio.wait_for(proc.stderr.readline(), 5)
                self.assertTrue(line, 'Client exited before listening')
                if b'Listening on' in line:
                    break
            reader, writer = await asyncio.open_connection('127.0.0.1', local_port)
            writer.write(b'live connection')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(15), 5), b'live connection')
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.communicate(), 8)
            self.assertEqual(proc.returncode, 0)
            with contextlib.suppress(ConnectionResetError):
                self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await writer.wait_closed()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    @unittest.skipUnless(os.environ.get('GUAC_TEST_OPENSSH') == '1' and os.geteuid() == 0
                         and shutil.which('sshd') and Path('/run/sshd').exists(),
                         'Opt-in real OpenSSH test requires GUAC_TEST_OPENSSH=1, root, sshd, and /run/sshd')
    async def test_real_openssh_exec_scp_and_port_forwarding(self):
        import pwd
        account = pwd.getpwnam(os.environ.get('GUAC_TEST_SSH_USER', 'root'))
        folder = Path(self.temp.name)

        async def command(*args, data=None):
            proc = await asyncio.create_subprocess_exec(*map(str, args), stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(data), 15)
                details = err.decode(errors='replace')
                if proc.returncode and (folder / 'sshd.log').exists():
                    details += '\n' + (folder / 'sshd.log').read_text()
                if 'chroot("/run/sshd"): Operation not permitted' in details:
                    self.skipTest('The execution environment cannot run the OpenSSH privilege-separation sandbox')
                self.assertEqual(proc.returncode, 0, details)
                return out
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        await command('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', folder / 'host')
        await command('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', folder / 'user')
        (folder / 'authorized_keys').write_bytes((folder / 'user.pub').read_bytes())
        reserve = await asyncio.start_server(self.echo, '127.0.0.1', 0)
        target_port = reserve.sockets[0].getsockname()[1]
        reserve.close()
        await reserve.wait_closed()
        config = folder / 'sshd_config'
        config.write_text(f'''Port {target_port}
ListenAddress 127.0.0.1
HostKey {folder / 'host'}
PidFile {folder / 'sshd.pid'}
AuthorizedKeysFile {folder / 'authorized_keys'}
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin yes
UsePAM no
StrictModes no
AllowUsers {account.pw_name}
AllowTcpForwarding yes
Subsystem sftp internal-sftp
LogLevel VERBOSE
''')
        if account.pw_uid != 0:
            folder.chmod(0o755)
            for path in [folder, *folder.iterdir()]:
                os.chown(path, account.pw_uid, account.pw_gid)
        daemon = await asyncio.create_subprocess_exec(shutil.which('sshd'), '-D', '-E', str(folder / 'sshd.log'), '-f', str(config),
                                                       stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                                                       user=account.pw_uid, group=account.pw_gid)
        forward = None
        try:
            for _ in range(30):
                if daemon.returncode is not None:
                    self.fail((await daemon.stderr.read()).decode())
                try:
                    r, w = await asyncio.open_connection('127.0.0.1', target_port)
                    w.close()
                    await w.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.05)
            _, local, _ = await self.connect(target_port)
            options = ['-i', str(folder / 'user'), '-o', 'BatchMode=yes',
                       '-o', 'StrictHostKeyChecking=accept-new',
                       '-o', 'UserKnownHostsFile=' + str(folder / 'known_hosts')]
            payload = os.urandom(256000)
            destination_host = account.pw_name + '@127.0.0.1'
            echoed = await command('ssh', '-p', local, *options, destination_host, 'cat', data=payload)
            self.assertEqual(echoed, payload)
            source, destination = folder / 'source.bin', folder / 'destination.bin'
            source.write_bytes(payload)
            await command('scp', '-q', '-P', local, *options, source, destination_host + ':' + str(destination))
            self.assertEqual(destination.read_bytes(), payload)
            echo_port = await self.target(self.echo)
            reserve = await asyncio.start_server(self.echo, '127.0.0.1', 0)
            forward_port = reserve.sockets[0].getsockname()[1]
            reserve.close()
            await reserve.wait_closed()
            forward = await asyncio.create_subprocess_exec(
                'ssh', '-p', str(local), *options, '-o', 'ExitOnForwardFailure=yes', '-N',
                '-L', f'127.0.0.1:{forward_port}:127.0.0.1:{echo_port}', destination_host,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            for _ in range(60):
                if forward.returncode is not None:
                    self.fail((await forward.stderr.read()).decode())
                try:
                    r, w = await asyncio.open_connection('127.0.0.1', forward_port)
                    w.write(b'forwarded through real SSH')
                    await w.drain()
                    self.assertEqual(await asyncio.wait_for(r.readexactly(25), 5), b'forwarded through real SSH')
                    w.close()
                    await w.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.05)
            else:
                self.fail('SSH port forward did not become ready')
        finally:
            for proc in (forward, daemon):
                if proc and proc.returncode is None:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), 5)


if __name__ == '__main__':
    unittest.main()

