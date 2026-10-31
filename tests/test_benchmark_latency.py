"""Integrity and lifecycle checks for the two-direction benchmark delay proxy."""
import asyncio
import unittest

from benchmarks.latency import LatencyProxy


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_stream_preserves_bytes_and_half_close(self):
        async def reply(reader, writer):
            data = await reader.read()
            writer.write(data[::-1])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(reply, '127.0.0.1', 0)
        proxy = await LatencyProxy('127.0.0.1', server.sockets[0].getsockname()[1], 2).start()
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
            data = bytes(range(256)) * 4096
            writer.write(data)
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await asyncio.wait_for(reader.read(), 10), data[::-1])
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.close()
            server.close()
            await server.wait_closed()
        self.assertFalse(proxy.tasks)
        self.assertFalse(proxy.writers)

    async def test_close_cancels_pending_delivery(self):
        async def hold(reader, writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(hold, '127.0.0.1', 0)
        proxy = await LatencyProxy('127.0.0.1', server.sockets[0].getsockname()[1], 10000).start()
        reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
        try:
            writer.write(b'pending')
            await writer.drain()
            await asyncio.sleep(0.01)
            await asyncio.wait_for(proxy.close(), 2)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()
            server.close()
            await server.wait_closed()
        self.assertFalse(proxy.tasks)
        self.assertFalse(proxy.writers)
