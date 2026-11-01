"""Bounded binary records preserve all bytes and cannot inject terminal ESC."""
import base64
import itertools
import json
import math
import os
import struct
import unittest

from guac_bridge.framing import (Decoder, HEADER, MAGIC, MAX_DATA, MAX_METADATA,
                                MAX_REQUEST_BODY, encode_event, decode_event)
from guac_bridge.flow import PROTOCOL_VERSION


class FramingTests(unittest.TestCase):
    def test_all_bytes_adjacent_escapes_and_fragmentation(self):
        event = dict(type='data', id='1', body=bytes(range(256))*128+b'\x1b]482203;\x07')
        wire = encode_event(event)
        self.assertNotIn(b'\x1b', wire)
        for size in (1,2,7,3072,6048,16384,len(wire)):
            decoder, result = Decoder(), []
            for offset in range(0,len(wire),size):
                result.extend(decoder.feed(wire[offset:offset+size]))
            self.assertEqual(result,[event])
            self.assertTrue(decoder.complete)
        for values in itertools.product((0x1b,0x1d,0x3b,0x3d),repeat=4):
            event=dict(type='data',id='2',body=bytes(values))
            self.assertEqual(decode_event(encode_event(event)),event)

    def test_request_and_metadata_round_trip(self):
        for size in (0,4096,65536,1024*1024):
            event=dict(type='request',id='1',method='POST',path='/v1/chat/completions',
                       headers={'x-request-id':'Grüße 😀\x1b'},body=os.urandom(size))
            self.assertEqual(decode_event(encode_event(event)),event)
        for event in [dict(type='ready',version=PROTOCOL_VERSION,package_version='0.10.0',flow={'window':32}),
                      dict(type='headers',id='1',status=200,headers={'x':'ü'}),
                      dict(type='error',id='1',message='😀\x1b'),
                      dict(type='shutdown'),dict(type='heartbeat')]:
            self.assertEqual(decode_event(encode_event(event)),event)

    def test_frame_limits_and_malformed_records(self):
        def header(kind,ident,size,version=PROTOCOL_VERSION):
            return HEADER.pack(MAGIC,version,kind,ident,size)
        for wire in [b'\x1b',b'\x1d\0',header(4,1,MAX_DATA+1),header(1,0,MAX_METADATA+1),
                     header(12,1,MAX_REQUEST_BODY+MAX_METADATA+5),header(5,1,1)+b'x',
                     header(4,0,0),header(0,1,0),header(4,1,0,4),header(12,1,3)+b'abc',
                     header(12,1,4)+struct.pack('!I',MAX_METADATA+1)]:
            with self.subTest(wire=wire),self.assertRaises(ValueError):
                decode_event(wire)
        for wire in [b'G',b'\x1d',encode_event(dict(type='data',id='1',body=b'abc'))[:-1]]:
            decoder=Decoder();decoder.feed(wire)
            self.assertFalse(decoder.complete)
            with self.assertRaises(ValueError):decode_event(wire)
        for event in [dict(type='data',id='1',body='text'),
                      dict(type='data',id='1',body=b'x'*(MAX_DATA+1)),
                      dict(type='credit',id='0'),dict(type='open',id=str(2**32))]:
            with self.assertRaises(ValueError):encode_event(event)

    def test_metadata_cannot_override_framing_or_contain_nan(self):
        for meta in [b'[]',b'{"type":"shutdown"}',b'{"id":"9"}',b'{"body":"x"}',b'{"n":NaN}']:
            wire=HEADER.pack(MAGIC,PROTOCOL_VERSION,13,1,len(meta))+meta
            with self.assertRaises(ValueError):decode_event(wire)
        for value in (math.nan,math.inf,-math.inf):
            with self.assertRaises(ValueError):encode_event(dict(type='ready',n=value))

    def test_binary_encoding_removes_payload_base64(self):
        raw=bytes(range(256))*64
        event=dict(type='data',id='1',body=raw)
        old=json.dumps(dict(type='data',id='a'*32,body=base64.b64encode(raw).decode()),separators=(',',':')).encode()+b'\n'
        self.assertGreater(1-len(encode_event(event))/len(old),.24)


class FramingStreamTests(unittest.IsolatedAsyncioTestCase):
    async def run_stream(self, terminal_bytes, *, expect_error=False):
        import asyncio
        from types import SimpleNamespace
        from guac_bridge import __version__
        from guac_bridge.client import Bridge, instruction
        from guac_bridge.flow import PROTOCOL_VERSION
        bridge = Bridge(SimpleNamespace(connect_timeout=1), None, '', '1', 'c', 'mysql', 'token')
        expected = dict(type='data', id='1', body=bytes(range(256)) + b'\x1b]482203;\x07')
        bridge.requests['1'] = asyncio.Queue(maxsize=40)
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
                self.assertIn('Unsupported or invalid binary helper protocol', bridge.failure)
            else:
                await asyncio.wait_for(bridge.ready.wait(), 2)
                self.assertEqual(await asyncio.wait_for(bridge.requests['1'].get(), 2), expected)
                self.assertFalse(bridge.failed.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_fragmented_and_coalesced_records_survive_both_framing_layers(self):
        await self.run_stream(None)

    async def test_old_base64_helper_framing_is_rejected_with_update_instructions(self):
        await self.run_stream(base64.b64encode(b'{"type":"ready","version":3}') + b'\n', expect_error=True)
