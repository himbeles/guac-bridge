"""Optional JSON CPU experiment; no production dependencies or codec changes.

uv run --with orjson --with msgspec python -m benchmarks.json_codecs --output results.json
"""
import argparse
import base64
import gc
import json
import platform
import statistics
import time
from pathlib import Path

import msgspec
import orjson
from guac_bridge import framing


def ordinary(value):
    """Conservative fast subset: avoid serializer-specific coercions/types."""
    kind = type(value)
    if kind in (str, bool) or value is None:
        return True
    if kind is int:
        return -(2**63) <= value < 2**63
    if kind in (list, tuple):
        return all(ordinary(item) for item in value)
    if kind is dict:
        return all(type(key) is str and ordinary(item) for key, item in value.items())
    return False


class Codec:
    def __init__(self, name, fast=True):
        self.name = name
        self.fast = fast
        self.dumps = orjson.dumps if name == 'orjson' else msgspec.json.Encoder().encode
        self.loads = orjson.loads if name == 'orjson' else msgspec.json.Decoder().decode

    def encode(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            return framing.encode_event(event)
        if not ordinary(event):
            return framing.encode_event(event)
        try:
            result = self.dumps(event)
        except (TypeError, ValueError, UnicodeError):
            return framing.encode_event(event)
        # Both serializers escape C0 controls as required by JSON. Only UTF-8
        # and raw DEL remain to check on their generated output. Incoming
        # untrusted records still receive the full printable-byte check.
        unsafe = ((not result.isascii() or b'\x7f' in result) if self.fast else
                  result.translate(None, framing._PRINTABLE))
        if unsafe:
            return framing.encode_event(event)
        return result + b'\n'

    def decode(self, line):
        if not isinstance(line, (bytes, bytearray)) or not line or line.translate(None, framing._PRINTABLE):
            raise ValueError('Invalid ASCII protocol record')
        try:
            value = self.loads(line)
        except (ValueError, UnicodeError):
            return framing.decode_event(line)
        # Large integers may be converted to floats by a candidate decoder.
        # Float-containing records use the reference implementation, including
        # finite JSON exponent values and unusually large numeric metadata.
        if not ordinary(value):
            return framing.decode_event(line)
        if not isinstance(value, dict) or not isinstance(value.get('type'), str):
            raise ValueError('Protocol event must be an object with a type')
        return value


def codecs(fast=True):
    return {'stdlib': (framing.encode_event, framing.decode_event),
            **{name: (codec.encode, codec.decode)
               for name in ('orjson', 'msgspec') for codec in [Codec(name, fast)]}}


def conformance():
    events = [dict(type='credit', id='abc', count=32),
              dict(type='data', text=''.join(map(chr, range(256))) + '😀\ud800'),
              dict(type='x', nested=[None, True, -1, 2**80, 1.25, {'a': '\x1b'}]),
              dict(type='x', **{'ü': 'text'})]
    invalid = [b'', b'[]', b'null', b'{}', b'{"type":1}', b'{',
               b'{"type":"x"}\n', b'{"type":"\x7f"}', b'{"type":"\xff"}',
               b'{"type":"x","n":NaN}', b'{"type":"x","n":Infinity}']
    count = 0
    for name, (encode, decode) in codecs().items():
        for event in events:
            raw = encode(event)
            assert raw == framing.encode_event(event), (name, event)
            assert decode(raw[:-1]) == event, name
            count += 1
        for raw in invalid:
            try:
                decode(raw)
            except ValueError:
                count += 1
            else:
                raise AssertionError((name, raw))
        for value in (float('nan'), float('inf'), -float('inf')):
            try:
                encode(dict(type='x', nested=[value]))
            except ValueError:
                count += 1
            else:
                raise AssertionError(name)
    return count


def micro(repeats=5, seconds=.15):
    cases = {'credit': dict(type='credit', id='a'*32, count=32),
             **{f'data-{size}': dict(type='data', id='a'*32,
                  body=base64.b64encode(b'x'*size).decode()) for size in (4096, 16384, 32768)},
             'unicode': dict(type='error', message='Grüße 😀\x1b\x7f')}
    rows = []
    available = codecs()
    for case, event in cases.items():
        raw = framing.encode_event(event)[:-1]
        for operation in ('encode', 'decode', 'roundtrip'):
            for repeat in range(repeats):
                order = list(available)
                if repeat % 2:
                    order.reverse()
                for name in order:
                    encode, decode = available[name]
                    fn = ((lambda: encode(event)) if operation == 'encode' else
                          (lambda: decode(raw)) if operation == 'decode' else
                          (lambda: decode(encode(event)[:-1])))
                    for _ in range(100):
                        fn()
                    gc.disable()
                    start = time.process_time_ns()
                    count = 0
                    while time.process_time_ns() - start < seconds * 1e9:
                        for _ in range(100):
                            fn()
                        count += 100
                    elapsed = time.process_time_ns() - start
                    gc.enable()
                    rows.append(dict(case=case, operation=operation, codec=name,
                                     repeat=repeat, ns_per_event=elapsed/count,
                                     events=count, cpu_ns=elapsed, wire_bytes=len(raw)+1))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = dict(python=platform.python_version(), platform=platform.platform(),
                  orjson=orjson.__version__, msgspec=msgspec.__version__,
                  encoder_output_check='isascii+DEL; serializers escape C0 controls',
                  conformance_checks=conformance(), micro=micro())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    for case in sorted({r['case'] for r in result['micro']}):
        subset = [r for r in result['micro'] if r['case'] == case and r['operation'] == 'roundtrip']
        med = {name: statistics.median(r['ns_per_event'] for r in subset if r['codec'] == name)
               for name in codecs()}
        print(case, {name: round(med['stdlib']/cost, 2) for name, cost in med.items()}, flush=True)


if __name__ == '__main__':
    main()
