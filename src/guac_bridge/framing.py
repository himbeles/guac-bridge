"""Protocol-v4 records: compact ASCII JSON, one object per terminal line."""
import json

_PRINTABLE = bytes(range(32, 127))


def encode_event(event):
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        raise ValueError('Protocol event must be an object with a type')
    # Escaping is deliberate: no raw Unicode, newlines, ESC/OSC or terminal
    # controls may appear in the terminal stream, including inside strings.
    return json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii') + b'\n'


def _reject_constant(value):
    raise ValueError('Non-finite protocol number')


def decode_event(line):
    if not isinstance(line, (bytes, bytearray)) or not line or line.translate(None, _PRINTABLE):
        raise ValueError('Invalid ASCII protocol record')
    try:
        event = json.loads(line.decode('ascii'), parse_constant=_reject_constant)
    except (ValueError, UnicodeError):
        raise ValueError('Invalid JSON protocol record') from None
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        raise ValueError('Protocol event must be an object with a type')
    return event
