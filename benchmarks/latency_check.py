"""Integrity and timing controls for the application-byte delay model."""
import asyncio
import os
import statistics
import time
from .latency import LatencyProxy


async def check_latency_model(rtt_ms):
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    proxy = await LatencyProxy('127.0.0.1', server.sockets[0].getsockname()[1], rtt_ms).start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
        samples = []
        for _ in range(8):
            start = time.perf_counter()
            writer.write(b'x')
            await writer.drain()
            assert await reader.readexactly(1) == b'x'
            samples.append((time.perf_counter() - start) * 1000)
        # Multiple chunks should overlap their propagation delays; the proxy
        # must not add a full RTT to every chunk of one bulk transfer.
        data = os.urandom(512 * 1024)
        start = time.perf_counter()
        writer.write(data)
        await writer.drain()
        assert await reader.readexactly(len(data)) == data
        bulk = time.perf_counter() - start
        writer.close()
        await writer.wait_closed()
        return dict(configured_rtt_ms=rtt_ms, measured_ping_ms=statistics.median(samples),
                    bulk_echo_bytes=len(data), bulk_echo_wall_s=bulk)
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()

