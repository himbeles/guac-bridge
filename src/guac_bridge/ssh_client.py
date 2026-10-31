"""Expose the remote server's SSH service on local loopback through Guacamole."""
import argparse
import asyncio
import base64
import contextlib
import errno
import logging
import math
import shlex
import ssl
import time
import urllib.parse
import uuid

import aiohttp

from . import __version__
from .flow import add_flow_arguments
from .update import add_update_argument, validate_update
from .client import Bridge, BridgeError, parse_url
from .console import setup_console, stop_hint
from .auth import (add_credential_arguments, authenticate, forget_credentials,
                   resolve_username)
from .background import ensure_running, record_hook_error, record_startup_error, state_path, stop_managed
from .control import ControlServer

LOG = logging.getLogger('guac-ssh')


async def start_listener(accept, port):
    try:
        return await asyncio.start_server(accept, '127.0.0.1', port)
    except OSError as exc:
        codes = {exc.errno, getattr(exc, 'winerror', None)}
        address = f'127.0.0.1:{port}'
        if codes & {errno.EADDRINUSE, 10048}:
            message = (f'Cannot listen on {address}: this local port is already in use. '
                       f'To stop a managed SSH bridge, run guac-ssh-client --stop --port {port}. '
                       'Otherwise stop the program using that port, or choose a different local port with --port.')
        elif codes & {errno.EACCES, errno.EPERM, 10013}:
            message = (f'Cannot listen on {address}: the operating system denied access. '
                       'The local port may be reserved or blocked. Choose a different local port with --port '
                       'or check local port reservations and permissions.')
        else:
            code = getattr(exc, 'winerror', None) or exc.errno
            detail = f' (OS error {code})' if code is not None else ''
            message = (f'Cannot open the local listener on {address}{detail}. '
                       'Check local port availability, reservations, and permissions, '
                       'or choose a different local port with --port.')
        raise BridgeError(message) from None


