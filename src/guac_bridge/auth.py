"""Shared Guacamole login and opt-in native credential storage."""
import getpass
import importlib
import logging
import os
import sys
import urllib.parse

import aiohttp

from .errors import AuthenticationError, BridgeError, CredentialStoreUnavailable

LOG = logging.getLogger('guac-auth')


def add_credential_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--save-password', action='store_true',
                       help='Save the Guacamole password in the system credential store after successful login')
    group.add_argument('--forget-password', action='store_true',
                       help='Delete the saved Guacamole password for this URL and username, then exit')
    group.add_argument('--no-keyring', action='store_true',
                       help='Ignore system credential storage for this invocation')


def credential_service(url):
    """One credential namespace per deployment; never include client IDs/tokens."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise BridgeError('Invalid Guacamole URL') from None
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None):
        raise BridgeError('Supply a Guacamole HTTP(S) URL without embedded credentials')
    host = parts.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    try:
        port = parts.port
    except ValueError:
        raise BridgeError('Invalid Guacamole URL port') from None
    if port is not None and port != (443 if parts.scheme == 'https' else 80):
        host += ':' + str(port)
    return 'guac-bridge:' + parts.scheme + '://' + host + parts.path.rstrip('/')


def _native_backend():
    # Select native stores explicitly. Never accept plaintext/file plugins or a
    # chainer which could silently fall back to an insecure backend.
    if sys.platform == 'win32':
        candidates = [('keyring.backends.Windows', 'WinVaultKeyring')]
    elif sys.platform == 'darwin':
        candidates = [('keyring.backends.macOS', 'Keyring')]
    else:
        candidates = [('keyring.backends.SecretService', 'Keyring'),
                      ('keyring.backends.kwallet', 'DBusKeyring')]
    for module, name in candidates:
        try:
            backend = getattr(importlib.import_module(module), name)()
            if backend.priority <= 0:
                continue
            if sys.platform == 'win32':
                backend.persist = 'local machine'
            return backend
        except Exception:
            # Native API exceptions may contain arbitrary credential details.
            # Report only the availability result, not their raw text.
            continue
    raise CredentialStoreUnavailable('System credential store is unavailable')


class CredentialStore:
    def __init__(self):
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            self._backend = _native_backend()
        return self._backend

    def get(self, service, username):
        try:
            value = self.backend.get_password(service, username)
            if value is not None and not isinstance(value, str):
                raise TypeError('Unexpected credential type')
            return value
        except Exception:
            raise CredentialStoreUnavailable('Cannot read the system credential store') from None

    def set(self, service, username, password):
        try:
            self.backend.set_password(service, username, password)
        except Exception:
            raise CredentialStoreUnavailable('Cannot write the system credential store') from None

    def delete(self, service, username):
        if self.get(service, username) is None:
            return False
        try:
            self.backend.delete_password(service, username)
            return True
        except Exception:
            raise CredentialStoreUnavailable('Cannot delete from the system credential store') from None


def resolve_username(args):
    username = args.username
    if not username:
        if sys.stdin is None or not sys.stdin.isatty():
            raise BridgeError('Supply --username when running without an interactive terminal')
        try:
            username = input('Guacamole username: ').strip()
        except EOFError:
            raise BridgeError('No Guacamole username supplied') from None
    if not username:
        raise BridgeError('A Guacamole username is required')
    return username


def prompt_password():
    if sys.stdin is None or not sys.stdin.isatty():
        raise BridgeError('No password available; use GUAC_PASSWORD, a saved credential, or an interactive terminal')
    try:
        return getpass.getpass('Guacamole password: ')
    except EOFError:
        raise BridgeError('No Guacamole password supplied') from None


def forget_credentials(url, username, store=None):
    store = CredentialStore() if store is None else store
    try:
        deleted = store.delete(credential_service(url), username)
    except CredentialStoreUnavailable:
        raise BridgeError('Cannot access the system credential store; saved password was not removed') from None
    LOG.info('Saved Guacamole password removed' if deleted else 'No saved Guacamole password found')


async def login(session, base, username, password, timeout):
    async with session.post(base + 'api/tokens', data={'username': username, 'password': password},
                            timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as response:
        if response.status != 200:
            error = AuthenticationError if response.status in (401, 403) else BridgeError
            raise error('Guacamole login returned HTTP ' + str(response.status) +
                        '. This tool supports the standard username/password login API; '
                        'SSO/MFA or an external login portal requires adaptation.')
        try:
            payload = await response.json(content_type=None)
            token = payload['authToken']
            if not isinstance(token, str) or not token:
                raise ValueError('Invalid token')
            return token
        except (ValueError, KeyError, TypeError):
            raise BridgeError('Login did not return a Guacamole authToken; check the URL base path') from None


async def authenticate(session, base, username, args, store=None):
    service = credential_service(base)
    enabled = not getattr(args, 'no_keyring', False)
    save = getattr(args, 'save_password', False)
    store = (CredentialStore() if store is None else store) if enabled else None
    password = os.environ.get('GUAC_PASSWORD')
    cached = False
    if password is None and enabled:
        try:
            password = store.get(service, username)
            cached = password is not None
        except CredentialStoreUnavailable:
            LOG.info('System credential store unavailable; using a password prompt')
            store = None
    if password is None:
        password = prompt_password()
    try:
        try:
            token = await login(session, base, username, password, args.connect_timeout)
        except AuthenticationError:
            if not cached:
                raise
            password = None
            LOG.warning('Saved Guacamole password was rejected; enter a replacement')
            password = prompt_password()
            token = await login(session, base, username, password, args.connect_timeout)
            save = True  # Replace the credential only after the new password works.
        if save and enabled:
            try:
                if store is None:
                    raise CredentialStoreUnavailable('No credential store')
                store.set(service, username, password)
                LOG.info('Guacamole password saved in the system credential store')
            except CredentialStoreUnavailable:
                LOG.warning('Login succeeded, but the password could not be saved in the system credential store')
        return token
    finally:
        password = None
