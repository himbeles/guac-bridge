"""Opt-in uv tool update over a Guacamole output pipe, before helper startup."""
import asyncio
import base64
import json
import logging
import shlex
import sys
import threading
import uuid

from . import __version__
from .errors import BridgeError

LOG = logging.getLogger('guac-llm')
UPDATE_TIMEOUT = 240
PROBE_TIMEOUT = 35
NOTICE = ('Server update requested: if needed, this will run uv tool install --force '
          'guac-bridge=={version} on the server using its configured package index and an explicit --python selection. '
          'If uv or the index is unavailable, update the server manually.')
MESSAGES = {
    'checking': 'Checking the installed server helper version',
    'installing': 'Attempting server update with uv tool and the server\'s configured package index (up to 180 seconds)',
    'matched': 'Server helper already matches the client; no installation needed',
    'updated': 'Server package updated and verified',
}
FAILURES = {
    'no-uv': 'uv is not available on the server',
    'no-timeout': 'The server timeout utility is unavailable; cannot bound the update',
    'install-failed': 'uv tool installation failed or timed out; the configured package index may be unavailable',
    'python-unknown': 'Cannot identify the existing helper Python; retry with --server-python 3.XX or update manually',
    'probe-failed': 'Cannot read the server helper version (missing executable, failed --version, or timeout)',
    'verify-failed': 'The installed server helper does not report the requested version',
}


def add_update_argument(parser):
    parser.add_argument('--server-python', metavar='PYTHON',
        help='Python version or interpreter path for --update-server, e.g. 3.12. By default reuse the existing helper interpreter.')
    parser.add_argument('--update-server', action='store_true',
        help=f'Before connecting, check and, if needed, attempt uv tool install --force guac-bridge=={__version__} '
             'on the server using its configured package index and an explicit --python selection. Requires server-side uv and index access; '
             'otherwise update manually. Supports the default helper path; foreground connections only. '
             'Without this flag, interactive foreground connections offer an update on mismatch [Y/n].')


def validate_update(args, helper):
    if not getattr(args, 'update_server', False):
        if getattr(args, 'server_python', None):
            raise BridgeError('--server-python requires --update-server')
        return
    if args.server_command != '~/.local/bin/' + helper:
        raise BridgeError('--update-server supports only the default uv tool helper path. '
                          'Update the custom --server-command installation manually.')
    if any(getattr(args, name, False) for name in ('ensure_running', 'stop', 'forget_password', 'managed_id')):
        raise BridgeError('--update-server requires a foreground connection. Stop an existing managed bridge '
                          'and run the client with --url and --update-server; otherwise update manually.')


async def offer_update(bridge, probe):
    args = bridge.args
    if (probe.result not in ('mismatched', 'unsupported-options')
            or any(getattr(args, name, False) for name in ('managed_id', 'ensure_running'))
            or sys.stdin is None or not sys.stdin.isatty()
            or args.server_command != '~/.local/bin/' + bridge.helper_name):
        return False
    LOG.warning('Server helper version: %s; client version: %s.', probe.remote_version, __version__)
    LOG.warning(NOTICE.format(version=__version__))
    loop = asyncio.get_running_loop()
    answer = loop.create_future()

    def deliver(value):
        if not answer.done():
            answer.set_result(value)

    def read_answer():
        while True:
            try:
                print('Update server now? [Y/n] ', end='', file=sys.stderr, flush=True)
                value = input().strip().lower()
            except (EOFError, OSError):
                value = 'n'
            if value in ('', 'y', 'yes', 'n', 'no'):
                try:
                    loop.call_soon_threadsafe(deliver, value in ('', 'y', 'yes'))
                except RuntimeError:
                    pass  # The connection/process may have been cancelled.
                return
            print('Please enter yes or no.', file=sys.stderr, flush=True)

    # Keep the websocket reader alive while waiting. A daemon thread avoids
    # blocking interpreter shutdown if Ctrl+C or a disconnect cancels the prompt.
    threading.Thread(target=read_answer, daemon=True).start()
    return await bridge.wait_for(answer, None)


