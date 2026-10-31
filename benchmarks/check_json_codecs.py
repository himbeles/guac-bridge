"""Run existing framing regressions against optional benchmark adapters."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from guac_bridge import client
from .json_codecs import codecs, conformance


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
    import test_framing
    print(f'{conformance()} conformance checks passed', flush=True)
    for name, (encode, decode) in codecs().items():
        print(f'Framing and fragmented Guacamole streams: {name}', flush=True)
        with patch.object(test_framing, 'encode_event', encode), \
             patch.object(test_framing, 'decode_event', decode), \
             patch.object(client, 'encode_event', encode), \
             patch.object(client, 'decode_event', decode):
            suite = unittest.defaultTestLoader.loadTestsFromModule(test_framing)
            if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
                raise SystemExit(1)


if __name__ == '__main__':
    main()
