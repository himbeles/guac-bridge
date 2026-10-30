"""Start a detached SSH bridge and wait for its loopback listener."""
import contextlib
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from .errors import BridgeError

LOG = logging.getLogger('guac-ssh')


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False


def default_log(port):
    if sys.platform == 'win32':
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Logs'
    else:
        root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return root / 'guac-bridge' / f'ssh-{port}.log'


def child_command(args):
    # Reuse this installation's interpreter, including uv tool environments.
    # Build an argument list; URLs/usernames are never interpreted by a shell.
    command = [sys.executable, '-m', 'guac_bridge.ssh_client']
    for name in ('url', 'username', 'port', 'target_host', 'target_port',
                 'max_connections', 'server_command', 'connection_id',
                 'data_source', 'transport', 'startup_delay', 'connect_timeout',
                 'ca_file'):
        value = getattr(args, name, None)
        if value is not None:
            command.extend(['--' + name.replace('_', '-'), str(value)])
    for name in ('insecure', 'no_keyring'):
        if getattr(args, name, False):
            command.append('--' + name.replace('_', '-'))
    return command


def stop_child(process):
    if process.poll() is None:
        if sys.platform == 'win32':
            # A virtual environment's python.exe may be a redirector which
            # starts another interpreter. Stop the tree, not just that wrapper.
            subprocess.run([str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                                'System32' / 'taskkill.exe'), '/PID', str(process.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
            if process.poll() is not None:
                return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextlib.contextmanager
def startup_lock(port, deadline):
    # VS Code and SSH multiplexers may launch several SSH processes at once.
    # Keep one background startup per user/port, including on Windows.
    path = default_log(port).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), 'r+b') as handle:
        # Lock beyond EOF without reading/writing: on Windows another owner's
        # byte-range lock also forbids even an initialization read of that byte.
        while True:
            try:
                handle.seek(0)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BridgeError('Timed out waiting for another bridge startup') from None
                time.sleep(.1)
        yield


def ensure_running(args):
    """Exit successfully only once a listener exists; own failed startups are stopped."""
    if listening(args.port):
        LOG.info('Using existing listener on 127.0.0.1:%s', args.port)
        return
    if not args.username:
        raise BridgeError('--ensure-running requires --username for background authentication')
    deadline = time.monotonic() + args.ready_timeout
    with startup_lock(args.port, deadline):
        if listening(args.port):
            LOG.info('Using existing listener on 127.0.0.1:%s', args.port)
            return
        _start_and_wait(args, deadline)


def _start_and_wait(args, deadline):
    log_path = Path(args.background_log).expanduser() if args.background_log else default_log(args.port)
    log_path = log_path.absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = {'stdin': subprocess.DEVNULL, 'close_fds': True}
    if sys.platform == 'win32':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab') as output:
        process = subprocess.Popen(child_command(args), stdout=output, stderr=output, **options)
    ready = False
    try:
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                raise BridgeError(f'Background bridge exited with status {code}; see {log_path}')
            if listening(args.port):
                ready = True
                LOG.info('Bridge ready on 127.0.0.1:%s (PID %s; log: %s)',
                         args.port, process.pid, log_path)
                return
            time.sleep(.1)
        raise BridgeError(f'Bridge did not become ready within {args.ready_timeout:g}s; see {log_path}')
    finally:
        if not ready:
            with contextlib.suppress(OSError):
                stop_child(process)
