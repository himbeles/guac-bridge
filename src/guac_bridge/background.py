"""Start a detached SSH bridge and wait for its loopback listener."""
import contextlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from .errors import BridgeError
from .control import read_state, request

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


def state_path(port):
    return default_log(port).with_suffix('.json')


def startup_error_path(port, instance):
    return default_log(port).with_suffix('.startup-' + instance + '.json')


def record_startup_error(args, message):
    instance = getattr(args, 'managed_id', None)
    if (not isinstance(instance, str) or len(instance) != 32
            or any(c not in '0123456789abcdef' for c in instance)
            or getattr(args, '_managed_ready', False)):
        return
    path = startup_error_path(args.port, instance)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump({'error': message}, output)


def stop_managed(port, timeout):
    deadline = time.monotonic() + timeout
    with startup_lock(port, deadline):
        state = read_state(state_path(port))
        if state is None:
            if listening(port):
                raise BridgeError('This listener is not managed; stop it in its original terminal')
            LOG.info('No managed bridge is running on port %s', port)
            return
        try:
            request(state, 'stop')
        except BridgeError:
            # Closing a visible console may bypass Python's cleanup. A stale
            # record must never be used as authority to kill a PID or listener.
            if listening(port):
                raise
            current = read_state(state_path(port))
            if current and current['instance'] == state['instance']:
                state_path(port).unlink(missing_ok=True)
            LOG.info('No managed bridge is running on port %s', port)
            return
        while time.monotonic() < deadline:
            current = read_state(state_path(port))
            if current is None or current['instance'] != state['instance']:
                LOG.info('Stopped bridge on port %s', port)
                return
            time.sleep(.1)
        raise BridgeError('Timed out waiting for the bridge to stop')


def child_command(args):
    # Reuse this installation's interpreter, including uv tool environments.
    # Build an argument list; URLs/usernames are never interpreted by a shell.
    command = [sys.executable, '-m', 'guac_bridge.ssh_client']
    for name in ('url', 'username', 'port', 'target_host', 'target_port',
                 'max_connections', 'server_command', 'connection_id',
                 'data_source', 'transport', 'startup_delay', 'connect_timeout',
                 'ca_file', 'window', 'frame_size'):
        value = getattr(args, name, None)
        if value is not None:
            command.extend(['--' + name.replace('_', '-'), str(value)])
    for name in ('insecure', 'no_keyring', 'ignore_server_version', 'require_server_version'):
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


def report_version_policy(args, report):
    if getattr(args, 'require_server_version', False) and (not report or report.get('version_verified') is not True):
        raise BridgeError('Cannot verify matching versions for this running bridge. '
                          f'Stop it with guac-ssh-client --stop --port {args.port}, '
                          'then reconnect with --require-server-version.')
    if report and isinstance(report.get('version_warning'), str):
        LOG.warning('%s', report['version_warning'])


def use_existing(args):
    report = None
    state = read_state(state_path(args.port))
    if state:
        with contextlib.suppress(BridgeError):
            report = request(state, 'status')
    report_version_policy(args, report)
    LOG.info('Using existing listener on 127.0.0.1:%s', args.port)


def ensure_running(args):
    """Exit successfully only once a listener exists; own failed startups are stopped."""
    if listening(args.port):
        use_existing(args)
        return
    if not args.username:
        raise BridgeError('--ensure-running requires --username for background authentication')
    deadline = time.monotonic() + args.ready_timeout
    with startup_lock(args.port, deadline):
        if listening(args.port):
            use_existing(args)
            return
        _start_and_wait(args, deadline)


def _start_and_wait(args, deadline):
    log_path = Path(args.background_log).expanduser() if args.background_log else default_log(args.port)
    log_path = log_path.absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Windows reports NUL (DEVNULL) as a TTY. A closed pipe reliably prevents
    # getpass/input from trying to prompt in the detached process.
    visible = getattr(args, 'show_console', False)
    instance = uuid.uuid4().hex
    error_path = startup_error_path(args.port, instance)
    command = child_command(args) + ['--managed-id', instance]
    options = {'close_fds': True}
    if visible:
        command.append('--visible-console')
        options['creationflags'] = subprocess.CREATE_NEW_CONSOLE
        process = subprocess.Popen(command, **options)
        destination = 'visible bridge console'
    else:
        options['stdin'] = subprocess.PIPE
        if sys.platform == 'win32':
            # DETACHED_PROCESS leaves the venv launcher without a console, so
            # its interpreter child may allocate a visible, empty one. Give
            # the entire interpreter tree a windowless console instead.
            options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab') as output:
            process = subprocess.Popen(command, stdout=output, stderr=output, **options)
        process.stdin.close()
        destination = str(log_path)
    ready = False
    try:
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                    error = json.loads(error_path.read_text())['error']
                    if isinstance(error, str):
                        raise BridgeError(error)
                raise BridgeError(f'Background bridge exited with status {code}; see {destination}')
            state = read_state(state_path(args.port))
            if state and state['instance'] == instance:
                report = request(state, 'status')
                report_version_policy(args, report)
                ready = True
                LOG.info('Bridge ready on 127.0.0.1:%s (PID %s; output: %s)',
                         args.port, state['pid'], destination)
                return
            time.sleep(.1)
        raise BridgeError(f'Bridge did not become ready within {args.ready_timeout:g}s; see {destination}')
    finally:
        if not ready:
            with contextlib.suppress(OSError):
                stop_child(process)
        with contextlib.suppress(OSError):
            error_path.unlink(missing_ok=True)
