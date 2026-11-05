"""Controlled RDP negotiation and binary traffic through a bridge; no desktop server required."""
import asyncio


async def verify_rdp_passthrough(test, bridge):
    # TPKT/X.224 connection request and confirm with RDP security negotiation.
    request = bytes.fromhex('03 00 00 13 0e e0 00 00 00 00 00 01 00 08 00 03 00 00 00')
    response = bytes.fromhex('03 00 00 13 0e d0 00 00 12 34 00 02 00 08 00 02 00 00 00')
    payload = bytes(range(256)) * 2048
    received = asyncio.Queue()

    async def service(reader, writer):
        try:
            header = await reader.readexactly(len(request))
            writer.write(response)
            await writer.drain()
            data = await reader.read()
            await received.put((header, data))
            writer.write(data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(service, '127.0.0.1', 0)
    async with server:
        port = await bridge(server.sockets[0].getsockname()[1])
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(request)
            await writer.drain()
            test.assertEqual(await asyncio.wait_for(reader.readexactly(len(response)), 10), response)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            test.assertEqual(await asyncio.wait_for(reader.read(), 15), payload)
            test.assertEqual(await asyncio.wait_for(received.get(), 5), (request, payload))
        finally:
            writer.close()
            await writer.wait_closed()
