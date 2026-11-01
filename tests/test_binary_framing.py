"""GR1 binary framing: exact wire bytes and safety at every chunk boundary."""
import itertools
import struct
import unittest

from guac_bridge.binary_framing import Decoder, MAX_FRAME, encode_event


class BinaryFramingTests(unittest.TestCase):
    def test_fixed_wire_vectors(self):
        self.assertEqual(encode_event(dict(type='open',id='1')).hex(), '475201020000000100000000')
        event = dict(type='data', id=str(0x1b1d1234), body=b'\x1d\x1b\0')
        self.assertEqual(encode_event(event).hex(), '475201041d3b1d3d1234000000031d3d1d3b00')
        ready = dict(type='ready', flow=dict(window=32,frame_size=65536))
        self.assertEqual(encode_event(ready).hex(), '4752010100000000000000080000002000010000')

    def test_all_bytes_and_every_fragment_size(self):
        payload = bytes(range(256))*256 + b'\x1b]482203;\x07'
        event = dict(type='data',id='1',body=payload)
        wire = encode_event(event)
        self.assertNotIn(b'\x1b',wire)
        for size in (1,2,7,3072,6048,16384,len(wire)):
            decoder, events = Decoder(), []
            for offset in range(0,len(wire),size):
                events.extend(decoder.feed(wire[offset:offset+size]))
            self.assertEqual(events,[event])
            self.assertTrue(decoder.complete)

    def test_adjacent_escapes_do_not_decode_twice(self):
        for values in itertools.product((0x1b,0x1d,0x3b,0x3d),repeat=4):
            event=dict(type='data',id='2',body=bytes(values))
            self.assertEqual(Decoder().feed(encode_event(event)),[event])

    def test_coalesced_controls_unicode_and_ready(self):
        events=[dict(type='credit',id='7'),dict(type='eof',id='7'),
                dict(type='error',id='7',message='Grüße 😀\x1b'),
                dict(type='ready',id='0',flow=dict(window=32,frame_size=65536))]
        decoder=Decoder()
        self.assertEqual(decoder.feed(b''.join(map(encode_event,events))),events)
        self.assertTrue(decoder.complete)

    def test_reject_bad_magic_version_kind_escape_and_lengths(self):
        header=struct.Struct('!2sBBII')
        samples=[b'\x1b',b'\x1d\0',header.pack(b'XX',1,4,1,0),
                 header.pack(b'GR',2,4,1,0),header.pack(b'GR',1,0,1,0),
                 header.pack(b'GR',1,4,1,MAX_FRAME+1),
                 header.pack(b'GR',1,5,1,1)+b'a',
                 header.pack(b'GR',1,1,0,0)]
        for wire in samples:
            with self.subTest(wire=wire),self.assertRaises(ValueError):
                Decoder().feed(wire)
        for wire in [b'G',b'\x1d',encode_event(dict(type='data',id='1',body=b'abc'))[:-1]]:
            decoder=Decoder();decoder.feed(wire)
            self.assertFalse(decoder.complete)

    def test_size_and_payload_types(self):
        for event in [dict(type='data',id='1',body='text'),
                      dict(type='data',id='1',body=b'a'*(MAX_FRAME+1)),
                      dict(type='open',id='-1'),dict(type='open',id=str(2**32))]:
            with self.assertRaises(ValueError):
                encode_event(event)


if __name__ == '__main__':
    unittest.main()
