"""Shared defaults and bounded transport tuning."""
import argparse

PROTOCOL_VERSION = 4
WINDOW = 32
CHUNK = 16384


def window_size(value):
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('window must be an integer from 1 to 64') from None
    if not 1 <= size <= 64:
        raise argparse.ArgumentTypeError('window must be from 1 to 64')
    return size


def add_flow_arguments(parser, *, ssh=False):
    group = parser.add_argument_group('Transport tuning',
        'Defaults favor bulk throughput on delayed connections. Larger values use more buffering; '
        'they do not speed up model inference or reduce network round-trip time.' +
        (' Stop and restart an existing managed bridge to apply changes.' if ssh else ''))
    group.add_argument('--window', type=window_size, default=WINDOW, metavar='N',
        help='Chunks per request/channel and terminal input blobs allowed in flight before waiting '
             'for acknowledgements (1-64; default: 32). Larger windows keep delayed links busy; '
             'smaller windows reduce memory use and queued bursts.')
    if ssh:
        group.add_argument('--frame-size', type=int, choices=(4096, 8192, 16384, 32768), default=CHUNK,
            metavar='BYTES', help='Maximum SSH data bytes per bridge message (4096, 8192, 16384, 32768; '
            'default: 16384). Larger frames reduce framing/encoding work during bulk transfers; '
            'smaller frames reduce burst size. This is not the network packet size. '
            'Raw data allowance per channel/direction is approximately window x frame-size.')


def settings(args, *, ssh=False):
    result = {'window': getattr(args, 'window', WINDOW)}
    if ssh:
        result['frame_size'] = getattr(args, 'frame_size', CHUNK)
    return result
