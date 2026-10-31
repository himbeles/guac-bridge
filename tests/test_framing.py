"""Protocol records stay ASCII and cannot inject terminal controls or delimiters."""
import base64
import json
import math
import os
import unittest

from guac_bridge.framing import encode_event, decode_event


class FramingTests(unittest.TestCase):
    def test_unicode_and_all_control_bytes_are_escaped_on_one_line(self):
        event = dict(type='data', id='Grüße 😀', text=''.join(map(chr, range(256))) + '\u2028\u2029\x1b]482203;\x07')
        encoded = encode_event(event)
        self.assertTrue(encoded.isascii())
        self.assertEqual(encoded.count(b'\n'), 1)
        self.assertTrue(encoded.endswith(b'\n'))
        self.assertFalse(any(byte < 32 or byte == 127 for byte in encoded[:-1]))
        self.assertEqual(decode_event(encoded[:-1]), event)

    def test_random_binary_payloads_and_large_request_round_trip(self):
        for size in (0, 4096, 16384, 32768, 1024 * 1024):
            raw = os.urandom(size)
            event = dict(type='data', body=base64.b64encode(raw).decode())
            self.assertEqual(base64.b64decode(decode_event(encode_event(event)[:-1])['body']), raw)

    def test_malformed_and_non_ascii_records_are_rejected(self):
        for line in (b'', b'{', b'[]', b'null', b'{}', b'{"type":1}',
                     b'{"type":"x"}\n{"type":"y"}', b'{"type":"\x1b"}',
                     b'{"type":"\xff"}', b'{"type":"\x7f"}', b'{"type":"x","n":NaN}',
                     b'{"type":"x","n":Infinity}', b'{"type":"x"}\r'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                decode_event(line)

    def test_non_finite_numbers_cannot_be_encoded(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                encode_event(dict(type='data', value=value))

    def test_codec_saves_about_a_quarter_of_terminal_bytes(self):
        event = dict(type='data', id='a' * 32, body=base64.b64encode(os.urandom(16384)).decode())
        current = encode_event(event)
        previous = base64.b64encode(json.dumps(event, separators=(',', ':')).encode()) + b'\n'
        self.assertGreater(1 - len(current) / len(previous), .249)
        self.assertLess(1 - len(current) / len(previous), .251)


class FramingStreamTests(unittest.IsolatedAsyncioTestCase):
    async def run_stream(self, terminal_bytes, *, expect_error=False):
        import asyncio
        from types import SimpleNamespace
        from guac_bridge import __version__
        from guac_bridge.client import Bridge, instruction
        from guac_bridge.flow import PROTOCOL_VERSION
        bridge = Bridge(SimpleNamespace(connect_timeout=1), None, '', '1', 'c', 'mysql', 'token')
        expected = dict(type='data', id='a', body=base64.b64encode(bytes(range(256))).decode(),
                        note='Grüße 😀 \n\x1b]482203;\x07')
        bridge.requests['a'] = asyncio.Queue(maxsize=40)
        if terminal_bytes is None:
            terminal_bytes = encode_event(dict(type='ready', version=PROTOCOL_VERSION,
                package_version=__version__, flow=bridge.flow)) + encode_event(expected)
        # Split JSON records inside fields and escape sequences. Split the
        # enclosing Guacamole instruction stream again into individual chars.
        pieces = [terminal_bytes[i:i + 7] for i in range(0, len(terminal_bytes), 7)]
        stream = instruction('pipe', '1', 'text/plain', 'LLM') + ''.join(
            instruction('blob', '1', base64.b64encode(piece).decode()) for piece in pieces)
        class Tunnel:
            async def send(self, *args): pass
            async def messages(self):
                for char in stream:
                    yield char
                await asyncio.Event().wait()
        bridge.tunnel = Tunnel()
        task = asyncio.create_task(bridge.read_loop())
        try:
            if expect_error:
                await asyncio.wait_for(bridge.failed.wait(), 2)
                self.assertIn('Unsupported server helper framing', bridge.failure)
            else:
                await asyncio.wait_for(bridge.ready.wait(), 2)
                self.assertEqual(await asyncio.wait_for(bridge.requests['a'].get(), 2), expected)
                self.assertFalse(bridge.failed.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_fragmented_and_coalesced_records_survive_both_framing_layers(self):
        await self.run_stream(None)

    async def test_old_base64_helper_framing_is_rejected_with_update_instructions(self):
        await self.run_stream(base64.b64encode(b'{"type":"ready","version":3}') + b'\n', expect_error=True)
