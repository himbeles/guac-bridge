"""Python GR1 lifecycle plus cross-language byte compatibility."""
import asyncio
import sys
import unittest

import test_rust_ssh as rust_tests
from benchmarks.rust_ssh import BridgeWorker


class PythonBinaryBridgeTests(rust_tests.RustBridgeTests):
    __unittest_skip__ = not sys.platform.startswith('linux')
    async def connect(self, target, variant='rust', timeout=30):
        variant = {'rust':'python-binary','rust-matched':'python-binary-matched'}.get(variant,variant)
        bridge = BridgeWorker(variant,timeout=timeout)
        self.bridges.append(bridge)
        return await bridge.start(target)


@unittest.skipUnless(sys.platform.startswith('linux'), 'PTY integration requires Linux')
class BinaryInteropTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rust_tests.RustBridgeTests.asyncSetUp
    asyncTearDown = rust_tests.RustBridgeTests.asyncTearDown
    target = rust_tests.RustBridgeTests.target

    async def transfer(self, variant, helper_language=None, transport='websocket'):
        async def echo(reader,writer):
            payload=await reader.read()
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
        target=await self.target(echo)
        bridge=BridgeWorker(variant,helper_language=helper_language,transport=transport)
        self.bridges.append(bridge)
        await bridge.start(target)
        payload=bytes(range(256))*4096+b'\x1b]482203;\x07'
        reader,writer=await asyncio.open_connection('127.0.0.1',bridge.port)
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        self.assertEqual(await asyncio.wait_for(reader.read(),15),payload)
        writer.close()
        await writer.wait_closed()
        await bridge.close()
        self.bridges.remove(bridge)
        self.assertEqual(bridge.child.returncode,0)
        self.assertEqual(bridge.helper_usage['helper_exit'],0)

    @unittest.skipUnless(rust_tests.BINARY.exists(), 'Build the Rust prototype first')
    async def test_python_client_rust_helper(self):
        await self.transfer('python-binary',helper_language='rust')

    @unittest.skipUnless(rust_tests.BINARY.exists(), 'Build the Rust prototype first')
    async def test_rust_client_python_helper(self):
        await self.transfer('rust',helper_language='python')

    async def test_python_binary_http_fallback(self):
        await self.transfer('python-binary',transport='http')


if __name__ == '__main__':
    unittest.main()
