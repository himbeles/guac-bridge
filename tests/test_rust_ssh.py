"""Native client/helper integration through a simulated Guacamole and real PTY."""
import asyncio
import contextlib
from pathlib import Path
import sys
import unittest

from benchmarks.rust_ssh import BridgeWorker

BINARY = Path(__file__).resolve().parents[1]/'rust-ssh/target/release/guac-ssh-client-rs'


@unittest.skipUnless(sys.platform.startswith('linux') and BINARY.exists(), 'Build the Linux Rust prototype first')
class RustBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridges = []
        self.servers = []
        self.handlers = set()

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)

    async def target(self, handler):
        async def managed(reader, writer):
            self.handlers.add(asyncio.current_task())
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                self.handlers.discard(asyncio.current_task())
        server = await asyncio.start_server(managed, '127.0.0.1', 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def connect(self, target, variant='rust', timeout=30):
        bridge = BridgeWorker(variant, timeout=timeout)
        self.bridges.append(bridge)
        return await bridge.start(target)

    async def test_binary_half_close_and_clean_shutdown(self):
        async def echo_after_eof(reader, writer):
            data = await reader.read()
            writer.write(data)
            await writer.drain()
            writer.write_eof()
        target = await self.target(echo_after_eof)
        # Includes every byte, XON/XOFF, CR/LF and real Guacamole close markers.
        payload = bytes(range(256))*4096 + b'\x1b]482203;\x07'*64
        for variant in ('rust-matched', 'rust'):
            bridge = await self.connect(target, variant)
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            received = await asyncio.wait_for(reader.read(), 15)
            self.assertEqual(received, payload)
            writer.close()
            await writer.wait_closed()
            await bridge.close()
            self.bridges.remove(bridge)
            self.assertEqual(bridge.child.returncode, 0)
            self.assertEqual(bridge.helper_usage['helper_exit'], 0)

    async def test_remote_half_close_allows_local_upload(self):
        received = asyncio.get_running_loop().create_future()
        async def target(reader, writer):
            writer.write(b'remote finished')
            await writer.drain()
            writer.write_eof()
            received.set_result(await reader.read())
        bridge = await self.connect(await self.target(target))
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'remote finished')
        writer.write(b'upload after remote EOF')
        await writer.drain()
        writer.write_eof()
        self.assertEqual(await asyncio.wait_for(received, 5), b'upload after remote EOF')
        writer.close()
        await writer.wait_closed()

    async def test_four_concurrent_channels(self):
        async def echo(reader, writer):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.write_eof()
        bridge = await self.connect(await self.target(echo))
        async def transfer(index):
            payload = bytes([index])*262144 + bytes(range(256))
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await reader.read(), payload)
            writer.close()
            await writer.wait_closed()
        await asyncio.wait_for(asyncio.gather(*(transfer(i) for i in range(4))), 15)

    async def test_small_interactive_writes_flush_without_filling_a_frame(self):
        async def echo(reader, writer):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.write_eof()
        bridge = await self.connect(await self.target(echo))
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
        for value in range(32):
            data = bytes([value])
            writer.write(data)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(1), 2), data)
        writer.write_eof()
        self.assertEqual(await reader.read(), b'')
        writer.close()
        await writer.wait_closed()

    async def test_target_refused_does_not_kill_bridge(self):
        server = await asyncio.start_server(lambda r,w: None, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        bridge = await self.connect(port)
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
        with contextlib.suppress(ConnectionResetError):
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
        self.assertEqual(await bridge.command('start'), {})
        self.assertIsNone(bridge.child.returncode)

    async def test_guacamole_disconnect_fails_client(self):
        async def target(reader, writer):
            await reader.read()
        bridge = await self.connect(await self.target(target))
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
        writer.write(b'some data')
        await writer.drain()
        await asyncio.sleep(.1)
        await bridge.gateway.ws.close()
        await asyncio.wait_for(bridge.child.wait(), 8)
        self.assertNotEqual(bridge.child.returncode, 0)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    async def test_stalled_input_acks_fail_client(self):
        async def target(reader, writer):
            await reader.read()
        bridge = await self.connect(await self.target(target), timeout=2)
        terminal = bridge.gateway.terminals[0]
        original = terminal.output.put
        async def drop_input_ack(item):
            if item.startswith('3.ack,1.0,'):
                return
            await original(item)
        terminal.output.put = drop_input_ack
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
        writer.write(b'x'*(2**20))
        await writer.drain()
        await asyncio.wait_for(bridge.child.wait(), 9)
        self.assertNotEqual(bridge.child.returncode, 0)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
