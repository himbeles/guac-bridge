"""Server update commands use fake uv executables; tests never install packages."""
import asyncio
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guac_bridge import __version__
from guac_bridge.errors import BridgeError
from guac_bridge.update import ServerUpdate, validate_update


def fake_installation(root, helper, *, matching=False, fail=False):
    home = Path(root) / 'home'
    bindir = home / '.local/bin'
    bindir.mkdir(parents=True)
    state, record = home / 'updated', home / 'uv-args'
    original = helper.read_text()
    module = 'guac_bridge.ssh_server' if helper.name == 'guac-ssh-server' else 'guac_bridge.server'
    wrapper = ('#!' + sys.executable + '\nimport sys, pathlib\n'
        'if sys.argv[1:] == ["--version"] and not pathlib.Path(' + repr(str(state)) + ').exists():\n'
        ' print("helper 0.0.0"); raise SystemExit(0)\n'
        'from ' + module + ' import cli\nraise SystemExit(cli())\n')
    if matching:
        wrapper = original
    destination = bindir / helper.name
    destination.write_text(wrapper)
    destination.chmod(0o755)
    uv = bindir / 'uv'
    uv.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + shlex.quote(str(record)) +
                  "\nprintf '%s' \"$UV_INDEX_URL\" > " + shlex.quote(str(home / 'index')) +
                  ('\nexit 17\n' if fail else '\ntouch ' + shlex.quote(str(state)) + '\n'))
    uv.chmod(0o755)
    env = dict(HOME=str(home), PATH=str(bindir) + os.pathsep + os.environ['PATH'],
               UV_INDEX_URL='https://index.example.invalid/simple')
    return home, record, env


class UpdateCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_explains_uv_index_and_manual_update(self):
        import argparse
        from guac_bridge.update import add_update_argument
        parser = argparse.ArgumentParser()
        add_update_argument(parser)
        self.assertFalse(parser.parse_args([]).update_server)
        self.assertTrue(parser.parse_args(['--update-server']).update_server)
        text = ' '.join(parser.format_help().split())
        self.assertIn('uv tool install --force guac-bridge==' + __version__, text)
        self.assertIn('configured package index', text)
        self.assertIn('otherwise update manually', text)

    async def test_explicit_server_python_overrides_detection(self):
        bridge = SimpleNamespace(helper_name='guac-ssh-server', args=SimpleNamespace(server_python='3.11'))
        update = ServerUpdate(bridge)
        self.assertIn(b'server_python=3.11;', update.command())
        self.assertIn(b'--python "$server_python"', update.command())
        update.future.cancel()
        with self.assertRaisesRegex(BridgeError, '--server-python requires --update-server'):
            validate_update(SimpleNamespace(update_server=False, server_python='3.11'), 'guac-ssh-server')

    async def test_custom_installation_and_background_mode_require_manual_action(self):
        for values in (dict(server_command='/custom/helper'),
                       dict(server_command='~/.local/bin/guac-ssh-server', ensure_running=True)):
            with self.assertRaises(BridgeError):
                validate_update(SimpleNamespace(update_server=True, **values), 'guac-ssh-server')

    async def test_failed_update_and_missing_response_require_manual_update(self):
        for status in ('no-uv', 'no-timeout', 'install-failed', 'verify-failed', None):
            update = ServerUpdate(SimpleNamespace(helper_name='guac-ssh-server'))
            if status:
                update.dispatch(dict(type='bootstrap-update', token=update.token, status=status))
            update.ended()
            with self.assertRaisesRegex(BridgeError, 'Update manually on the server with: uv tool install'):
                await update.future

    async def test_terminal_echo_cannot_impersonate_update_result(self):
        update = ServerUpdate(SimpleNamespace(helper_name='guac-ssh-server'))
        with self.assertRaisesRegex(BridgeError, 'Unexpected server update response'):
            update.dispatch(dict(type='bootstrap-update', token='wrong', status='updated'))
        self.assertIsNone(update.result)
        update.future.cancel()

    async def test_shell_command_is_bounded_and_contains_only_pinned_install(self):
        update = ServerUpdate(SimpleNamespace(helper_name='guac-ssh-server'))
        command = update.command()
        self.assertLess(len(command), 3900)
        self.assertIn(('guac-bridge==' + __version__).encode(), command)
        self.assertIn(b'timeout --kill-after=5 180', command)
        self.assertNotIn(b'--index-url', command)
        update.future.cancel()

    @unittest.skipUnless(os.name == 'posix', 'Server bootstrap shell runs on Unix')
    async def test_missing_uv_is_detected_by_bootstrap_command(self):
        import shutil
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            bindir = home / '.local/bin'
            bindir.mkdir(parents=True)
            for name in ('bash', 'timeout'):
                (bindir / name).symlink_to(shutil.which(name))
            helper = bindir / 'guac-ssh-server'
            helper.write_text("#!/bin/sh\nprintf '%s\n' 'helper 0.0.0'\n")
            helper.chmod(0o755)
            update = ServerUpdate(SimpleNamespace(helper_name='guac-ssh-server'))
            result = subprocess.run(['/bin/bash', '-c', update.command().decode()],
                env=dict(os.environ, HOME=str(home), PATH=str(bindir)), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout.replace(b'\x1b]482202;2;LLM\x07', b'').replace(b'\x1b]482203;\x07', b'')
            events = [json.loads(base64.b64decode(line)) for line in output.splitlines()]
            self.assertEqual(events[-1]['status'], 'no-uv')
            update.future.cancel()

    async def test_timeout_reports_manual_update(self):
        class FakeBridge:
            helper_name = 'guac-ssh-server'
            async def send_input(self, data): pass
            async def drain_input(self): pass
            async def wait_for(self, future, timeout):
                future.cancel()
                raise asyncio.TimeoutError()
        update = ServerUpdate(FakeBridge())
        with self.assertRaisesRegex(BridgeError, 'Update manually'):
            await update.run()


# Import fixtures without inheriting their test methods (avoids running suites twice).
if os.name == 'posix':
    import test_bridge as bridge_fixtures
    import test_ssh_bridge as ssh_fixtures

    class UpdateLLMTests(unittest.IsolatedAsyncioTestCase):
        asyncSetUp = bridge_fixtures.BridgeTests.asyncSetUp
        asyncTearDown = bridge_fixtures.BridgeTests.asyncTearDown
        models, chat, connect = bridge_fixtures.BridgeTests.models, bridge_fixtures.BridgeTests.chat, bridge_fixtures.BridgeTests.connect

        async def test_update_then_forward_api_request(self):
            home, record, env = fake_installation(self.temp.name, self.helper)
            with patch.dict(os.environ, env):
                bridge, local, _ = await self.connect(mode='http', update_server=True, server_command='~/.local/bin/guac-llm-server')
                async with self.session.get(local + 'v1/models') as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())['data'][0]['id'], 'test-model')
            self.assertEqual(record.read_text().splitlines(), ['tool', 'install', '--force', '--python', getattr(sys, '_base_executable', sys.executable), 'guac-bridge==' + __version__])
            self.assertEqual((home / 'index').read_text(), env['UV_INDEX_URL'])
            self.assertTrue(bridge.ready.is_set())

        async def test_interactive_enter_updates_in_the_same_connection(self):
            _, record, env = fake_installation(self.temp.name, self.helper)
            import io
            with patch.dict(os.environ, env), patch('sys.stdin.isatty', return_value=True), \
                 patch('sys.stderr', io.StringIO()) as output, patch('builtins.input', return_value=''):
                bridge, local, _ = await self.connect(server_command='~/.local/bin/guac-llm-server')
                async with self.session.get(local + 'v1/models') as response:
                    self.assertEqual(response.status, 200)
            self.assertTrue(record.exists())
            self.assertTrue(bridge.ready.is_set())
            self.assertIn('Update server now? [Y/n]', output.getvalue())

        async def test_failed_install_stops_before_helper_and_explains_manual_action(self):
            _, _, env = fake_installation(self.temp.name, self.helper, fail=True)
            with patch.dict(os.environ, env), self.assertRaisesRegex(BridgeError, 'Update manually'):
                await self.connect(update_server=True, server_command='~/.local/bin/guac-llm-server')
            self.assertFalse(self.bridges[-1].ready.is_set())

    class UpdateSSHTests(unittest.IsolatedAsyncioTestCase):
        asyncSetUp, asyncTearDown = ssh_fixtures.SSHTests.asyncSetUp, ssh_fixtures.SSHTests.asyncTearDown
        target, connect, echo, transfer = ssh_fixtures.SSHTests.target, ssh_fixtures.SSHTests.connect, ssh_fixtures.SSHTests.echo, ssh_fixtures.SSHTests.transfer

        async def test_update_then_binary_transfer(self):
            _, record, env = fake_installation(self.temp.name, self.helper)
            target = await self.target(self.echo)
            with patch.dict(os.environ, env):
                bridge, local, _ = await self.connect(target, update_server=True, server_command='~/.local/bin/guac-ssh-server')
                await self.transfer(local, os.urandom(65536))
            self.assertTrue(bridge.ready.is_set())
            self.assertTrue(record.exists())

        async def test_matching_version_skips_installation(self):
            _, record, env = fake_installation(self.temp.name, self.helper, matching=True)
            target = await self.target(self.echo)
            with patch.dict(os.environ, env):
                _, local, _ = await self.connect(target, update_server=True, server_command='~/.local/bin/guac-ssh-server')
                await self.transfer(local, b'already current')
            self.assertFalse(record.exists())
