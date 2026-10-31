"""Fixed fast limits and rejection of old protocols."""
import unittest
from types import SimpleNamespace
from guac_bridge import __version__
from guac_bridge.client import Bridge, BridgeError
from guac_bridge.ssh_client import SSHBridge


class FlowTests(unittest.TestCase):
    def test_fixed_fast_profile(self):
        for cls in (Bridge, SSHBridge):
            bridge = cls(SimpleNamespace(), None, '', '1', 'c', 'mysql', 'token')
            bridge.dispatch(dict(type='ready', version=2, protocol='ssh-tcp', package_version=__version__))
            self.assertTrue(bridge.ready.is_set())
            self.assertEqual((bridge.input_window, bridge.data_window, bridge.tcp_chunk), (32, 32, 16384))

    def test_old_or_missing_protocol_is_rejected(self):
        for cls in (Bridge, SSHBridge):
            for version in (None, 1, 99):
                bridge = cls(SimpleNamespace(), None, '', '1', 'c', 'mysql', 'token')
                with self.assertRaisesRegex(BridgeError, 'uv tool install --force guac-bridge=='):
                    bridge.dispatch(dict(type='ready', version=version, protocol='ssh-tcp', package_version=__version__))
                self.assertFalse(bridge.ready.is_set())
