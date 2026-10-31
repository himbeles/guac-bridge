"""Opt-in uv tool update over a Guacamole output pipe, before helper startup."""
import asyncio
import base64
import json
import logging
import shlex
import uuid

from . import __version__
from .errors import BridgeError

LOG = logging.getLogger('guac-llm')
UPDATE_TIMEOUT = 240
NOTICE = ('Server update requested: if needed, this will run uv tool install --force '
          'guac-bridge=={version} on the server using its configured package index. '
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
    'verify-failed': 'The installed server helper does not report the requested version',
}


def add_update_argument(parser):
    parser.add_argument('--update-server', action='store_true',
        help=f'Before connecting, check and, if needed, attempt uv tool install --force guac-bridge=={__version__} '
             'on the server using its configured package index. Requires server-side uv and index access; '
             'otherwise update manually. Supports the default helper path; foreground connections only.')


def validate_update(args, helper):
    if not getattr(args, 'update_server', False):
        return
    if args.server_command != '~/.local/bin/' + helper:
        raise BridgeError('--update-server supports only the default uv tool helper path. '
                          'Update the custom --server-command installation manually.')
    if any(getattr(args, name, False) for name in ('ensure_running', 'stop', 'forget_password', 'managed_id')):
        raise BridgeError('--update-server requires a foreground connection. Stop an existing managed bridge '
                          'and run the client with --url and --update-server; otherwise update manually.')


class ServerUpdate:
    def __init__(self, bridge):
        self.bridge = bridge
        self.token = uuid.uuid4().hex
        self.future = asyncio.get_running_loop().create_future()
        self.active = True
        self.result = None

    def command(self):
        def emit(status):
            event = dict(type='bootstrap-update', token=self.token, status=status)
            encoded = base64.b64encode(json.dumps(event, separators=(',', ':')).encode()).decode()
            return "printf '%s\\n' " + shlex.quote(encoded)
        helper = '"$HOME"/.local/bin/' + shlex.quote(self.bridge.helper_name)
        version = shlex.quote(__version__)
        package = shlex.quote('guac-bridge==' + __version__)
        probe = f'installed=$(timeout --kill-after=5 10 {helper} --version 2>/dev/null) && [ "${{installed##* }}" = {version} ]'
        script = (
            "printf '\\033]482202;2;LLM\\007'; "
            "trap 'printf \"\\033]482203;\\007\"' EXIT; " + emit('checking') + '; '
            "if ! type -P timeout >/dev/null; then " + emit('no-timeout') + '; exit; fi; '
            'if ' + probe + '; then ' + emit('matched') + '; exit; fi; '
            'uv_bin=$(type -P uv); '
            'if [ -z "$uv_bin" ] && [ -x "$HOME/.local/bin/uv" ]; then uv_bin="$HOME/.local/bin/uv"; fi; '
            'if [ -z "$uv_bin" ]; then ' + emit('no-uv') + '; exit; fi; ' + emit('installing') + '; '
            f'timeout --kill-after=5 180 "$uv_bin" tool install --force {package} </dev/null >/dev/null 2>&1 & update_pid=$!; '
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
                              f'uv tool install --force guac-bridge=={__version__}. Then reconnect.') from None

    def dispatch(self, event):
        if event.get('type') != 'bootstrap-update':
            return False
        if event.get('token') != self.token:
            raise BridgeError('Unexpected server update response; update manually')
        status = event.get('status')
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
                f'uv tool install --force guac-bridge=={__version__}. Then reconnect.'))
