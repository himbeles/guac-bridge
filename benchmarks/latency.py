"""Bounded, ordered application-byte delay in both directions of a TCP proxy.

Read chunks receive independent delivery deadlines; sleeping after each read
would incorrectly add delay per chunk and impose a fake bandwidth cap. This
does not emulate TCP congestion, packet loss, kernel ACKs, or SYN handshakes.
"""
import asyncio
import contextlib
import time


class LatencyProxy:
    def __init__(self, host, port, rtt_ms):
        self.host, self._target = host, port
        self.port = None
        self.delay = rtt_ms / 2000
        self.server = None
        self.tasks = set()
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def forward(self, reader, writer):
        queue = asyncio.Queue(maxsize=64)

        async def produce():
            while True:
                data = await reader.read(65536)
                await queue.put((time.monotonic() + self.delay, data))
                if not data:
                    return

        async def consume():
            while True:
                deadline, data = await queue.get()
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                if not data:
                    if writer.can_write_eof():
                        writer.write_eof()
                    return
                writer.write(data)
                await writer.drain()

        tasks = [asyncio.create_task(produce()), asyncio.create_task(consume())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote = None
        forwarding = []
        try:
            backend_reader, remote = await asyncio.open_connection(self.host, self.port_target)
            self.writers.update((writer, remote))
            forwarding = [asyncio.create_task(self.forward(reader, remote)),
                          asyncio.create_task(self.forward(backend_reader, writer))]
            await asyncio.gather(*forwarding)
        except (ConnectionError, OSError):
            pass
        finally:
            for child in forwarding:
                child.cancel()
            await asyncio.gather(*forwarding, return_exceptions=True)
            for output in (writer, remote):
                if output:
                    output.close()
                    with contextlib.suppress(OSError):
                        await output.wait_closed()
                    self.writers.discard(output)
            self.tasks.discard(task)

    @property
    def port_target(self):
        return self._target

    async def close(self):
        if self.server:
            self.server.close()
        # Python 3.12+ can wait for accepted transports in wait_closed().
        # Stop active forwarding before awaiting that server barrier.
        tasks, writers = list(self.tasks), list(self.writers)
        for writer in writers:
            writer.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.difference_update(tasks)
        self.writers.difference_update(writers)
        if self.server:
            await self.server.wait_closed()
