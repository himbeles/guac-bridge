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
from guac_bridge.control import ControlServer, read_state, request
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
import asyncio, sys
from pathlib import Path
from guac_bridge.control import ControlServer
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    visible = '--visible-console' in sys.argv
    if not visible:
        window = kernel.GetConsoleWindow()
        assert not window or not user.IsWindowVisible(window), 'Unexpected visible background window'
    for stream in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        handle = kernel.GetStdHandle(stream & 0xffffffff)
        if visible:
            mode = wintypes.DWORD()
            assert kernel.GetFileType(handle) == 2, 'Visible output is not a console character device'
            assert kernel.GetConsoleMode(handle, ctypes.byref(mode)), 'Visible output is not attached to a console'
        else:
            assert kernel.GetFileType(handle) == 1, 'Background output is not the log file'
if '--visible-console' not in sys.argv:
    assert sys.stdin is None or not sys.stdin.isatty()
async def main():
    await asyncio.sleep(.2)
    server = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', {self.args.port})
    print('fixture ready', flush=True)
    control = await ControlServer(Path({str(background.state_path(self.args.port))!r}), sys.argv[sys.argv.index('--managed-id') + 1]).start()
    await control.stopped.wait()
    server.close()
    await server.wait_closed()
    await control.close()
asyncio.run(main())
''')

    def test_child_startup_output_is_relayed_without_old_log_lines(self):
        path = self.root / 'progress.log'
        path.write_bytes(b'old error\n')
        offset = path.stat().st_size
        with path.open('ab') as output:
            output.write(b'[info] Checking helper\npartial')
        with self.assertLogs('guac-ssh', level='INFO') as logs:
            offset, pending = background.relay_startup_output(path, offset, b'')
        self.assertIn('Checking helper', '\n'.join(logs.output))
        self.assertNotIn('old error', '\n'.join(logs.output))
        self.assertEqual(pending, b'partial')
        with path.open('ab') as output:
            output.write(b' line\n')
        with self.assertLogs('guac-ssh', level='INFO') as logs:
            _, pending = background.relay_startup_output(path, offset, pending)
        self.assertIn('partial line', '\n'.join(logs.output))
        self.assertEqual(pending, b'')

    def test_hook_error_is_also_saved_when_terminal_output_is_hidden(self):
        self.args.ensure_running = True
        background.record_hook_error(self.args, 'Server helper 0.5.0 needs updating')
        self.assertIn('Server helper 0.5.0', Path(self.args.background_log).read_text())

    def test_existing_listener_returns_without_spawning(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', self.args.port))
            server.listen()
            with patch.object(background.subprocess, 'Popen') as spawn:
                background.ensure_running(self.args)
            spawn.assert_not_called()

    def test_existing_managed_listener_repeats_version_warning(self):
        report = dict(version_verified=False, version_warning='Server version mismatch: old helper')
        with patch.object(background, 'listening', return_value=True), \
                patch.object(background, 'read_state', return_value={'instance': 'test'}), \
                patch.object(background, 'request', return_value=report):
            with self.assertLogs('guac-ssh', level='WARNING') as logs:
                background.ensure_running(self.args)
        self.assertIn('Server version mismatch', '\n'.join(logs.output))

    def test_strict_reuse_rejects_unverified_listener(self):
        self.args.require_server_version = True
        with patch.object(background, 'listening', return_value=True), \
                patch.object(background, 'read_state', return_value={'instance': 'test'}), \
                patch.object(background, 'request', return_value={'version_verified': False}):
            with self.assertRaisesRegex(BridgeError, '--require-server-version'):
                background.ensure_running(self.args)

    def test_transport_sizes_are_forwarded_to_background_child(self):
        self.args.window, self.args.frame_size = 8, 8192
        command = background.child_command(self.args)
        self.assertEqual(command[command.index('--window') + 1], '8')
        self.assertEqual(command[command.index('--frame-size') + 1], '8192')

    def test_strict_flag_is_forwarded_to_child(self):
        self.args.require_server_version = True
        self.assertIn('--require-server-version', background.child_command(self.args))

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

    def test_child_startup_error_is_reported_directly_and_record_is_removed(self):
        body = f'''
