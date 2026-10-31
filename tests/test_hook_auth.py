"""First hook login saves verified credentials before a detached bridge starts."""
import getpass
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch

import aiohttp
from aiohttp import web

from guac_bridge import hook_auth
from guac_bridge.auth import credential_service
from guac_bridge.errors import AuthenticationError, BridgeError, CredentialStoreUnavailable


class Store:
    def __init__(self, value=None, fail_write=False):
        self.value, self.fail_write = value, fail_write

    def get(self, *args):
        return self.value

    def set(self, service, username, password):
        if self.fail_write:
            raise CredentialStoreUnavailable('Store unavailable')
        self.value = password


class HookPromptTests(unittest.TestCase):
    def test_windows_prompt_reads_console_without_echo_and_handles_backspace(self):
        output = Mock()
        keys = Mock(side_effect=['s', 'x', '\b', 'e', 'c', 'r', 'e', 't', '\r'])
        with patch.object(hook_auth.sys, 'platform', 'win32'), \
             patch('guac_bridge.console._windows_console_output', return_value=output), \
             patch.dict('sys.modules', {'msvcrt': SimpleNamespace(getwch=keys)}), \
             patch('builtins.open', mock_open()):
            self.assertEqual(hook_auth.prompt_hook_password(), 'secret')
        self.assertNotIn('secret', ''.join(call.args[0] for call in output.write.call_args_list))
        output.close.assert_called_once()

    def test_no_terminal_never_falls_back_to_echoed_input(self):
        with patch.object(hook_auth.sys, 'platform', 'linux'), \
             patch.object(hook_auth.sys, 'stdin', SimpleNamespace(isatty=lambda: False)), \
             patch('builtins.open', side_effect=OSError), patch.object(hook_auth.getpass, 'getpass') as ask:
            with self.assertRaisesRegex(BridgeError, 'No calling terminal'):
                hook_auth.prompt_hook_password()
            ask.assert_not_called()


class HookAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logins, self.deleted = [], []
        app = web.Application()
        async def login(request):
            data = await request.post()
            self.logins.append(data['password'])
            return web.json_response({'authToken': 'test-token'}) if data['password'] == 'correct' else web.Response(status=403)
        async def delete(request):
            self.deleted.append(True)
            return web.Response()
        app.router.add_post('/api/tokens', login)
        app.router.add_delete('/api/tokens/{token}', delete)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.args = SimpleNamespace(url=f'http://127.0.0.1:{port}/', username='alice',
            connection_id='1', data_source='mysql', ca_file=None, insecure=False, connect_timeout=2,
            save_password=True, no_keyring=False)
        self.env = patch.dict(os.environ)
        self.env.start()
        os.environ.pop('GUAC_PASSWORD', None)

    async def asyncTearDown(self):
        self.env.stop()
        await self.runner.cleanup()

    async def test_unavailable_store_stops_before_prompting(self):
        store = Mock()
        store.get.side_effect = CredentialStoreUnavailable('Unavailable')
        with patch.object(hook_auth, 'prompt_hook_password') as ask:
            with self.assertRaisesRegex(BridgeError, 'credential storage is unavailable'):
                await hook_auth.prepare_credentials(self.args, store=store)
        ask.assert_not_called()
        self.assertEqual(self.logins, [])

    async def test_first_login_is_saved_and_session_is_closed(self):
        store = Store()
        with patch.object(hook_auth, 'prompt_hook_password', return_value='correct') as ask:
            await hook_auth.prepare_credentials(self.args, store=store)
        ask.assert_called_once()
        self.assertEqual(store.value, 'correct')
        self.assertEqual(self.deleted, [True])

    async def test_saved_login_is_reused_without_prompt(self):
        with patch.object(hook_auth, 'prompt_hook_password') as ask:
            await hook_auth.prepare_credentials(self.args, store=Store('correct'))
        ask.assert_not_called()

    async def test_rejected_saved_password_is_replaced_only_after_valid_login(self):
        store = Store('old')
        with patch.object(hook_auth, 'prompt_hook_password', return_value='correct'):
            await hook_auth.prepare_credentials(self.args, store=store)
        self.assertEqual(self.logins, ['old', 'correct'])
        self.assertEqual(store.value, 'correct')

    async def test_failed_login_is_not_saved(self):
        store = Store()
        with patch.object(hook_auth, 'prompt_hook_password', return_value='wrong'):
            with self.assertRaises(AuthenticationError):
                await hook_auth.prepare_credentials(self.args, store=store)
        self.assertIsNone(store.value)

    async def test_store_failure_stops_background_setup_and_closes_login_session(self):
        with patch.object(hook_auth, 'prompt_hook_password', return_value='correct'):
            with self.assertRaisesRegex(BridgeError, 'password could not be saved'):
                await hook_auth.prepare_credentials(self.args, store=Store(fail_write=True))
        self.assertEqual(self.deleted, [True])