class SSHBridge(Bridge):
    helper_name = 'guac-ssh-server'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.credits = {}
        self.connections = set()

    def helper_command(self):
        executable = ('"$HOME"/' + shlex.quote(self.args.server_command[2:])
                      if self.args.server_command.startswith('~/') else shlex.quote(self.args.server_command))
        values = ('--stdio', '--target-host', self.args.target_host, '--target-port', self.args.target_port,
                  '--max-connections', self.args.max_connections, '--connect-timeout', self.args.connect_timeout,
                  '--window', self.data_window, '--frame-size', self.tcp_chunk)
        command = 'exec ' + executable + ' ' + ' '.join(shlex.quote(str(v)) for v in values) + '\n'
        if len(command.encode()) > 3900:
            raise BridgeError('Helper command exceeds terminal line limit')
        return command.encode()

    async def send_event(self, event):
        # A connection cancellation must not interrupt a frame shared with other
        # TCP connections, leaving an incomplete Base64 line in the remote PTY.
        task = asyncio.create_task(super().send_event(event))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await task
            raise

    def dispatch(self, event):
        if event.get('type') == 'ready' and event.get('protocol') != 'ssh-tcp':
            raise BridgeError('Expected guac-ssh-server; incorrect remote helper')
        if event.get('type') == 'credit':
            self.last_seen = time.monotonic()
            semaphore = self.credits.get(event.get('id'))
            if semaphore:
                semaphore.release()
        else:
            super().dispatch(event)

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        if self.failed.is_set() or self.closing or len(self.requests) >= self.args.max_connections:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        self.connections.add(task)
        ident = uuid.uuid4().hex
        queue = asyncio.Queue(maxsize=self.data_window + 4)
        self.requests[ident] = queue
        self.credits[ident] = asyncio.BoundedSemaphore(self.data_window)
        tasks = []
        remote_closed = False
        try:
            await self.send_event({'type': 'open', 'id': ident})
            event = await self.wait_for(queue.get(), self.args.connect_timeout)
            if event['type'] != 'opened':
                remote_closed = event['type'] in ('error', 'closed')
                raise BridgeError(event.get('message', 'SSH target did not open'))

            async def upload():
                while True:
                    await self.credits[ident].acquire()
                    data = await reader.read(self.tcp_chunk)
                    if not data:
                        self.credits[ident].release()
                        await self.send_event({'type': 'eof', 'id': ident})
                        return
                    await self.send_event({'type': 'data', 'id': ident,
                                           'body': base64.b64encode(data).decode()})

            async def download():
                nonlocal remote_closed
                while True:
                    event = await queue.get()
                    kind = event['type']
                    if kind == 'data':
                        data = base64.b64decode(event['body'], validate=True)
                        if len(data) > self.tcp_chunk:
                            raise BridgeError('TCP frame exceeds limit')
                        writer.write(data)
                        await writer.drain()
                        await self.send_event({'type': 'credit', 'id': ident})
                    elif kind in ('eof', 'closed'):
                        remote_closed = kind == 'closed'
                        if writer.can_write_eof():
                            writer.write_eof()
                            await writer.drain()
                        return
                    elif kind == 'error':
                        remote_closed = True
                        raise BridgeError(event['message'])
                    else:
                        raise BridgeError('Unexpected TCP relay event')

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        except (BridgeError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
            LOG.warning('%s', str(exc) if isinstance(exc, BridgeError) else 'SSH TCP connection interrupted')
            if writer.transport:
                writer.transport.abort()
        finally:
            for child in tasks:
                child.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not remote_closed and not self.failed.is_set() and self.tunnel and not self.tunnel.closed:
                try:
                    await asyncio.wait_for(self.close_channel(ident, queue), 5)
                except (Exception, asyncio.CancelledError):
                    self.fail('Remote SSH channel close did not complete; restart the bridge')
            self.requests.pop(ident, None)
            self.credits.pop(ident, None)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.connections.discard(task)

    async def close_channel(self, ident, queue):
        await self.send_event({'type': 'close', 'id': ident})
        while True:
            event = await queue.get()
            if event['type'] in ('closed', 'error'):
                return

    async def close(self):
        self.closing = True
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


async def serve(args):
    username = resolve_username(args)
    if getattr(args, 'forget_password', False):
        forget_credentials(args.url, username)
        return
    base, conn, kind, source = parse_url(args.url, args.connection_id, args.data_source)
    tls = ssl.create_default_context(cafile=args.ca_file)
    if args.insecure:
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        LOG.warning('TLS verification disabled by --insecure')
    connector = aiohttp.TCPConnector(ssl=tls)
    async with aiohttp.ClientSession(connector=connector, trust_env=True, auto_decompress=False,
                                     timeout=aiohttp.ClientTimeout(total=None, sock_connect=args.connect_timeout),
                                     headers={'Accept-Encoding': 'identity'}) as session:
        token = await authenticate(session, base, username, args)
        bridge = SSHBridge(args, session, base, conn, kind, source, token)
        listener = None
        control = None
        waiters = []
        try:
            await bridge.start()
            listener = await start_listener(bridge.accept, args.port)
            if getattr(args, 'managed_id', None):
                control = await ControlServer(state_path(args.port), args.managed_id,
                    version_verified=bridge.server_version_matched, version_warning=bridge.version_warning).start()
                args._managed_ready = True
            LOG.info('Listening on 127.0.0.1:%s', args.port)
            if control:
                LOG.info('Stop with guac-ssh-client --stop --port %s', args.port)
            if control is None or getattr(args, 'visible_console', False):
                stop_hint()
            waiters = [asyncio.create_task(bridge.failed.wait())]
            if control:
                waiters.append(asyncio.create_task(control.stopped.wait()))
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if bridge.failed.is_set():
                raise BridgeError(bridge.failure)
        finally:
            if listener:
                listener.close()
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            await bridge.close()
            if listener:
                await listener.wait_closed()
            with contextlib.suppress(Exception):
                async with session.delete(base + 'api/tokens/' + urllib.parse.quote(token, safe=''),
                                          timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as response:
                    await response.read()
            if control:
                await control.close()


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('--url', help='Full Guacamole connection URL')
    parser.add_argument('--username', help='Guacamole username; otherwise prompted')
    add_credential_arguments(parser)
    add_flow_arguments(parser, ssh=True)
    add_update_argument(parser)
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument('--require-server-version', action='store_true',
                          help='Require matching client/server package versions instead of warning')
    versions.add_argument('--ignore-server-version', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=2222, help='Local loopback listener port')
    management = parser.add_mutually_exclusive_group()
    management.add_argument('--ensure-running', action='store_true',
                        help='Reuse a local listener or start a detached bridge; exit when ready')
    management.add_argument('--stop', action='store_true', help='Stop the managed bridge on --port and exit')
    parser.add_argument('--show-console', action='store_true', help='Open a visible bridge console on Windows with --ensure-running')
    parser.add_argument('--managed-id', help=argparse.SUPPRESS)
    parser.add_argument('--visible-console', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ready-timeout', type=float, default=60,
                        help='Maximum seconds to wait for background startup (default: 60)')
    parser.add_argument('--background-log', help='Background log file (default: user cache/guac-bridge/ssh-PORT.log)')
    parser.add_argument('--target-host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    parser.add_argument('--target-port', type=int, default=22, help='Server-side loopback SSH port')
    parser.add_argument('--max-connections', type=int, default=8)
    parser.add_argument('--server-command', default='~/.local/bin/guac-ssh-server', help='Remote helper executable path')
    parser.add_argument('--connection-id')
    parser.add_argument('--data-source')
    parser.add_argument('--transport', choices=('auto', 'websocket', 'http'), default='auto')
    parser.add_argument('--startup-delay', type=float, default=2)
    parser.add_argument('--connect-timeout', type=float, default=30)
    parser.add_argument('--ca-file', help='Corporate CA PEM bundle')
    parser.add_argument('--insecure', action='store_true', help='Disable Guacamole TLS verification')
    args = parser.parse_args(argv)
    if not args.stop and not args.url:
        parser.error('--url is required unless using --stop')
    if args.show_console:
        import sys
        if not args.ensure_running or sys.platform != 'win32':
            parser.error('--show-console requires --ensure-running on Windows')
    if not (1 <= args.port <= 65535 and 1 <= args.target_port <= 65535 and 1 <= args.max_connections <= 16):
        parser.error('Ports must be 1..65535 and connection limit must be 1..16')
    if not math.isfinite(args.connect_timeout) or not math.isfinite(args.startup_delay) or args.connect_timeout <= 0 or args.startup_delay < 0:
        parser.error('Timeout must be positive and startup delay cannot be negative')
    if not math.isfinite(args.ready_timeout) or args.ready_timeout <= 0:
        parser.error('Ready timeout must be positive and finite')
    if ((args.ensure_running or args.stop) and args.forget_password) or (args.stop and args.save_password):
        parser.error('Use credential management options in an interactive foreground invocation')
    setup_console('SSH', hook=args.ensure_running)
    try:
        validate_update(args, 'guac-ssh-server')
        if args.stop:
            stop_managed(args.port, args.ready_timeout)
        elif args.ensure_running:
            ensure_running(args)
        else:
            asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except (BridgeError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        message = (str(exc) if isinstance(exc, BridgeError) else type(exc).__name__ +
                   ': check network, proxy, TLS trust, and local port')
        record_startup_error(args, message)
        record_hook_error(args, message)
        LOG.error('%s', message)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())