class ServerUpdate:
    def __init__(self, bridge):
        self.bridge = bridge
        self.token = uuid.uuid4().hex
        self.future = asyncio.get_running_loop().create_future()
        self.active = True
        self.result = None
        self.remote_version = None

    def command(self):
        def emit(status):
            event = dict(type='bootstrap-update', token=self.token, status=status)
            encoded = base64.b64encode(json.dumps(event, separators=(',', ':')).encode()).decode()
            return "printf '%s\\n' " + shlex.quote(encoded)
        helper = '"$HOME"/.local/bin/' + shlex.quote(self.bridge.helper_name)
        version = shlex.quote(__version__)
        package = shlex.quote('guac-bridge==' + __version__)
        probe = f'installed=$(timeout --kill-after=5 10 {helper} --version 2>/dev/null) && [ "${{installed##* }}" = {version} ]'
        requested = getattr(getattr(self.bridge, 'args', None), 'server_python', None)
        if requested:
            python_setup = 'server_python=' + shlex.quote(requested) + '; '
        else:
            python_setup = (
                '{ IFS= read -r shebang < ' + helper + '; } 2>/dev/null; candidate=${shebang#\"#!\"}; '
                'server_python=; if [[ "$candidate" == /* ]] && [ -x "$candidate" ]; then '
                'server_python=$(timeout --kill-after=5 10 "$candidate" -I -c '
                + shlex.quote('import sys;print(getattr(sys,"_base_executable",sys.executable))')
                + ' 2>/dev/null); fi; '
                'if [ -z "$server_python" ] || [ ! -x "$server_python" ]; then '
                + emit('python-unknown') + '; exit; fi; ')
        script = (
            "printf '\\033]482202;2;LLM\\007'; "
            "trap 'printf \"\\033]482203;\\007\"' EXIT; " + emit('checking') + '; '
            "if ! type -P timeout >/dev/null; then " + emit('no-timeout') + '; exit; fi; '
            'if ' + probe + '; then ' + emit('matched') + '; exit; fi; '
            'uv_bin=$(type -P uv); '
            'if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then uv_bin="$HOME/.local/bin/uv"; fi; '
            'if [ -z "$uv_bin" ]; then ' + emit('no-uv') + '; exit; fi; ' + python_setup + emit('installing') + '; '
            f'timeout --kill-after=5 180 "$uv_bin" tool install --force --python "$server_python" {package} </dev/null >/dev/null 2>&1 & update_pid=$!; '
            "trap 'kill -TERM \"$update_pid\" 2>/dev/null; exit 1' HUP INT TERM; "
            'if wait "$update_pid"; then trap - HUP INT TERM; '
            'if ' + probe + '; then ' + emit('updated') + '; else ' + emit('verify-failed') + '; fi; '
            'else trap - HUP INT TERM; ' + emit('install-failed') + '; fi'
        )
        command = ('bash -c ' + shlex.quote(script) + '\n').encode()
        if len(command) > 3900:
            raise BridgeError('Server update command exceeds terminal line limit; update manually')
        return command

    async def run(self):
        LOG.warning(NOTICE.format(version=__version__))
        await self.bridge.send_input(self.command())
        await self.bridge.drain_input()
        try:
            await self.bridge.wait_for(self.future, UPDATE_TIMEOUT)
        except asyncio.TimeoutError:
            raise BridgeError('Server update did not complete in time. Update manually on the server with: '
                              f'uv tool install --force --python 3.XX guac-bridge=={__version__} '
                              '(replace 3.XX with the helper Python version). Then reconnect.') from None

    def dispatch(self, event):
        if event.get('type') != 'bootstrap-update':
            return False
        if event.get('token') != self.token:
            raise BridgeError('Unexpected server update response; update manually')
        status = event.get('status')
        version = event.get('version')
        if isinstance(version, str) and 0 < len(version) <= 64 and all(c.isalnum() or c in '.+-_!' for c in version):
            self.remote_version = version
            LOG.info('Server helper version: %s; client version: %s', version, __version__)
        if status in ('mismatched', 'unsupported-options'):
            self.result = status
            return True
        if status in MESSAGES:
            LOG.info('%s', MESSAGES[status])
        elif status not in FAILURES:
            raise BridgeError('Invalid server update response; update manually')
        if status in ('matched', 'updated') or status in FAILURES:
            self.result = status
        return True

    def ended(self):
        self.active = False
        if self.future.done():
            return
        if self.result in ('matched', 'updated'):
            self.future.set_result(None)
        else:
            reason = FAILURES.get(self.result, 'No verified update result was received')
            self.future.set_exception(BridgeError(reason + '. Update manually on the server with: '
                f'uv tool install --force --python 3.XX guac-bridge=={__version__} '
                              '(replace 3.XX with the helper Python version). Then reconnect.'))


