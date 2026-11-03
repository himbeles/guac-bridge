"""Native Win32 manager smoke test, including discovery and singleton behaviour."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import socketserver
import subprocess
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


def text(hwnd):
    buffer = ctypes.create_unicode_buffer(8192)
    # WM_GETTEXT supports edit controls in another process; GetWindowTextW does not.
    user32.SendMessageW(hwnd, 0x000D, len(buffer), ctypes.addressof(buffer))
    return buffer.value


class TrayTests(unittest.TestCase):
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
        child = subprocess.Popen([str(BINARY), 'ssh', '--url', f'http://127.0.0.1:{server.server_address[1]}',
            '--connection-id', 'test', '--data-source', 'mysql',
            '--username', 'test', '--no-keyring', '--port', '0'],
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

    def test_prepare_update_waits_for_tray_process_exit(self):
        existing = user32.FindWindowW('GuacBridgeManager', None)
        if existing:
            user32.PostMessageW(existing, 0x111, 105, 0)
            wait_for(lambda: not user32.FindWindowW('GuacBridgeManager', None))
        child = subprocess.Popen([str(BINARY), 'tray', '--background'])
        try:
            wait_for(lambda: user32.FindWindowW('GuacBridgeManager', None))
            result = subprocess.run([str(BINARY), 'prepare-update'], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(child.wait(timeout=3), 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

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
            token=token, identity='tray-smoke-test', username=None, mode='llm', port=65534,
            target='smoke-test:8000', version='test', executable=str(BINARY), instance=str(uuid.uuid4()))))
        child = None
        try:
            child = subprocess.Popen([str(BINARY), 'tray', '--background'])
            hwnd = wait_for(lambda: user32.FindWindowW('GuacBridgeManager', None))
            detail = user32.GetDlgItem(hwnd, 110)
            wait_for(lambda: 'Total uploaded: 2.0 KiB' in text(detail))
            self.assertIn('[LLM]', text(detail))
            self.assertIn('Total downloaded: 4.0 KiB', text(detail))
            self.assertNotIn(token, text(detail))
            self.assertFalse(user32.IsWindowVisible(hwnd))
            subprocess.run([str(BINARY), 'tray'], check=True, timeout=10)
            wait_for(lambda: user32.IsWindowVisible(hwnd))
            self.assertEqual(user32.FindWindowW('GuacBridgeManager', None), hwnd)
            # Start smaller than the runner desktop, independent of its resolution.
            user32.MoveWindow(hwnd, 20, 20, 800, 460, True)
            initial = w.RECT()
            user32.GetWindowRect(detail, ctypes.byref(initial))
            user32.SendMessageW(hwnd, 0x112, 0xF030, 0)  # SC_MAXIMIZE dispatches nested WM_SIZE
            enlarged = w.RECT()
            user32.GetWindowRect(detail, ctypes.byref(enlarged))
            self.assertGreater(enlarged.right - enlarged.left, initial.right - initial.left,
                'Details must grow with the window when maximized')
            user32.SendMessageW(hwnd, 0x112, 0xF120, 0)  # SC_RESTORE
            restored = w.RECT()
            user32.GetWindowRect(detail, ctypes.byref(restored))
            self.assertEqual(restored.right - restored.left, initial.right - initial.left)
            # Exercise direct close and the system command used by X / Alt+F4.
            for message, command in ((0x10, 0), (0x112, 0xF060)):
                user32.PostMessageW(hwnd, message, command, 0)
                wait_for(lambda: not user32.IsWindowVisible(hwnd))
                self.assertIsNone(child.poll(), 'Closing the manager must preserve the tray process')
                self.assertEqual(user32.FindWindowW('GuacBridgeManager', None), hwnd)
                self.assertNotIn('stop', commands)
                subprocess.run([str(BINARY), 'tray'], check=True, timeout=10)
                wait_for(lambda: user32.IsWindowVisible(hwnd))
            user32.PostMessageW(hwnd, 0x111, 105, 0)  # Exit tray, keeping bridges
            self.assertEqual(child.wait(timeout=10), 0)
            self.assertTrue(registration.exists())
            self.assertNotIn('stop', commands)
        finally:
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            registration.unlink(missing_ok=True)
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
