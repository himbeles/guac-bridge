"""Authenticate in the SSH caller's console before starting a detached bridge."""
import contextlib
import getpass
import logging
import ssl
import sys
import urllib.parse
import warnings

import aiohttp

from .auth import CredentialStore, authenticate, credential_service
from .errors import BridgeError, CredentialStoreUnavailable

LOG = logging.getLogger('guac-ssh')


def windows_console_line(prompt, *, secret=False):
    from .console import _windows_console_output
    output = _windows_console_output()
    if output is None:
        raise BridgeError('No calling console for first login. Run guac-ssh-client --url URL --save-password interactively once')
    try:
        # getwch reads the attached console, independently of the hook's NUL stdin.
        # Never fall back to an echoed password or open an additional window.
        import msvcrt
        with open('CONIN$', 'r'):
            pass
        print(prompt, end='', file=output, flush=True)
        chars = []
        while True:
            char = msvcrt.getwch()
            if not char:
                raise EOFError
            if char in ('\r', '\n'):
                print(file=output, flush=True)
                return ''.join(chars)
            if char == '\x03':
                raise KeyboardInterrupt
            if char in ('\x00', '\xe0'):
                msvcrt.getwch()
            elif char == '\b':
                if chars:
                    chars.pop()
                    if not secret:
                        print('\b \b', end='', file=output, flush=True)
            else:
                chars.append(char)
                if not secret:
                    print(char, end='', file=output, flush=True)
    finally:
        output.close()


def prompt_hook_password():
    if sys.platform == 'win32':
        try:
            return windows_console_line('Guacamole password: ', secret=True)
        except (EOFError, OSError):
            raise BridgeError('Cannot read a password from the calling console') from None
    if sys.stdin is not None and sys.stdin.isatty():
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                return getpass.getpass('Guacamole password: ')
            except (getpass.GetPassWarning, EOFError):
                raise BridgeError('Cannot prompt securely in this console; save the password in a foreground connection') from None
    try:
        with open('/dev/tty', 'r'):
            pass
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass('Guacamole password: ')
    except (OSError, EOFError, getpass.GetPassWarning):
        raise BridgeError('No calling terminal for first login. Run guac-ssh-client --url URL --save-password interactively once') from None


class SavedStore:
    def __init__(self, store):
        self.store = store
        self.saved = False

    def get(self, *args):
        return self.store.get(*args)

    def set(self, *args):
        self.store.set(*args)
        self.saved = True


async def prepare_credentials(args, store=None):
    from .client import parse_url
    base, *_ = parse_url(args.url, args.connection_id, args.data_source)
    raw = CredentialStore() if store is None else store
    try:
        raw.get(credential_service(base), args.username)
    except CredentialStoreUnavailable:
        raise BridgeError('System credential storage is unavailable; cannot save a password for background startup. '
                          'Use a foreground bridge or GUAC_PASSWORD without --save-password.') from None
    saved = SavedStore(raw)
    tls = ssl.create_default_context(cafile=args.ca_file)
    if args.insecure:
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        LOG.warning('TLS verification disabled by --insecure')
    LOG.info('Checking Guacamole login before background startup')
    connector = aiohttp.TCPConnector(ssl=tls)
    async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
        token = await authenticate(session, base, args.username, args, store=saved, prompt=prompt_hook_password)
        try:
            if not saved.saved:
                raise BridgeError('Login succeeded, but the password could not be saved. Cannot start the background bridge; '
                                  'use a foreground connection or fix system credential storage.')
        finally:
            with contextlib.suppress(Exception):
                async with session.delete(base + 'api/tokens/' + urllib.parse.quote(token, safe=''),
                        timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as response:
                    await response.read()


def confirm_hook_update():
    while True:
        try:
            if sys.platform == 'win32':
                value = windows_console_line('Update server now? [Y/n] ')
            elif sys.stdin is not None and sys.stdin.isatty():
                print('Update server now? [Y/n] ', end='', file=sys.stderr, flush=True)
                value = input()
            else:
                with open('/dev/tty', 'r') as source, open('/dev/tty', 'w') as output:
                    print('Update server now? [Y/n] ', end='', file=output, flush=True)
                    value = source.readline()
                    if not value:
                        return False
        except (EOFError, OSError, BridgeError):
            LOG.warning('Cannot ask for update approval in the calling console; no installation started. '
                        'Reconnect in a foreground terminal with --update-server, or update manually.')
            return False
        value = value.strip().lower()
        if value in ('', 'y', 'yes'):
            return True
        if value in ('n', 'no'):
            return False
        LOG.info('Please enter yes or no.')
