"""Credential selection, native-store boundaries, and Guacamole authentication."""
import argparse
import asyncio
import os
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

import aiohttp
from aiohttp import web

from guac_bridge import auth
from guac_bridge.errors import AuthenticationError, BridgeError, CredentialStoreUnavailable


class MemoryStore:
    def __init__(self, value=None):
        self.value = value
        self.reads = []
        self.writes = []
        self.deletes = []

    def get(self, service, username):
        self.reads.append((service, username))
        return self.value

    def set(self, service, username, password):
        self.writes.append((service, username, password))
        self.value = password

    def delete(self, service, username):
        self.deletes.append((service, username))
        existed = self.value is not None
        self.value = None
        return existed


class CredentialTests(unittest.TestCase):
    def test_deployment_identity_ignores_connection_fragment_and_query(self):
        self.assertEqual(auth.credential_service('https://EXAMPLE.org:443/guacamole/?token=secret#/client/one'),
                         'guac-bridge:https://example.org/guacamole')
        self.assertEqual(auth.credential_service('https://example.org/guacamole/#/client/two'),
                         'guac-bridge:https://example.org/guacamole')
        self.assertNotEqual(auth.credential_service('https://example.org/a'),
                            auth.credential_service('https://example.org/b'))
        self.assertNotEqual(auth.credential_service('https://example.org:8443/a'),
                            auth.credential_service('https://example.org/a'))
        self.assertEqual(auth.credential_service('http://[::1]:80/'), 'guac-bridge:http://[::1]')
        for url in ('file:///tmp/password', 'https://user:secret@example.org', 'https://[bad', 'https://example.org:bad'):
            with self.assertRaises(BridgeError):
                auth.credential_service(url)

    def test_username_is_separate_credential_identity(self):
        backend = Mock()
        backend.get_password.return_value = None
        store = auth.CredentialStore()
        store._backend = backend
        store.get('deployment', 'alice')
        store.get('deployment', 'DOMAIN\\bob')
        self.assertEqual(backend.get_password.call_args_list[0].args, ('deployment', 'alice'))
        self.assertEqual(backend.get_password.call_args_list[1].args, ('deployment', 'DOMAIN\\bob'))

    def test_windows_backend_is_native_and_local_to_machine(self):
        native = Mock(priority=5)
        module = SimpleNamespace(WinVaultKeyring=Mock(return_value=native))
        with patch.object(auth.sys, 'platform', 'win32'), patch.object(auth.importlib, 'import_module', return_value=module) as load:
            self.assertIs(auth._native_backend(), native)
        load.assert_called_once_with('keyring.backends.Windows')
        self.assertEqual(native.persist, 'local machine')

    def test_unavailable_native_store_has_no_plaintext_fallback(self):
        with patch.object(auth.sys, 'platform', 'win32'), patch.object(auth.importlib, 'import_module', side_effect=ImportError) as load:
            with self.assertRaises(CredentialStoreUnavailable):
                auth._native_backend()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(load.call_args.args, ('keyring.backends.Windows',))

    def test_store_errors_never_expose_backend_secret_text(self):
        backend = Mock()
        backend.get_password.side_effect = RuntimeError('SECRET-FROM-BACKEND')
        backend.set_password.side_effect = RuntimeError('SECRET-FROM-BACKEND')
        store = auth.CredentialStore()
        store._backend = backend
        for operation in (lambda: store.get('s', 'u'), lambda: store.set('s', 'u', 'secret'),
                          lambda: store.delete('s', 'u')):
            with self.assertRaises(CredentialStoreUnavailable) as caught:
                operation()
            self.assertNotIn('SECRET-FROM-BACKEND', str(caught.exception))

    def test_forget_removes_only_selected_identity_and_handles_missing(self):
        store = MemoryStore('saved')
        auth.forget_credentials('https://example.org/guacamole/#/client/one', 'alice', store)
        self.assertIsNone(store.value)
        self.assertEqual(store.deletes, [('guac-bridge:https://example.org/guacamole', 'alice')])
        auth.forget_credentials('https://example.org/guacamole/', 'alice', store)

    def test_forget_unavailable_store_fails_clearly(self):
        store = Mock()
        store.delete.side_effect = CredentialStoreUnavailable('secret detail')
        with self.assertRaises(BridgeError) as caught:
            auth.forget_credentials('https://example.org', 'alice', store)
        self.assertNotIn('secret detail', str(caught.exception))

    def test_credential_flags_are_mutually_exclusive(self):
        parser = argparse.ArgumentParser()
        auth.add_credential_arguments(parser)
        with patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                parser.parse_args(['--save-password', '--no-keyring'])
            with self.assertRaises(SystemExit):
                parser.parse_args(['--save-password', '--forget-password'])

    def test_noninteractive_prompt_never_falls_back_to_echo(self):
        with patch.object(auth.sys.stdin, 'isatty', return_value=False), patch.object(auth.getpass, 'getpass') as prompt:
            with self.assertRaises(BridgeError):
                auth.prompt_password()
            prompt.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32' and os.environ.get('GUAC_TEST_NATIVE_KEYRING') == '1',
                         'Windows native credential-store integration is enabled in Windows CI')
    def test_native_windows_store_roundtrip_and_delete(self):
        service = 'guac-bridge-test:' + uuid.uuid4().hex
        username = 'temporary-test-user'
        value = uuid.uuid4().hex
        store = auth.CredentialStore()
        try:
            self.assertIsNone(store.get(service, username))
            store.set(service, username, value)
            self.assertEqual(store.get(service, username), value)
            self.assertEqual(store.backend.persist, 2)  # CRED_PERSIST_LOCAL_MACHINE
            self.assertTrue(store.delete(service, username))
            self.assertIsNone(store.get(service, username))
        finally:
            store.delete(service, username)


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.args = SimpleNamespace(save_password=False, no_keyring=False, connect_timeout=5)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.login = AsyncMock(return_value='token')
        self.login_patch = patch.object(auth, 'login', self.login)
        self.login_patch.start()
        self.prompt_patch = patch.object(auth, 'prompt_password', return_value='entered-secret')
        self.prompt = self.prompt_patch.start()

    async def asyncTearDown(self):
        self.prompt_patch.stop()
        self.login_patch.stop()
        self.environment.stop()

    async def test_environment_overrides_store_and_prompt(self):
        os.environ['GUAC_PASSWORD'] = 'environment-secret'
        store = MemoryStore('stored-secret')
        await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(self.login.call_args.args[3], 'environment-secret')
        self.assertFalse(store.reads)
        self.assertFalse(store.writes)
        self.prompt.assert_not_called()

    async def test_saved_password_is_reused_without_resaving(self):
        store = MemoryStore('stored-secret')
        result = await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(result, 'token')
        self.assertEqual(self.login.call_args.args[3], 'stored-secret')
        self.assertFalse(store.writes)
        self.prompt.assert_not_called()

    async def test_prompt_is_not_saved_without_opt_in(self):
        store = MemoryStore()
        await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.prompt.assert_called_once()
        self.assertFalse(store.writes)

    async def test_password_saved_only_after_successful_login(self):
        self.args.save_password = True
        store = MemoryStore()

        async def check_before_save(*args):
            self.assertFalse(store.writes)
            return 'token'
        self.login.side_effect = check_before_save
        await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(store.writes, [('guac-bridge:https://example.org', 'alice', 'entered-secret')])

    async def test_failed_login_never_saves_password(self):
        self.args.save_password = True
        store = MemoryStore()
        self.login.side_effect = AuthenticationError('rejected')
        with self.assertRaises(AuthenticationError):
            await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertFalse(store.writes)
        self.assertFalse(store.deletes)

    async def test_rejected_saved_password_gets_one_replacement_attempt(self):
        store = MemoryStore('old-secret')
        self.login.side_effect = [AuthenticationError('rejected'), 'new-token']
        await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(self.login.await_count, 2)
        self.prompt.assert_called_once()
        self.assertEqual(store.value, 'entered-secret')

    async def test_failed_replacement_keeps_old_credential(self):
        store = MemoryStore('old-secret')
        self.login.side_effect = AuthenticationError('rejected')
        with self.assertRaises(AuthenticationError):
            await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(self.login.await_count, 2)
        self.assertEqual(store.value, 'old-secret')
        self.assertFalse(store.writes)
        self.assertFalse(store.deletes)

    async def test_network_error_does_not_prompt_replace_or_delete(self):
        store = MemoryStore('stored-secret')
        self.login.side_effect = aiohttp.ClientConnectionError('offline')
        with self.assertRaises(aiohttp.ClientConnectionError):
            await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.prompt.assert_not_called()
        self.assertFalse(store.writes)
        self.assertFalse(store.deletes)

    async def test_no_keyring_skips_all_store_access(self):
        self.args.no_keyring = True
        store = Mock()
        await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        store.get.assert_not_called()
        store.set.assert_not_called()
        self.prompt.assert_called_once()

    async def test_unavailable_store_falls_back_without_logging_secrets(self):
        self.args.save_password = True
        store = Mock()
        store.get.side_effect = CredentialStoreUnavailable('SECRET-IN-EXCEPTION')
        with self.assertLogs('guac-auth', level='INFO') as logs:
            result = await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(result, 'token')
        self.assertNotIn('SECRET-IN-EXCEPTION', '\n'.join(logs.output))
        self.assertNotIn('entered-secret', '\n'.join(logs.output))
        self.prompt.assert_called_once()

    async def test_save_failure_does_not_break_successful_connection(self):
        self.args.save_password = True
        store = Mock()
        store.get.return_value = None
        store.set.side_effect = CredentialStoreUnavailable('SECRET-IN-EXCEPTION')
        with self.assertLogs('guac-auth', level='WARNING') as logs:
            result = await auth.authenticate(None, 'https://example.org/', 'alice', self.args, store)
        self.assertEqual(result, 'token')
        self.assertNotIn('SECRET-IN-EXCEPTION', '\n'.join(logs.output))

    async def test_forget_exits_both_clients_without_opening_network_session(self):
        from guac_bridge import client, ssh_client
        args = SimpleNamespace(url='https://example.org/guacamole/', username='alice', forget_password=True)
        for module in (client, ssh_client):
            with patch.object(module, 'forget_credentials') as forget, patch.object(module.aiohttp, 'ClientSession') as session:
                await module.serve(args)
                forget.assert_called_once_with(args.url, 'alice')
                session.assert_not_called()


class LoginHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_rejection_then_replacement_is_saved(self):
        attempts = []

        async def endpoint(request):
            data = await request.post()
            attempts.append(data['password'])
            if data['password'] != 'working-secret':
                return web.json_response({'message': 'rejected'}, status=403)
            return web.json_response({'authToken': 'valid-token'})
        app = web.Application()
        app.router.add_post('/guacamole/api/tokens', endpoint)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f'http://127.0.0.1:{port}/guacamole/'
        store = MemoryStore('expired-secret')
        try:
            with patch.dict(os.environ, {}, clear=True), patch.object(auth, 'prompt_password', return_value='working-secret'):
                async with aiohttp.ClientSession() as session:
                    result = await auth.authenticate(session, base, 'alice', SimpleNamespace(connect_timeout=5), store)
            self.assertEqual(result, 'valid-token')
            self.assertEqual(attempts, ['expired-secret', 'working-secret'])
            self.assertEqual(store.value, 'working-secret')
        finally:
            await runner.cleanup()


if __name__ == '__main__':
    unittest.main()
