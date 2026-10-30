"""Expose the remote server's SSH service on local loopback through Guacamole."""
import argparse
import asyncio
import base64
import contextlib
import getpass
import logging
import os
import shlex
import ssl
import time
import urllib.parse
import uuid

import aiohttp

from .client import Bridge, BridgeError, login, parse_url
from .console import setup_console, stop_hint

LOG = logging.getLogger('guac-ssh')
CHUNK = 4096
WINDOW = 8


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
                  '--max-connections', self.args.max_connections, '--connect-timeout', self.args.connect_timeout)
        command = executable + ' ' + ' '.join(shlex.quote(str(v)) for v in values) + '\n'
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
        queue = asyncio.Queue(maxsize=WINDOW + 4)
        self.requests[ident] = queue
        self.credits[ident] = asyncio.BoundedSemaphore(WINDOW)
        tasks = []
        try:
            await self.send_event({'type': 'open', 'id': ident})
            event = await self.wait_for(queue.get(), self.args.connect_timeout)
            if event['type'] != 'opened':
                raise BridgeError(event.get('message', 'SSH target did not open'))

            async def upload():
                while True:
                    await self.credits[ident].acquire()
                    data = await reader.read(CHUNK)
                    if not data:
                        self.credits[ident].release()
                        await self.send_event({'type': 'eof', 'id': ident})
                        return
                    await self.send_event({'type': 'data', 'id': ident,
                                           'body': base64.b64encode(data).decode()})

            async def download():
                while True:
                    event = await queue.get()
                    kind = event['type']
                    if kind == 'data':
                        data = base64.b64decode(event['body'], validate=True)
                        if len(data) > CHUNK:
                            raise BridgeError('TCP frame exceeds limit')
                        writer.write(data)
                        await writer.drain()
                        await self.send_event({'type': 'credit', 'id': ident})
                    elif kind in ('eof', 'closed'):
                        if writer.can_write_eof():
                            writer.write_eof()
                            await writer.drain()
                        return
                    elif kind == 'error':
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
            if not self.failed.is_set() and self.tunnel and not self.tunnel.closed:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.send_event({'type': 'close', 'id': ident}), 5)
            self.requests.pop(ident, None)
            self.credits.pop(ident, None)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.connections.discard(task)

    async def close(self):
        self.closing = True
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


async def serve(args):
    base, conn, kind, source = parse_url(args.url, args.connection_id, args.data_source)
    username = args.username or input('Guacamole username: ')
    password = os.environ.get('GUAC_PASSWORD') or getpass.getpass('Guacamole password: ')
    tls = ssl.create_default_context(cafile=args.ca_file)
    if args.insecure:
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        LOG.warning('TLS verification disabled by --insecure')
    connector = aiohttp.TCPConnector(ssl=tls)
    async with aiohttp.ClientSession(connector=connector, trust_env=True, auto_decompress=False,
                                     timeout=aiohttp.ClientTimeout(total=None, sock_connect=args.connect_timeout),
                                     headers={'Accept-Encoding': 'identity'}) as session:
        token = await login(session, base, username, password, args.connect_timeout)
        password = None
        bridge = SSHBridge(args, session, base, conn, kind, source, token)
        listener = None
        try:
            await bridge.start()
            listener = await asyncio.start_server(bridge.accept, '127.0.0.1', args.port)
            LOG.info('Listening on 127.0.0.1:%s', args.port)
            stop_hint()
            await bridge.failed.wait()
            raise BridgeError(bridge.failure)
        finally:
            if listener:
                listener.close()
            await bridge.close()
            if listener:
                await listener.wait_closed()
            with contextlib.suppress(Exception):
                async with session.delete(base + 'api/tokens/' + urllib.parse.quote(token, safe=''),
                                          timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as response:
                    await response.read()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Full Guacamole connection URL')
    parser.add_argument('--username', help='Guacamole username; otherwise prompted')
    parser.add_argument('--port', type=int, default=2222, help='Local loopback listener port')
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
    args = parser.parse_args()
    if not (1 <= args.port <= 65535 and 1 <= args.target_port <= 65535 and 1 <= args.max_connections <= 16):
        parser.error('Ports must be 1..65535 and connection limit must be 1..16')
    if args.connect_timeout <= 0 or args.startup_delay < 0:
        parser.error('Timeout must be positive and startup delay cannot be negative')
    setup_console('SSH')
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except (BridgeError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOG.error('%s', str(exc) if isinstance(exc, BridgeError) else type(exc).__name__ +
                  ': check network, proxy, TLS trust, and local port')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())

