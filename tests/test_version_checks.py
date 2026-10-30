"""Matching helper versions are required before a bridge becomes ready."""
import subprocess
import sys
from types import SimpleNamespace
import unittest

from guac_bridge import __version__
from guac_bridge.client import Bridge
from guac_bridge.errors import BridgeError
from guac_bridge.ssh_client import SSHBridge


class VersionChecks(unittest.TestCase):
    def bridge(self, cls):
        return cls(SimpleNamespace(connect_timeout=1), None, '', '1', 'c', 'mysql', 'token')

    def test_matching_version_becomes_ready_for_both_bridges(self):
        for cls in (Bridge, SSHBridge):
            bridge = self.bridge(cls)
            bridge.dispatch(dict(type='ready', version=1, protocol='ssh-tcp', package_version=__version__))
            self.assertTrue(bridge.ready.is_set())

    def test_mismatch_or_missing_version_has_exact_server_install_command(self):
        for cls in (Bridge, SSHBridge):
            for remote in ('0.4.2', None, 123, 'invalid\n\x1b[31m'):
                with self.subTest(cls=cls.__name__, remote=remote):
                    bridge = self.bridge(cls)
                    event = dict(type='ready', version=1, protocol='ssh-tcp')
                    if remote is not None:
                        event['package_version'] = remote
                    with self.assertRaises(BridgeError) as caught:
                        bridge.dispatch(event)
                    message = str(caught.exception)
                    self.assertIn('client version: ' + __version__, message)
                    self.assertIn(f'uv tool install --force guac-bridge=={__version__}', message)
                    self.assertIn(bridge.helper_name, message)
                    self.assertIn('--server-command', message)
                    self.assertNotIn('\x1b', message)
                    self.assertFalse(bridge.ready.is_set())

    def test_cli_versions_are_available_without_connection_or_stdio_arguments(self):
        for module in ('client', 'ssh_client', 'server', 'ssh_server'):
            result = subprocess.run([sys.executable, '-m', 'guac_bridge.' + module, '--version'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(__version__, result.stdout)
