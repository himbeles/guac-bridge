"""Private, per-startup request/response files for caller-console update consent."""
import asyncio
import contextlib
import json
import logging
import os

from . import __version__
from .errors import BridgeError

LOG = logging.getLogger('guac-ssh')


def paths(port, instance):
    from .background import default_log
    if not isinstance(instance, str) or len(instance) != 32 or any(c not in '0123456789abcdef' for c in instance):
        raise BridgeError('Invalid managed startup identity')
    root = default_log(port)
    return (root.with_suffix('.prompt-' + instance + '.json'),
            root.with_suffix('.answer-' + instance + '.json'))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(data, output)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        with path.open() as source:
            value = json.loads(source.read(4096))
    except ValueError:
        raise BridgeError('Invalid startup approval data') from None
    if not isinstance(value, dict):
        raise BridgeError('Invalid startup approval data')
    return value


async def request_update(bridge, probe):
    request, response = paths(bridge.args.port, bridge.args.managed_id)
    try:
        write_json(request, dict(instance=bridge.args.managed_id, action='update-server',
                                 remote_version=probe.remote_version, client_version=__version__))
        async def decision():
            while True:
                try:
                    value = read_json(response)
                except FileNotFoundError:
                    await asyncio.sleep(.1)
                    continue
                if value.get('instance') != bridge.args.managed_id or type(value.get('approved')) is not bool:
                    raise BridgeError('Invalid server update approval response')
                return value['approved']
        try:
            return await bridge.wait_for(decision(), 300)
        except asyncio.TimeoutError:
            raise BridgeError('Timed out waiting for server update approval; no installation was started') from None
    finally:
        cleanup(bridge.args.port, bridge.args.managed_id)


def service_request(args, instance):
    request, response = paths(args.port, instance)
    try:
        value = read_json(request)
    except FileNotFoundError:
        return None
    if (value.get('instance') != instance or value.get('action') != 'update-server'
            or value.get('client_version') != __version__):
        raise BridgeError('Invalid server update request from background bridge')
    version = value.get('remote_version')
    if not isinstance(version, str) or not 0 < len(version) <= 64 or not all(c.isalnum() or c in '.+-_!' for c in version):
        raise BridgeError('Invalid server version in update request')
    from .update import NOTICE
    from .hook_auth import confirm_hook_update
    LOG.warning('Server helper version: %s; client version: %s.', version, __version__)
    LOG.warning(NOTICE.format(version=__version__))
    approved = confirm_hook_update()
    write_json(response, dict(instance=instance, approved=approved))
    request.unlink(missing_ok=True)
    return approved


def cleanup(port, instance):
    for path in paths(port, instance):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