class HelperProbe(ServerUpdate):
    """Check version/options before launching a helper with bridge arguments."""
    def command(self):
        def emit(status, version=False):
            event = dict(type='bootstrap-update', token=self.token, status=status)
            if not version:
                encoded = base64.b64encode(json.dumps(event, separators=(',', ':')).encode()).decode()
                return "printf '%s\\n' " + shlex.quote(encoded)
            prefix = json.dumps(event, separators=(',', ':'))[:-1] + ',"version":"'
            return ("printf '%s' " + shlex.quote(prefix) + ' "$installed" ' + shlex.quote('"}')
                    + " | base64 -w0; printf '\\n'")
        path = getattr(getattr(self.bridge, 'args', None), 'server_command', '~/.local/bin/' + self.bridge.helper_name)
        helper = ('"$HOME"/' + shlex.quote(path[2:]) if path.startswith('~/') else shlex.quote(path))
        required = '[[' + ' "$help" == *--window* ]]'
        if self.bridge.helper_name == 'guac-ssh-server':
            required += ' && [[ "$help" == *--frame-size* ]]'
        script = ("printf '\\033]482202;2;LLM\\007'; trap 'printf \"\\033]482203;\\007\"' EXIT; "
            'if ! type -P timeout >/dev/null; then ' + emit('no-timeout') + '; exit; fi; '
            f'if ! installed=$(timeout --kill-after=5 10 {helper} --version 2>/dev/null); then '
            + emit('probe-failed') + '; exit; fi; installed=${installed##* }; '
            'if ! [[ "$installed" =~ ^[a-zA-Z0-9.+_!-]{1,64}$ ]] || ! type -P base64 >/dev/null; then '
            + emit('probe-failed') + '; exit; fi; '
            'if [ "$installed" = ' + shlex.quote(__version__) + ' ]; then ' + emit('matched', True) + '; exit; fi; '
            f'help=$(timeout --kill-after=5 10 {helper} --help 2>/dev/null); '
            'if ' + required + '; then ' + emit('mismatched', True) + '; else '
            + emit('unsupported-options', True) + '; fi')
        command = ('bash -c ' + shlex.quote(script) + '\n').encode()
        if len(command) > 3900:
            raise BridgeError('Helper preflight exceeds terminal line limit; shorten --server-command')
        return command

    async def run(self):
        LOG.info('Checking server helper version before startup')
        await self.bridge.send_input(self.command())
        await self.bridge.drain_input()
        try:
            await self.bridge.wait_for(self.future, PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            raise BridgeError('Server helper version check timed out. Check the helper installation '
                              'and shell readiness, or use --update-server.') from None

    def ended(self):
        if self.future.done():
            self.active = False
            return
        if self.result in ('mismatched', 'unsupported-options'):
            self.active = False
            self.future.set_result(None)
            return
        super().ended()

    def check_result(self):
        if self.result not in ('mismatched', 'unsupported-options'):
            return
        strict = getattr(self.bridge.args, 'require_server_version', False)
        if self.result == 'mismatched' and not strict:
            return
        reason = ('does not support the required bridge arguments' if self.result == 'unsupported-options'
                  else 'does not match --require-server-version')
        raise BridgeError(
            f'Server helper {self.bridge.helper_name} version: {self.remote_version or "unknown"}; '
            f'client version: {__version__}. The helper {reason}. '
            f'On the server, run: uv tool install --force --python 3.XX guac-bridge=={__version__} '
            '(use the helper Python version). Or reconnect with --update-server; '
            'this attempts uv tool installation using the server package index. '
            'For a custom --server-command, update that installation manually.')
