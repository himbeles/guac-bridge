"""Interactive consent defaults to yes only for an actual Enter response."""
import asyncio
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guac_bridge.errors import BridgeError
from guac_bridge.update import HelperProbe, offer_update


class FakeBridge:
    helper_name = 'guac-ssh-server'

    def __init__(self):
        self.args = SimpleNamespace(server_command='~/.local/bin/guac-ssh-server')

    async def wait_for(self, answer, timeout):
        return await answer


class UpdatePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_enter_yes_no_and_invalid_response(self):
        for values, expected in [([''], True), (['YES'], True), (['n'], False),
                                 (['invalid', 'no'], False)]:
            bridge = FakeBridge()
            probe = SimpleNamespace(result='mismatched', remote_version='0.5.0')
            output = io.StringIO()
            with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr', output), \
                 patch('builtins.input', side_effect=values):
                self.assertEqual(await offer_update(bridge, probe), expected)
            self.assertIn('Update server now? [Y/n]', output.getvalue())

    async def test_eof_declines_and_never_defaults_to_yes(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr', io.StringIO()), \
             patch('builtins.input', side_effect=EOFError):
            self.assertFalse(await offer_update(FakeBridge(), SimpleNamespace(result='mismatched', remote_version='0.5.0')))

    async def test_background_custom_path_and_noninteractive_never_prompt(self):
        for values, tty in [(dict(managed_id='child'), True), (dict(ensure_running=True), True),
                            (dict(server_command='/custom/helper'), True), ({}, False)]:
            bridge = FakeBridge()
            for key, value in values.items():
                setattr(bridge.args, key, value)
            with patch('sys.stdin.isatty', return_value=tty), patch('builtins.input') as question:
                self.assertFalse(await offer_update(bridge, SimpleNamespace(result='unsupported-options', remote_version='0.5.0')))
                question.assert_not_called()

    async def test_declining_does_not_relax_protocol_or_strict_checks(self):
        for result, strict, rejected in [('unsupported-options', False, True),
                                        ('mismatched', True, True), ('mismatched', False, False)]:
            bridge = FakeBridge()
            bridge.args.require_server_version = strict
            probe = HelperProbe(bridge)
            probe.result, probe.remote_version = result, '0.5.0'
            probe.ended()
            await probe.future
            if rejected:
                with self.assertRaises(BridgeError):
                    probe.check_result()
            else:
                probe.check_result()
