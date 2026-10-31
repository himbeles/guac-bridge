"""Bounded flow-control profile, selected explicitly by each new request/channel."""
CAPABILITY = 'flow-control-v1'
LEGACY_WINDOW = 8
WINDOW = 32
LEGACY_CHUNK = 4096
CHUNK = 16384


def request_limits(event, *, ssh=False):
    # Missing fields are the old-client contract, even with a new helper.
    window = event.get('window', LEGACY_WINDOW)
    chunk = event.get('chunk', LEGACY_CHUNK) if ssh else LEGACY_CHUNK
    if type(window) is not int or window not in (LEGACY_WINDOW, WINDOW):
        raise ValueError('Unsupported data window')
    if type(chunk) is not int or chunk not in (LEGACY_CHUNK, CHUNK):
        raise ValueError('Unsupported SSH frame size')
    return window, chunk
