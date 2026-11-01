"""Protocol-v5 binary frames, safe for Guacamole terminal pipes.

Only ESC and GS are byte-stuffed. TCP/HTTP bodies remain raw bytes. JSON is
used for infrequent ready/request/header metadata, never for data frames.
"""
import json
import re
import struct

from .flow import PROTOCOL_VERSION

HEADER = struct.Struct('!2sBBII')
MAGIC = b'GB'
MAX_DATA = 65536
MAX_METADATA = 65536
MAX_REQUEST_BODY = 16 * 1024 * 1024
MAX_FRAME = MAX_REQUEST_BODY + MAX_METADATA + 4
KINDS = ('', 'ready', 'open', 'opened', 'data', 'credit', 'eof', 'close',
         'closed', 'error', 'heartbeat', 'shutdown', 'request', 'headers',
         'cancel', 'cancelled', 'end')
OPCODES = {kind: i for i, kind in enumerate(KINDS) if kind}
GLOBAL = {'ready', 'heartbeat', 'shutdown'}
_BAD_ESCAPE = re.compile(b'\x1d(?![\x3b\x3d])')


def _metadata(event):
    data = json.dumps({k: v for k, v in event.items() if k not in ('type', 'id', 'body')},
                      ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
    if len(data) > MAX_METADATA:
        raise ValueError('Metadata exceeds limit')
    return data


def encode_event(event):
    if not isinstance(event, dict) or event.get('type') not in OPCODES:
        raise ValueError('Invalid protocol event')
    kind = event['type']
    try:
        ident = int(event.get('id', 0))
    except (TypeError, ValueError):
        raise ValueError('Invalid channel ID') from None
    if not 0 <= ident <= 0xffffffff or (ident == 0) != (kind in GLOBAL):
        raise ValueError('Invalid channel ID')
    if kind in ('data', 'request'):
        body = event['body']
        if not isinstance(body, bytes):
            raise ValueError('Protocol body must be bytes')
        if len(body) > (MAX_DATA if kind == 'data' else MAX_REQUEST_BODY):
            raise ValueError('Protocol body exceeds limit')
        if kind == 'request':
            meta = _metadata(event)
            body = struct.pack('!I', len(meta)) + meta + body
    elif kind in ('ready', 'headers'):
        body = _metadata(event)
    elif kind == 'error':
        body = event['message'].encode('utf-8')
        if len(body) > MAX_METADATA:
            raise ValueError('Error metadata exceeds limit')
    else:
        body = b''
    raw = HEADER.pack(MAGIC, PROTOCOL_VERSION, OPCODES[kind], ident, len(body)) + body
    return raw.replace(b'\x1d', b'\x1d\x3d').replace(b'\x1b', b'\x1d\x3b')


def _reject_constant(value):
    raise ValueError('Non-finite protocol metadata')


def _decode_metadata(raw):
    meta = json.loads(raw, parse_constant=_reject_constant)
    if not isinstance(meta, dict) or any(k in meta for k in ('type', 'id', 'body')):
        raise ValueError('Invalid protocol metadata')
    return meta


class Decoder:
    def __init__(self):
        self.buffer = bytearray()
        self.escaped = False

    def feed(self, data):
        if b'\x1b' in data:
            raise ValueError('Unescaped terminal ESC')
        if self.escaped:
            data = b'\x1d' + data
        self.escaped = data.endswith(b'\x1d')
        if self.escaped:
            data = data[:-1]
        if _BAD_ESCAPE.search(data):
            raise ValueError('Invalid binary escape')
        self.buffer.extend(data.replace(b'\x1d\x3b', b'\x1b').replace(b'\x1d\x3d', b'\x1d'))
        if len(self.buffer) > MAX_FRAME + 65536:
            raise ValueError('Binary input exceeds limit')
        events, offset = [], 0
        while len(self.buffer) - offset >= HEADER.size:
            magic, version, opcode, ident, size = HEADER.unpack_from(self.buffer, offset)
            if magic != MAGIC or version != PROTOCOL_VERSION or not 1 <= opcode < len(KINDS):
                raise ValueError('Unsupported binary helper protocol')
            kind = KINDS[opcode]
            limit = MAX_FRAME if kind == 'request' else MAX_DATA if kind == 'data' else MAX_METADATA
            if size > limit or (ident == 0) != (kind in GLOBAL):
                raise ValueError('Invalid frame size or channel ID')
            if kind not in ('request','data','ready','headers','error') and size:
                raise ValueError('Unexpected control payload')
            if len(self.buffer) - offset < HEADER.size + size:
                break
            body = bytes(self.buffer[offset+HEADER.size:offset+HEADER.size+size])
            event = dict(type=kind)
            if kind not in GLOBAL:
                event['id'] = str(ident)
            if kind == 'data':
                event['body'] = body
            elif kind == 'request':
                if size < 4:
                    raise ValueError('Invalid request frame')
                meta_size = struct.unpack_from('!I', body)[0]
                if meta_size > MAX_METADATA or meta_size > size-4 or size-4-meta_size > MAX_REQUEST_BODY:
                    raise ValueError('Invalid request metadata/body sizes')
                event.update(_decode_metadata(body[4:4+meta_size]))
                event['body'] = body[4+meta_size:]
            elif kind in ('ready','headers'):
                event.update(_decode_metadata(body))
            elif kind == 'error':
                event['message'] = body.decode('utf-8')
            events.append(event)
            offset += HEADER.size + size
        del self.buffer[:offset]
        return events

    @property
    def complete(self):
        return not self.buffer and not self.escaped


def decode_event(data):
    decoder = Decoder()
    events = decoder.feed(data)
    if len(events) != 1 or not decoder.complete:
        raise ValueError('Expected one complete binary event')
    return events[0]


def read_events(stream):
    """Incremental bounded read; EOF inside a header/body/escape is an error."""
    decoder = Decoder()
    while True:
        data = stream.read1(16384)
        if not data:
            if not decoder.complete:
                raise ValueError('Truncated binary frame')
            return
        yield from decoder.feed(data)
