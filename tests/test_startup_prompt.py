"""Consent is returned to a managed child, never inferred from a missing console."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guac_bridge import background, startup_prompt
from guac_bridge.errors import BridgeError


class Bridge:
    def __init__(self):
        self.args = SimpleNamespace(port=2223, managed_id='a' * 32)
    async def wait_for(self, operation, timeout):
        return await asyncio.wait_for(operation, timeout)


class StartupPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(background, 'default_log', return_value=Path(self.temp.name) / 'ssh.log')
        self.patch.start()
    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_yes_and_no_reach_child_and_files_are_removed(self):
        for approved in (True, False):
            bridge = Bridge()
            task = asyncio.create_task(startup_prompt.request_update(bridge, SimpleNamespace(remote_version='0.5.0')))
            request, response = startup_prompt.paths(2223, bridge.args.managed_id)
            async with asyncio.timeout(3):
                while not request.exists():
                    await asyncio.sleep(.01)
            with patch('guac_bridge.hook_auth.confirm_hook_update', return_value=approved) as question:
                self.assertEqual(startup_prompt.service_request(bridge.args, bridge.args.managed_id), approved)
            question.assert_called_once()
            self.assertEqual(await task, approved)
            self.assertFalse(request.exists())
            self.assertFalse(response.exists())

    async def test_cancelled_approval_does_not_leave_request_or_approve_update(self):
        bridge = Bridge()
        task = asyncio.create_task(startup_prompt.request_update(bridge, SimpleNamespace(remote_version='0.5.0')))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(any(Path(self.temp.name).iterdir()))

    async def test_wrong_startup_identity_is_rejected(self):
        bridge = Bridge()
        request, _ = startup_prompt.paths(2223, bridge.args.managed_id)
        startup_prompt.write_json(request, dict(instance='wrong', action='update-server', client_version='wrong'))
        with self.assertRaisesRegex(BridgeError, 'Invalid server update request'):
            startup_prompt.service_request(bridge.args, bridge.args.managed_id)


class ConsoleConsentTests(unittest.TestCase):
    def test_windows_enter_and_no(self):
        from guac_bridge import hook_auth
        for value, expected in [('', True), ('n', False), ('yes', True)]:
            with patch.object(hook_auth.sys, 'platform', 'win32'), \
                 patch.object(hook_auth, 'windows_console_line', return_value=value):
                self.assertEqual(hook_auth.confirm_hook_update(), expected)

    def test_missing_console_is_never_approval(self):
        from guac_bridge import hook_auth
        with patch.object(hook_auth.sys, 'platform', 'win32'), \
             patch.object(hook_auth, 'windows_console_line', side_effect=BridgeError('No console')):
            self.assertFalse(hook_auth.confirm_hook_update())
