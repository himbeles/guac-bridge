"""Representative transport sizes, validation and helper confirmation."""
import argparse
import unittest
from types import SimpleNamespace
from guac_bridge import __version__
from guac_bridge.client import Bridge, BridgeError
from guac_bridge.ssh_client import SSHBridge
from guac_bridge.flow import PROTOCOL_VERSION, add_flow_arguments


class FlowTests(unittest.TestCase):
    def test_three_representative_profiles(self):
        for argv, window, frame in [([], 32, 16384),
                (['--window', '8', '--frame-size', '4096'], 8, 4096),
                (['--window', '64', '--frame-size', '32768'], 64, 32768)]:
            parser = argparse.ArgumentParser()
            add_flow_arguments(parser, ssh=True)
            args = parser.parse_args(argv)
            bridge = SSHBridge(args, None, '', '1', 'c', 'mysql', 'token')
            bridge.dispatch(dict(type='ready', version=PROTOCOL_VERSION, protocol='ssh-tcp',
                                 package_version=__version__, flow=bridge.flow))
            self.assertTrue(bridge.ready.is_set())
            self.assertEqual((bridge.input_window, bridge.data_window, bridge.tcp_chunk), (window, window, frame))
            self.assertEqual(bridge.response_queue_size, window + 8)

    def test_old_protocol_or_unconfirmed_settings_are_rejected(self):
        for cls in (Bridge, SSHBridge):
            for version, flow in [(None, {}), (1, {}), (2, {}), (99, {}),
                                  (PROTOCOL_VERSION, {}), (PROTOCOL_VERSION, {'window': 8})]:
                bridge = cls(SimpleNamespace(), None, '', '1', 'c', 'mysql', 'token')
                with self.assertRaisesRegex(BridgeError, 'uv tool install --force guac-bridge=='):
                    bridge.dispatch(dict(type='ready', version=version, flow=flow,
                                         protocol='ssh-tcp', package_version=__version__))
                self.assertFalse(bridge.ready.is_set())

    def test_invalid_sizes_fail_at_argument_parsing(self):
        import contextlib
        import io
        parser = argparse.ArgumentParser()
        add_flow_arguments(parser, ssh=True)
        for argv in (['--window', '0'], ['--window', '65'], ['--window', 'x'], ['--frame-size', '65536']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)
