"""Actionable local SSH listener errors without unrelated network diagnostics."""
import asyncio
import errno
import socket
import unittest
from unittest.mock import AsyncMock, patch

from guac_bridge.errors import BridgeError
from guac_bridge.ssh_client import start_listener


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_occupied_port_reports_address_and_remedies(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with self.assertRaises(BridgeError) as caught:
                await start_listener(lambda reader, writer: writer.close(), port)
        message = str(caught.exception)
        self.assertIn(f'127.0.0.1:{port}', message)
        self.assertIn('already in use', message)
        self.assertIn(f'guac-ssh-client --stop --port {port}', message)
        self.assertIn('different local port with --port', message)
        self.assertNotIn('TLS', message)

    async def test_windows_address_in_use_error_is_recognized(self):
        error = OSError(10048, 'RAW-ERROR-TEXT')
        error.winerror = 10048
        with patch('guac_bridge.ssh_client.asyncio.start_server', AsyncMock(side_effect=error)):
            with self.assertRaisesRegex(BridgeError, '127.0.0.1:2223: this local port is already in use'):
                await start_listener(None, 2223)

    async def test_access_denied_identifies_reservation_or_permissions(self):
        for code in (errno.EACCES, errno.EPERM, 10013):
            with self.subTest(code=code):
                with patch('guac_bridge.ssh_client.asyncio.start_server', AsyncMock(side_effect=OSError(code, 'denied'))):
                    with self.assertRaises(BridgeError) as caught:
                        await start_listener(None, 2222)
                self.assertIn('denied access', str(caught.exception))
                self.assertIn('reserved or blocked', str(caught.exception))
                self.assertNotIn('already in use', str(caught.exception))

    async def test_unknown_error_retains_code_without_raw_exception_text(self):
        with patch('guac_bridge.ssh_client.asyncio.start_server', AsyncMock(side_effect=OSError(12345, 'RAW-ERROR-TEXT'))):
            with self.assertRaises(BridgeError) as caught:
                await start_listener(None, 2222)
        self.assertIn('127.0.0.1:2222 (OS error 12345)', str(caught.exception))
        self.assertNotIn('RAW-ERROR-TEXT', str(caught.exception))

    async def test_free_port_returns_working_listener(self):
        listener = await start_listener(lambda reader, writer: writer.close(), 0)
        try:
            port = listener.sockets[0].getsockname()[1]
            _, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.close()
            await writer.wait_closed()
        finally:
            listener.close()
            await listener.wait_closed()
