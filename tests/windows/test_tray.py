"""Native tray and WinUI manager smoke tests, including process lifetime and usage."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / 'target/release/guac-bridge.exe'
user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
user32.FindWindowW.restype = w.HWND
user32.GetDlgItem.argtypes = [w.HWND, ctypes.c_int]
user32.GetDlgItem.restype = w.HWND
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.IsWindowVisible.argtypes = [w.HWND]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.MoveWindow.argtypes = [w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.BOOL]


def wait_for(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Timed out waiting for native manager state')


TITLE = 'Guac Bridge — running bridges'


def manager():
    return user32.FindWindowW(None, TITLE)


def ui(hwnd, invoke=None):
    args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(ROOT / 'tests/windows/uia.ps1'), '-Window', str(hwnd)]
    if invoke:
        args += ['-Invoke', invoke]
    return subprocess.check_output(args, timeout=20).decode(errors='replace')


class TrayTests(unittest.TestCase):
    def test_cli_starts_without_ui_activation(self):
        result = subprocess.run([str(BINARY), '--version'], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'guac-bridge', result.stdout)

    def test_bridge_launches_tray_before_remote_initialization(self):
        existing = user32.FindWindowW('GuacBridgeManager', None)
        if existing:
            user32.PostMessageW(existing, 0x111, 105, 0)
            wait_for(lambda: not user32.FindWindowW('GuacBridgeManager', None))
        release = threading.Event()
        class PendingLogin(socketserver.BaseRequestHandler):
            def handle(self):
                release.wait(20)
        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), PendingLogin)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        child = subprocess.Popen([str(BINARY), 'ssh', f'http://127.0.0.1:{server.server_address[1]}',
            '--connection-id', 'test', '--data-source', 'mysql',
            '--user', 'test', '--no-keyring', '--foreground', '--port', '0'],
            env={**os.environ, 'GUAC_BRIDGE_PASSWORD': 'test', 'NO_PROXY': '127.0.0.1'})
        try:
            hwnd = wait_for(lambda: user32.FindWindowW('GuacBridgeManager', None))
            self.assertIsNone(child.poll(), 'Tray must start while login is still pending')
            self.assertFalse(user32.IsWindowVisible(hwnd))
        finally:
            child.terminate()
            child.wait(timeout=5)
            release.set()
            server.shutdown()
            server.server_close()
            hwnd = user32.FindWindowW('GuacBridgeManager', None)
            if hwnd:
                user32.PostMessageW(hwnd, 0x111, 105, 0)
                wait_for(lambda: not user32.FindWindowW('GuacBridgeManager', None))

    @unittest.skipIf(sys.getwindowsversion().build < 22000, 'WinUI bootstrap requires Windows 11 APIs')
    def test_prepare_update_waits_for_tray_process_exit(self):
        existing = user32.FindWindowW('GuacBridgeManager', None)
        if existing:
            user32.PostMessageW(existing, 0x111, 105, 0)
            wait_for(lambda: not user32.FindWindowW('GuacBridgeManager', None))
        child = subprocess.Popen([str(BINARY), 'tray'])
        try:
            wait_for(manager, timeout=30)
            result = subprocess.run([str(BINARY), 'prepare-update'], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(child.wait(timeout=3), 0)
            self.assertFalse(manager(), 'Update must close the WinUI process too')
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    @unittest.skipIf(sys.getwindowsversion().build < 22000, 'WinUI bootstrap requires Windows 11 APIs')
    def test_discovery_singleton_hide_reopen_and_exit(self):
        # Previous bridge tests may already have launched this job's tray.
        existing = user32.FindWindowW('GuacBridgeManager', None)
        if existing:
            user32.PostMessageW(existing, 0x111, 105, 0)
            wait_for(lambda: not user32.FindWindowW('GuacBridgeManager', None))
        token = str(uuid.uuid4())
        commands = []

        class Control(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline(1024))
                if request.get('token') != token:
                    return
                commands.append(request['command'])
                if request['command'] == 'inspect':
                    self.wfile.write(json.dumps(dict(uptime_ms=5000, uploaded=2048,
                        downloaded=4096, connections=1, total_connections=3,
                        idle_ms=0)).encode() + b'\n')
                else:
                    self.wfile.write(b'OK\n')

        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Control)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        directory = Path(os.environ['LOCALAPPDATA']) / 'guac-bridge' / 'data'
        directory.mkdir(parents=True, exist_ok=True)
        registration = directory / 'llm-65534.json'
        self.assertFalse(registration.exists(), 'Refusing to replace an existing registration')
        registration.write_text(json.dumps(dict(pid=os.getpid(), control=server.server_address[1],
            token=token, identity='tray-smoke-test', username='frog-user', jump_url='https://guac.example/guacamole/#/client/MQBjAG15c3Fs?token=never-display', mode='llm', port=65534,
            target='smoke-test:8000', version='test', executable=str(BINARY), instance=str(uuid.uuid4()), log=None)))
        rdp_registration = directory / 'rdp-65533.json'
        self.assertFalse(rdp_registration.exists(), 'Refusing to replace an existing registration')
        rdp_state = json.loads(registration.read_text())
        rdp_state.update(mode='rdp', port=65533, target='rdp-smoke-test:3389', instance=str(uuid.uuid4()))
        rdp_registration.write_text(json.dumps(rdp_state))
        tcp_registration = directory / 'tcp-65532.json'
        self.assertFalse(tcp_registration.exists(), 'Refusing to replace an existing registration')
        tcp_state = dict(rdp_state, mode='tcp', port=65532, target='tcp-smoke-test:5432', instance=str(uuid.uuid4()))
        tcp_registration.write_text(json.dumps(tcp_state))
        child = None
        try:
            child = subprocess.Popen([str(BINARY), 'tray', '--background'])
            hwnd = wait_for(lambda: user32.FindWindowW('GuacBridgeManager', None))
            self.assertFalse(user32.IsWindowVisible(hwnd))
            self.assertFalse(manager(), 'Background tray must not open the manager')
            subprocess.run([str(BINARY), 'tray'], check=True, timeout=10)
            window = wait_for(manager, timeout=30)
            content = wait_for(lambda: (value if 'Total uploaded' in value else None)
                if (value := ui(window)) else None, timeout=30)
            self.assertIn('HTTP', content)
            self.assertIn('RDP', content)
            self.assertIn('TCP', content)
            self.assertIn('tcp://127.0.0.1:65532', content)
            self.assertIn('rdp://127.0.0.1:65533', content)
            self.assertIn('Gateway Type: Guacamole', content)
            self.assertNotIn('[Guacamole]', content)
            self.assertIn('2.0 KiB', content)
            self.assertIn('Total downloaded', content)
            self.assertIn('4.0 KiB', content)
            self.assertIn('https://guac.example/guacamole/#/client/MQBjAG15c3Fs', content)
            self.assertIn('frog-user', content)
            self.assertIn('http://127.0.0.1:65534', content)
            self.assertIn('http://smoke-test:8000 (on server)', content)
            self.assertNotIn('never-display', content)
            self.assertNotIn(token, content)
            self.assertNotIn('Bridge details', content)
            # Keep the full window within the runner's small virtual desktop.
            user32.MoveWindow(window, 10, 10, min(1000, user32.GetSystemMetrics(0) - 20),
                min(680, user32.GetSystemMetrics(1) - 50), True)
            subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(ROOT / 'tests/windows/uia.ps1'), '-Window', str(window),
                '-Screenshot', str(ROOT / 'manager-preview.png')], check=True, timeout=20)
            # A second launch raises the same UI process.
            subprocess.run([str(BINARY), 'tray'], check=True, timeout=10)
            self.assertEqual(manager(), window)
            ui(window, 'Stop selected…')
            self.assertIn('Disconnect and stop', ui(window))
            self.assertNotIn('stop', commands, 'Confirmation must precede stop')
            ui(window, 'Cancel')
            self.assertNotIn('Disconnect and stop', ui(window))
            # Resizing the native window preserves its content and constraints.
            user32.MoveWindow(window, 20, 20, 800, 460, True)
            initial = w.RECT()
            user32.GetWindowRect(window, ctypes.byref(initial))
            user32.SendMessageW(window, 0x112, 0xF030, 0)
            enlarged = w.RECT()
            user32.GetWindowRect(window, ctypes.byref(enlarged))
            self.assertGreater(enlarged.right - enlarged.left, initial.right - initial.left)
            self.assertIn('Total uploaded', ui(window))
            user32.SendMessageW(window, 0x112, 0xF120, 0)
            # Both WM_CLOSE and X / Alt+F4 close only the manager process.
            for message, command in ((0x10, 0), (0x112, 0xF060)):
                user32.PostMessageW(window, message, command, 0)
                wait_for(lambda: not manager())
                self.assertIsNone(child.poll(), 'Closing the manager must preserve the tray process')
                self.assertEqual(user32.FindWindowW('GuacBridgeManager', None), hwnd)
                self.assertNotIn('stop', commands)
                subprocess.run([str(BINARY), 'tray'], check=True, timeout=10)
                window = wait_for(manager, timeout=30)
                wait_for(lambda: '4.0 KiB' in ui(window), timeout=30)
            user32.PostMessageW(hwnd, 0x111, 105, 0)
            self.assertEqual(child.wait(timeout=20), 0)
            self.assertFalse(manager(), 'Exit tray closes the manager too')
            self.assertTrue(registration.exists())
            self.assertNotIn('stop', commands)
        finally:
            if child and child.poll() is None:
                host = user32.FindWindowW('GuacBridgeManager', None)
                if host:
                    user32.PostMessageW(host, 0x111, 105, 0)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    child.wait(timeout=5)
            log = directory / 'manager.log'
            if log.exists() and log.stat().st_size:
                print('Manager diagnostics:', log.read_text(errors='replace'))
            registration.unlink(missing_ok=True)
            rdp_registration.unlink(missing_ok=True)
            tcp_registration.unlink(missing_ok=True)
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
