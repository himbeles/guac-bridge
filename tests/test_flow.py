"""Capability selection and bounded settings, independent of package versions."""
import unittest
from types import SimpleNamespace
from guac_bridge import __version__
from guac_bridge.client import Bridge
from guac_bridge.ssh_client import SSHBridge
from guac_bridge.flow import CAPABILITY, request_limits


class FlowTests(unittest.TestCase):
    def test_capability_selects_fast_profile_and_absence_keeps_legacy(self):
        for cls in (Bridge, SSHBridge):
            for caps in ([], [CAPABILITY], None, CAPABILITY):
                bridge = cls(SimpleNamespace(), None, '', '1', 'c', 'mysql', 'token')
                bridge.dispatch(dict(type='ready', version=1, protocol='ssh-tcp',
                                     package_version=__version__, capabilities=caps))
                fast = caps == [CAPABILITY]
                self.assertEqual(bridge.input_window, 32 if fast else 8)
                self.assertEqual(bridge.data_window, 32 if fast else 8)
                self.assertEqual(bridge.tcp_chunk, 16384 if fast else 4096)
                self.assertGreater(bridge.response_queue_size, bridge.data_window)

    def test_old_client_defaults_and_new_client_selection(self):
        self.assertEqual(request_limits({}, ssh=True), (8, 4096))
        self.assertEqual(request_limits({}), (8, 4096))
        self.assertEqual(request_limits(dict(window=32, chunk=16384), ssh=True), (32, 16384))
        self.assertEqual(request_limits(dict(window=32)), (32, 4096))

    def test_invalid_or_unbounded_limits_are_rejected(self):
        for field, values in [('window', (0, -1, 33, 1000000, True, '32', None)),
                              ('chunk', (0, -1, 16385, 1000000, True, '16384', None))]:
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        request_limits({field: value}, ssh=True)