import json, sys
from pathlib import Path
instance = sys.argv[sys.argv.index('--managed-id') + 1]
path = Path({str(self.root / ('ssh-' + str(self.args.port)))!r} + '.startup-' + instance + '.json')
path.write_text(json.dumps(dict(error='Server version mismatch. Run uv tool install --force guac-bridge==0.5.0')))
sys.exit(1)
'''
        with self.fake_child(body), patch.object(background.subprocess, 'Popen', side_effect=self.spawn):
            with self.assertRaisesRegex(BridgeError, 'Server version mismatch. Run uv tool install'):
                background.ensure_running(self.args)
        self.assertFalse(list(self.root.glob('*.startup-*.json')))

    def test_timeout_terminates_child(self):
        self.args.ready_timeout = .4
        with self.fake_child('import time; time.sleep(60)'), \
                patch.object(background.subprocess, 'Popen', side_effect=self.spawn):
            with self.assertRaisesRegex(BridgeError, 'did not become ready'):
                background.ensure_running(self.args)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertFalse(background.listening(self.args.port))

    def test_interrupted_startup_terminates_child(self):
        # Interruption while waiting must clean up the owned startup process.
        with self.fake_child('import time; time.sleep(60)'), \
                patch.object(background.subprocess, 'Popen', side_effect=self.spawn), \
                patch.object(background, 'read_state', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                background.ensure_running(self.args)
        self.assertIsNotNone(self.processes[0].poll())

    def test_stop_managed_bridge_shuts_down_listener_and_removes_state(self):
        with self.listener_child(), patch.object(background.subprocess, 'Popen', side_effect=self.spawn):
            background.ensure_running(self.args)
        background.stop_managed(self.args.port, 10)
        self.processes[0].wait(timeout=5)
        self.assertFalse(background.listening(self.args.port))
        self.assertFalse(background.state_path(self.args.port).exists())

    def test_stop_does_not_kill_unmanaged_listener(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', self.args.port))
            server.listen()
            with self.assertRaisesRegex(BridgeError, 'not managed'):
                background.stop_managed(self.args.port, 1)
            self.assertTrue(background.listening(self.args.port))

    def test_stop_cli_requires_no_url_or_username(self):
        with patch.object(ssh_client, 'stop_managed') as stop, patch.object(ssh_client, 'setup_console'):
            self.assertEqual(ssh_client.cli(['--stop', '--port', '2223']), 0)
        stop.assert_called_once_with(2223, 60)

    def test_stale_record_is_removed_without_killing_any_process(self):
        import json
        state = dict(control_port=self.args.port, token='0' * 64, instance='old', pid=1)
        background.state_path(self.args.port).write_text(json.dumps(state))
        with patch.object(background, 'stop_child') as stop:
            background.stop_managed(self.args.port, 2)
        stop.assert_not_called()
        self.assertFalse(background.state_path(self.args.port).exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Visible consoles are a Windows option')
    def test_windows_visible_console_is_ready_and_can_be_stopped(self):
        self.args.show_console = True
        with self.listener_child(), patch.object(background.subprocess, 'Popen', side_effect=self.spawn):
            background.ensure_running(self.args)
        self.assertTrue(background.listening(self.args.port))
        background.stop_managed(self.args.port, 10)
        self.processes[0].wait(timeout=5)
        self.assertFalse(background.listening(self.args.port))

    def test_username_required_before_new_background_start(self):
        self.args.username = None
        with self.assertRaisesRegex(BridgeError, 'requires --username'):
            background.ensure_running(self.args)

    def test_child_arguments_preserve_values_without_shell_or_recursive_ensure(self):
        self.args.username = 'domain\\alice with spaces'
        self.args.server_command = '~/path with spaces/helper'
        self.args.no_keyring = True
        self.args.insecure = True
        self.args.ignore_server_version = True
        command = background.child_command(self.args)
        self.assertEqual(command[:3], [sys.executable, '-m', 'guac_bridge.ssh_client'])
        self.assertEqual(command[command.index('--url') + 1], self.args.url)
        self.assertEqual(command[command.index('--username') + 1], self.args.username)
        self.assertEqual(command[command.index('--server-command') + 1], self.args.server_command)
        self.assertIn('--no-keyring', command)
        self.assertIn('--insecure', command)
        self.assertIn('--ignore-server-version', command)
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


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_requires_token_and_removes_only_own_state(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control.json'
            control = await ControlServer(path, 'test-instance', version_verified=False,
                                          version_warning='Server version mismatch: old helper').start()
            try:
                state = read_state(path)
                bad = dict(state, token='0' * 64)
                with self.assertRaises(BridgeError):
                    await asyncio.to_thread(request, bad, 'stop')
                self.assertFalse(control.stopped.is_set())
                result = await asyncio.to_thread(request, state, 'status')
                self.assertTrue(result['ok'])
                self.assertIs(result['version_verified'], False)
                self.assertEqual(result['version_warning'], 'Server version mismatch: old helper')
                self.assertNotIn('token', result)
                await asyncio.to_thread(request, state, 'stop')
                await asyncio.wait_for(control.stopped.wait(), 2)
            finally:
                await control.close()
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
