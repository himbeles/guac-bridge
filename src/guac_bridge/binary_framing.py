"""Experimental GR1 framing shared on the wire with the native Rust prototype."""
import re
import struct

HEADER = struct.Struct('!2sBBII')
MAGIC = b'GR'
VERSION = 1
MAX_FRAME = 262144
KINDS = ('', 'ready', 'open', 'opened', 'data', 'credit', 'eof', 'close',
         'closed', 'error', 'heartbeat', 'shutdown')
OPCODES = {name: number for number, name in enumerate(KINDS) if name}
_BAD_ESCAPE = re.compile(b'\x1d(?![\x3b\x3d])')


def encode_event(event):
    kind = event['type']
    opcode = OPCODES[kind]
    ident = int(event.get('id', 0))
    if not 0 <= ident <= 0xffffffff:
        raise ValueError('Invalid channel ID')
    if kind == 'data':
        body = event['body']
        if not isinstance(body, bytes):
            raise ValueError('Binary data must be bytes')
    elif kind == 'ready':
        flow = event['flow']
        body = struct.pack('!II', flow['window'], flow['frame_size'])
    elif kind == 'error':
        body = event['message'].encode('utf-8')
    else:
        body = b''
    if len(body) > MAX_FRAME:
        raise ValueError('Binary frame exceeds limit')
    raw = HEADER.pack(MAGIC, VERSION, opcode, ident, len(body)) + body
    # Escape GS before inserting any new GS bytes for ESC. Both passes run in C.
    return raw.replace(b'\x1d', b'\x1d\x3d').replace(b'\x1b', b'\x1d\x3b')


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
        # Restore ESC first, so restored GS bytes cannot form a new escape.
        self.buffer.extend(data.replace(b'\x1d\x3b', b'\x1b').replace(b'\x1d\x3d', b'\x1d'))
        if len(self.buffer) > 2 * MAX_FRAME:
            raise ValueError('Binary input exceeds limit')
        events, offset = [], 0
        while len(self.buffer) - offset >= HEADER.size:
            magic, version, opcode, ident, size = HEADER.unpack_from(self.buffer, offset)
            if magic != MAGIC or version != VERSION or not 1 <= opcode < len(KINDS):
                raise ValueError('Wrong binary helper protocol')
            if size > MAX_FRAME:
                raise ValueError('Binary frame exceeds limit')
            if len(self.buffer) - offset < HEADER.size + size:
                break
            kind = KINDS[opcode]
            body = bytes(self.buffer[offset+HEADER.size:offset+HEADER.size+size])
            event = dict(type=kind, id=str(ident))
            if kind == 'data':
                event['body'] = body
            elif kind == 'ready':
                if size != 8 or ident != 0:
                    raise ValueError('Invalid ready frame')
                window, frame_size = struct.unpack('!II', body)
                event['flow'] = dict(window=window, frame_size=frame_size)
            elif kind == 'error':
                event['message'] = body.decode('utf-8')
            elif body:
                raise ValueError('Unexpected control payload')
            events.append(event)
            offset += HEADER.size + size
        del self.buffer[:offset]
        return events

    @property
    def complete(self):
        return not self.buffer and not self.escaped
