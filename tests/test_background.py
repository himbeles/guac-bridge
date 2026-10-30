"""Portable startup, readiness, cleanup, and concurrent SSH invocation tests."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guac_bridge import background, ssh_client
from guac_bridge.errors import BridgeError


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes = []
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.args = SimpleNamespace(port=port, username='alice', url='https://example.org/#/client/ID',
                                    ready_timeout=10, background_log=str(self.root / 'bridge.log'))
        self.log_patch = patch.object(background, 'default_log', side_effect=lambda port: self.root / f'ssh-{port}.log')
        self.log_patch.start()
        self.real_popen = subprocess.Popen

    def tearDown(self):
        for process in self.processes:
            background.stop_child(process)
            process.wait(timeout=5)
        self.log_patch.stop()
        self.temp.cleanup()

    def spawn(self, *args, **kwargs):
        process = self.real_popen(*args, **kwargs)
        self.processes.append(process)
        return process

    def fake_child(self, body):
        return patch.object(background, 'child_command', return_value=[sys.executable, '-u', '-c', body])

    def listener_child(self):
        return self.fake_child(f'''
import socket, sys, time
assert sys.stdin is None or not sys.stdin.isatty()
time.sleep(.2)
sock = socket.socket()
sock.bind(('127.0.0.1', {self.args.port}))
print('fixture ready', flush=True)
sock.listen()
while True:
    conn, addr = sock.accept()
    conn.close()
''')

    def test_existing_listener_returns_without_spawning(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', self.args.port))
            server.listen()
            with patch.object(background.subprocess, 'Popen') as spawn:
                background.ensure_running(self.args)
            spawn.assert_not_called()

    def test_delayed_child_is_ready_then_remains_running_and_is_reused(self):
        with self.listener_child(), patch.object(background.subprocess, 'Popen', side_effect=self.spawn) as spawn:
            try:
                background.ensure_running(self.args)
            except BridgeError as exc:
                self.fail(str(exc) + '\n' + Path(self.args.background_log).read_text())
            self.assertTrue(background.listening(self.args.port))
            self.assertIsNone(self.processes[0].poll())
            background.ensure_running(self.args)
            spawn.assert_called_once()
        self.assertIn('fixture ready', Path(self.args.background_log).read_text())

    def test_concurrent_ensure_invocations_start_only_one_child(self):
        with self.listener_child(), patch.object(background.subprocess, 'Popen', side_effect=self.spawn) as spawn:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: background.ensure_running(self.args), range(4)))
            spawn.assert_called_once()
        self.assertTrue(background.listening(self.args.port))

    def test_failed_child_reports_exit_and_log(self):
        with self.fake_child("import sys; print('authentication failed', flush=True); sys.exit(7)"), \
                patch.object(background.subprocess, 'Popen', side_effect=self.spawn):
            with self.assertRaisesRegex(BridgeError, 'status 7; see'):
                background.ensure_running(self.args)
        self.assertIn('authentication failed', Path(self.args.background_log).read_text())

    def test_timeout_terminates_child(self):
        self.args.ready_timeout = .4
        with self.fake_child('import time; time.sleep(60)'), \
                patch.object(background.subprocess, 'Popen', side_effect=self.spawn):
            with self.assertRaisesRegex(BridgeError, 'did not become ready'):
                background.ensure_running(self.args)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertFalse(background.listening(self.args.port))

    def test_interrupted_startup_terminates_child(self):
        # First two probes precede spawn; interruption happens while waiting.
        with self.fake_child('import time; time.sleep(60)'), \
                patch.object(background.subprocess, 'Popen', side_effect=self.spawn), \
                patch.object(background, 'listening', side_effect=[False, False, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                background.ensure_running(self.args)
        self.assertIsNotNone(self.processes[0].poll())

    def test_username_required_before_new_background_start(self):
        self.args.username = None
        with self.assertRaisesRegex(BridgeError, 'requires --username'):
            background.ensure_running(self.args)

    def test_child_arguments_preserve_values_without_shell_or_recursive_ensure(self):
        self.args.username = 'domain\\alice with spaces'
        self.args.server_command = '~/path with spaces/helper'
        self.args.no_keyring = True
        self.args.insecure = True
        command = background.child_command(self.args)
        self.assertEqual(command[:3], [sys.executable, '-m', 'guac_bridge.ssh_client'])
        self.assertEqual(command[command.index('--url') + 1], self.args.url)
        self.assertEqual(command[command.index('--username') + 1], self.args.username)
        self.assertEqual(command[command.index('--server-command') + 1], self.args.server_command)
        self.assertIn('--no-keyring', command)
        self.assertIn('--insecure', command)
        self.assertNotIn('--ensure-running', command)
        self.assertNotIn('--save-password', command)

    def test_cli_dispatches_ensure_without_running_foreground(self):
        with patch.object(ssh_client, 'ensure_running') as ensure, \
                patch.object(ssh_client.asyncio, 'run') as run, patch.object(ssh_client, 'setup_console'):
            code = ssh_client.cli(['--ensure-running', '--url', self.args.url, '--username', 'alice'])
        self.assertEqual(code, 0)
        self.assertTrue(ensure.call_args.args[0].ensure_running)
        run.assert_not_called()

    def test_cli_rejects_invalid_background_flags_and_timeouts(self):
        for option in (['--save-password'], ['--forget-password'], ['--ready-timeout', 'nan'],
                       ['--ready-timeout', 'inf'], ['--ready-timeout', '0']):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    ssh_client.cli(['--ensure-running', '--url', self.args.url, *option])
                self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
