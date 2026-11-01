#!/usr/bin/env python3
"""Local OpenAI HTTP endpoint over an authenticated Apache Guacamole terminal."""
import argparse
import asyncio
import base64
from collections import deque
import contextlib
import json
import logging
import os
import shlex
import ssl
import sys
import time
import urllib.parse

import aiohttp
from aiohttp import web

from . import __version__
from .framing import Decoder, encode_event
from .console import setup_console, stop_hint
from .auth import (add_credential_arguments, authenticate, forget_credentials,
                   login, resolve_username)
from .errors import BridgeError
from .flow import WINDOW, CHUNK, PROTOCOL_VERSION, add_flow_arguments, settings
from .update import HelperProbe, ServerUpdate, add_update_argument, offer_update, validate_update

LOG = logging.getLogger('guac-llm')
PATHS = {'/v1/models': 'GET', '/v1/chat/completions': 'POST', '/v1/completions': 'POST'}


def instruction(*args):
    return ','.join(str(len(str(arg))) + '.' + str(arg) for arg in args) + ';'


class Parser:
    """Incremental Guacamole length-prefixed instruction parser."""
    def __init__(self):
        self.buffer = ''

    def feed(self, text):
        self.buffer += text
        if len(self.buffer) > 16 * 1024 * 1024:
            raise BridgeError('Guacamole instruction exceeds size limit')
        result = []
        start = 0
        while start < len(self.buffer):
            pos, args = start, []
            while True:
                dot = self.buffer.find('.', pos)
                if dot < 0:
                    self.buffer = self.buffer[start:]
                    return result
                length = self.buffer[pos:dot]
                if not length.isdecimal() or len(length) > 9:
                    raise BridgeError('Invalid Guacamole instruction length')
                end = dot + 1 + int(length)
                if end >= len(self.buffer):
                    self.buffer = self.buffer[start:]
                    return result
                args.append(self.buffer[dot + 1:end])
                separator = self.buffer[end]
                pos = end + 1
                if separator == ';':
                    result.append(args)
                    start = pos
                    break
                if separator != ',':
                    raise BridgeError('Invalid Guacamole separator')
        self.buffer = self.buffer[start:]
        return result


def parse_url(url, connection=None, data_source=None):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
        raise BridgeError('Supply an http(s) Guacamole URL without embedded credentials')
    kind = 'c'
    if '/client/' in parts.fragment:
        encoded = urllib.parse.unquote(parts.fragment.split('/client/', 1)[1].split('?')[0])
        try:
            decoded = base64.b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
            found_id, kind, found_source = decoded.split('\0')
        except (ValueError, UnicodeError) as exc:
            raise BridgeError('Cannot decode connection from URL; use --connection-id and --data-source') from exc
        connection = connection or found_id
        data_source = data_source or found_source
    if not connection or not data_source or kind not in ('c', 'g'):
        raise BridgeError('Include #/client/... in URL, or supply --connection-id and --data-source')
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') + '/', '', ''))
    return base, connection, kind, data_source


def bootstrap(args):
    executable = ('"$HOME"/' + shlex.quote(args.server_command[2:])
                  if args.server_command.startswith('~/') else shlex.quote(args.server_command))
    command = 'exec ' + executable + ' ' + ' '.join(shlex.quote(str(value)) for value in (
        '--upstream', args.upstream,
        '--request-timeout', args.request_timeout, '--max-requests', args.max_requests,
        '--window', getattr(args, 'window', WINDOW))) + '\n'
    if len(command.encode()) > 3900:
        raise BridgeError('Remote bootstrap exceeds terminal canonical line limit')
    return command.encode()


class Tunnel:
    def __init__(self, session, base, params):
        self.session, self.base, self.params = session, base, params
        self.mode = 'websocket'
        self.ws = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def connect(self):
        url = self.base.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
        try:
            self.ws = await self.session.ws_connect(
                url + 'websocket-tunnel', params=self.params, protocols=('guacamole',),
                heartbeat=20, max_msg_size=16 * 1024 * 1024,
                origin=urllib.parse.urlunsplit((*urllib.parse.urlsplit(self.base)[:2], '', '', '')))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError('Guacamole WebSocket tunnel failed. Check the URL, network/proxy and WebSocket support.') from exc

    async def send(self, *args):
        payload = instruction(*args)
        async with self.lock:
            if self.closed or self.ws is None:
                raise BridgeError('Guacamole tunnel is closed')
            await self.ws.send_str(payload)

    async def messages(self):
        async for message in self.ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                yield message.data
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise BridgeError('Guacamole WebSocket tunnel failed')

    async def close(self):
        self.closed = True
        if self.ws:
            await self.ws.close()


class Bridge:
    input_window = WINDOW
    response_queue_size = WINDOW + 8
    helper_name = 'guac-llm-server'

    def helper_command(self):
        return bootstrap(self.args)

    def __init__(self, args, session, base, conn, kind, data_source, token):
        self.args, self.session = args, session
        self.base, self.conn, self.kind, self.data_source = base, conn, kind, data_source
        self.token = token
        self.tunnel = None
        self.updater = None
        self.reader = None
        self.maintainer = None
        self.synced = asyncio.Event()
        self.ready = asyncio.Event()
        self.failed = asyncio.Event()
        self.failure = None
        self.input_lock = asyncio.Lock()
        self.acks = asyncio.Queue()
        self.pending_input = deque()
        self.ack_deadlines = {}
        self.input_stream = 0
        self.output_stream = None
        self.output_buffer = b''
        self.decoder = Decoder()
        self.requests = {}
        self.last_seen = time.monotonic()
        self.closing = False
        self.server_version_matched = False
        self.version_warning = None
        self.flow = settings(args, ssh=self.helper_name == 'guac-ssh-server')
        self.data_window = self.flow['window']
        self.input_window = 2 * self.data_window
        self.next_id = 1
        self.response_queue_size = self.data_window + 8
        self.tcp_chunk = self.flow.get('frame_size', CHUNK)

    async def start(self):
        validate_update(self.args, self.helper_name)
        params = [('token', self.token), ('GUAC_DATA_SOURCE', self.data_source),
                  ('GUAC_ID', self.conn), ('GUAC_TYPE', self.kind),
                  ('GUAC_WIDTH', '1024'), ('GUAC_HEIGHT', '768'), ('GUAC_DPI', '96'),
                  ('GUAC_TIMEZONE', 'Europe/Berlin'), ('GUAC_IMAGE', 'image/png')]
        self.tunnel = Tunnel(self.session, self.base, params)
        await self.tunnel.connect()
        LOG.info('Connected to Guacamole (%s)', self.tunnel.mode)
        self.reader = asyncio.create_task(self.read_loop())
        await self.wait_for(self.synced.wait(), self.args.connect_timeout)
        await self.wait_for(asyncio.sleep(self.args.startup_delay), self.args.connect_timeout)
        ack = self.new_ack()
        await self.tunnel.send('pipe', self.input_stream, 'text/plain', 'STDIN')
        await self.wait_for(ack, self.args.connect_timeout)
        stage = ServerUpdate(self) if getattr(self.args, 'update_server', False) else HelperProbe(self)
        self.updater = stage
        try:
            await stage.run()
        finally:
            self.updater = None
        if isinstance(stage, HelperProbe):
            if await offer_update(self, stage):
                self.updater = ServerUpdate(self)
                try:
                    await self.updater.run()
                finally:
                    self.updater = None
            else:
                stage.check_result()
        await self.send_input(self.helper_command())
        try:
            await self.wait_for(self.ready.wait(), self.args.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise BridgeError('Remote helper did not become ready. Check ' + self.helper_name +
                              ' installation/PATH, shell readiness, and Guacamole STDIN/output pipes; '
                              'try --startup-delay 5. If the helper needs updating, use --update-server to attempt '
                              'uv tool installation using the server package index, or update manually.') from exc
        self.maintainer = asyncio.create_task(self.maintain())
        LOG.info('Server helper ready')

    async def wait_for(self, awaitable, timeout):
        operation = asyncio.ensure_future(awaitable)
        failed = asyncio.create_task(self.failed.wait())
        try:
            done, _ = await asyncio.wait((operation, failed), timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if failed in done:
                raise BridgeError(self.failure or 'Guacamole disconnected')
            if operation in done:
                return operation.result()
            raise asyncio.TimeoutError()
        finally:
            for task in (operation, failed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, failed, return_exceptions=True)

    def new_ack(self):
        future = asyncio.get_running_loop().create_future()
        self.acks.put_nowait(future)
        self.ack_deadlines[future] = asyncio.get_running_loop().call_later(
            self.args.connect_timeout, self.input_ack_timeout, future)
        future.add_done_callback(self.input_ack_done)
        return future

    def input_ack_timeout(self, future):
        if not future.done():
            future.set_exception(BridgeError('Guacamole input acknowledgement timed out; restart the bridge'))

    def input_ack_done(self, future):
        timer = self.ack_deadlines.pop(future, None)
        if timer:
            timer.cancel()
        # A small event can return before its ACK. Observe errors even if no
        # later send ever needs to wait on this future.
        if not future.cancelled():
            error = future.exception()
            if error is not None:
                self.fail(str(error) if isinstance(error, BridgeError) else 'Terminal input failed; restart the bridge')

    def finish_input_acks(self, message=None):
        while not self.acks.empty():
            future = self.acks.get_nowait()
            if not future.done():
                if message:
                    future.set_exception(BridgeError(message))
                else:
                    future.cancel()

    async def send_input(self, data):
        async with self.input_lock:
            pending = self.pending_input
            try:
                for offset in range(0, len(data), 6048):
                    if self.failed.is_set() or not self.tunnel or self.tunnel.closed:
                        raise BridgeError(self.failure or 'Guacamole tunnel is closed')
                    while pending and pending[0].done():
                        pending.popleft().result()
                    if len(pending) >= self.input_window:
                        await self.wait_for(pending.popleft(), self.args.connect_timeout)
                    pending.append(self.new_ack())
                    await self.tunnel.send('blob', self.input_stream,
                                           base64.b64encode(data[offset:offset + 6048]).decode())
            except BaseException:
                self.fail('Terminal input failed; restart the bridge')
                raise

    async def drain_input(self):
        async with self.input_lock:
            while self.pending_input:
                future = self.pending_input.popleft()
                if future.done():
                    future.result()
                else:
                    await self.wait_for(future, self.args.connect_timeout)

    async def send_event(self, event):
        await self.send_input(encode_event(event))
        if event.get('type') == 'shutdown':
            await self.drain_input()

    def fail(self, message):
        if self.failed.is_set() or self.closing:
            return
        self.failure = message
        self.failed.set()
        LOG.error('%s', message)
        for queue in self.requests.values():
            # A bounded queue can be full when the consumer is slow. Discard buffered
            # bytes on failure; never replace a truncated stream with a success marker.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait({'type': 'error', 'message': message})

    async def read_loop(self):
        parser = Parser()
        try:
            async for text in self.tunnel.messages():
                for args in parser.feed(text):
                    opcode, *values = args
                    if opcode == 'sync':
                        await self.tunnel.send('sync', values[0])
                        self.synced.set()
                    elif opcode == 'ack' and int(values[0]) == self.input_stream:
                        if not self.acks.empty():
                            future = self.acks.get_nowait()
                            if not future.done():
                                if int(values[2]) == 0:
                                    future.set_result(None)
                                else:
                                    future.set_exception(BridgeError('Guacamole rejected terminal input (status ' + values[2] + ')'))
                    elif opcode == 'pipe':
                        if values[2] == 'LLM' and self.output_stream is None:
                            self.output_stream = int(values[0])
                            await self.tunnel.send('ack', values[0], 'OK', 0)
                        else:
                            await self.tunnel.send('ack', values[0], 'Unsupported stream', 256)
                    elif opcode == 'blob' and int(values[0]) == self.output_stream:
                        data = base64.b64decode(values[1], validate=True)
                        if self.updater:
                            # Bootstrap remains independent of the installed data protocol.
                            self.output_buffer += data
                            if len(self.output_buffer) > 128 * 1024:
                                raise BridgeError('Bootstrap frame exceeds limit')
                            while b'\n' in self.output_buffer:
                                line, self.output_buffer = self.output_buffer.split(b'\n', 1)
                                if line.strip():
                                    self.dispatch(json.loads(base64.b64decode(line.strip(), validate=True)))
                        else:
                            try:
                                for event in self.decoder.feed(data):
                                    self.dispatch(event)
                            except (ValueError, UnicodeError):
                                raise BridgeError('Unsupported or invalid binary helper protocol. Reconnect with '
                                                  '--update-server or update the server helper manually.') from None
                        await self.tunnel.send('ack', values[0], 'OK', 0)
                    elif opcode == 'end' and int(values[0]) == self.output_stream:
                        if self.updater and self.updater.active:
                            self.output_stream = None
                            self.output_buffer = b''
                            self.decoder = Decoder()
                            self.updater.ended()
                            continue
                        if not self.closing:
                            raise BridgeError('Remote helper output pipe closed')
                        # The helper may close its output before the gateway's
                        # final STDIN ACK arrives. Keep reading those ACKs.
                        self.output_stream = None
                    elif opcode == 'error':
                        raise BridgeError('Guacamole error status ' + (values[1] if len(values) > 1 else 'unknown'))
                    elif opcode == 'disconnect':
                        raise BridgeError('Guacamole disconnected')
                    elif opcode in ('img', 'file', 'audio', 'video', 'body') and values:
                        # We intentionally do not render the graphical terminal.
                        await self.tunnel.send('ack', values[0], 'Not needed', 256)
            self.fail('Guacamole tunnel closed; restart the bridge')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(str(exc) if isinstance(exc, BridgeError) else 'Guacamole transport/protocol failure: ' + type(exc).__name__)
        finally:
            self.finish_input_acks('Guacamole tunnel closed before acknowledging terminal input')

    def dispatch(self, event):
        if self.updater and self.updater.dispatch(event):
            return
        self.last_seen = time.monotonic()
        if event['type'] == 'ready':
            if event.get('version') != PROTOCOL_VERSION:
                raise BridgeError('Unsupported remote helper protocol. Update the server with: '
                                  f'uv tool install --force guac-bridge=={__version__}. Then reconnect. '
                                  'Or use --update-server to attempt uv tool installation using the server package index.')
            if event.get('flow') != self.flow:
                raise BridgeError('Server helper did not confirm the requested transport sizes. '
                                  f'Update it with: uv tool install --force guac-bridge=={__version__}.')
            remote_version = event.get('package_version')
            self.server_version_matched = remote_version == __version__
            if remote_version != __version__:
                label = (remote_version if isinstance(remote_version, str) and remote_version
                         and len(remote_version) <= 64
                         and all(c.isalnum() or c in '.+-_!' for c in remote_version)
                         else 'not reported (older or incompatible helper)')
                message = (
                    f'Server helper {self.helper_name} version: {label}; client version: {__version__}. '
                    'On the server, run: '
                    f'uv tool install --force guac-bridge=={__version__}. '
                    'Then reconnect. If using --server-command, update the installation at that path.')
                if getattr(self.args, 'require_server_version', False):
                    raise BridgeError('Matching package versions required by --require-server-version. ' + message)
                self.version_warning = (
                    f'Server version mismatch: {self.helper_name} reports {label}; client is {__version__}. '
                    'Continuing with the supported protocol. To align versions on the server: '
                    f'uv tool install --force guac-bridge=={__version__}. '
                    'Alternatively use --update-server to attempt installation using the server package index.')
                LOG.warning('%s', self.version_warning)
            LOG.info('Flow control: %s data credits, %s input ACKs', self.data_window, self.input_window)
            if self.helper_name == 'guac-ssh-server':
                LOG.info('SSH frame size: %s bytes', self.tcp_chunk)
            self.ready.set()
        elif event['type'] != 'heartbeat':
            queue = self.requests.get(event.get('id'))
            if queue:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise BridgeError('Remote response exceeded flow control window') from exc

    async def maintain(self):
        refreshed = time.monotonic()
        try:
            while True:
                await asyncio.sleep(10)
                if time.monotonic() - self.last_seen > 45:
                    self.fail('Remote helper heartbeat lost; restart the bridge')
                    return
                if time.monotonic() - refreshed > 60 and self.kind == 'c':
                    path = 'api/session/data/' + urllib.parse.quote(self.data_source, safe='')
                    path += '/connections/' + urllib.parse.quote(self.conn, safe='')
                    async with self.session.get(self.base + path, params={'token': self.token},
                                                timeout=aiohttp.ClientTimeout(total=15),
                                                allow_redirects=False) as response:
                        if response.status != 200:
                            self.fail('Guacamole session refresh failed: HTTP ' + str(response.status))
                            return
                        await response.read()
                    refreshed = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.fail('Guacamole session refresh failed; restart the bridge')

    def new_ident(self):
        if self.next_id > 0xffffffff:
            raise BridgeError('Channel ID space exhausted; restart the bridge')
        ident = str(self.next_id)
        self.next_id += 1
        return ident

    async def proxy(self, request):
        if self.args.local_api_key and request.headers.get('Authorization') != 'Bearer ' + self.args.local_api_key:
            return web.json_response({'error': {'message': 'Invalid local API key'}}, status=401)
        if self.failed.is_set():
            return web.json_response({'error': {'message': self.failure}}, status=503)
        if request.query_string:
            return web.json_response({'error': {'message': 'Query parameters are not supported'}}, status=400)
        body = await request.read()
        if len(self.requests) >= self.args.max_requests:
            return web.json_response({'error': {'message': 'Bridge concurrency limit reached'}}, status=429)
        ident = self.new_ident()
        queue = asyncio.Queue(maxsize=self.response_queue_size)
        self.requests[ident] = queue
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ('authorization', 'content-type', 'accept', 'x-request-id')}
        # Keep framing/decompression simple and byte-for-byte at the local endpoint.
        headers['Accept-Encoding'] = 'identity'
        if self.args.upstream_api_key:
            headers['Authorization'] = 'Bearer ' + self.args.upstream_api_key
        response, complete = None, False
        try:
            async with asyncio.timeout(self.args.request_timeout):
                await self.send_event({'type': 'request', 'id': ident, 'method': request.method,
                                       'path': request.path, 'body': body,
                                       'headers': headers})
                while True:
                    event = await self.next_event(queue, request)
                    kind = event['type']
                    if kind == 'headers':
                        if response is not None:
                            raise BridgeError('Duplicate upstream headers')
                        response = web.StreamResponse(status=event['status'], headers=event['headers'])
                        await response.prepare(request)
                    elif kind == 'data':
                        if response is None:
                            raise BridgeError('Missing upstream headers')
                        await response.write(event['body'])
                        await self.send_event({'type': 'credit', 'id': ident})
                    elif kind == 'end':
                        if response is None:
                            raise BridgeError('Missing upstream response')
                        await response.write_eof()
                        complete = True
                        return response
                    elif kind == 'error':
                        raise BridgeError(event['message'])
        except (BridgeError, asyncio.TimeoutError, ConnectionError, aiohttp.ClientError) as exc:
            status = 504 if isinstance(exc, asyncio.TimeoutError) else 502
            message = str(exc) if isinstance(exc, BridgeError) else ('Request timed out' if status == 504 else 'Connection lost')
            if response is None:
                return web.json_response({'error': {'message': message, 'type': 'bridge_error'}}, status=status)
            LOG.warning('Response interrupted: %s', message)
            if request.transport:
                request.transport.abort()
            return response
        finally:
            try:
                if not complete and not self.failed.is_set():
                    try:
                        await asyncio.wait_for(self.cancel_request(ident, queue), timeout=5)
                    except asyncio.CancelledError:
                        self.fail('Remote request cancellation did not complete; restart the bridge')
                        raise
                    except Exception:
                        self.fail('Remote request cancellation did not complete; restart the bridge')
            finally:
                self.requests.pop(ident, None)

    async def cancel_request(self, ident, queue):
        await self.send_event({'type': 'cancel', 'id': ident})
        while True:
            event = await queue.get()
            if event['type'] in ('cancelled', 'end', 'error'):
                return

    async def next_event(self, queue, request):
        # Detect a disconnected HTTP consumer even while vLLM is still prefilling.
        while True:
            if request.transport is None or request.transport.is_closing():
                raise ConnectionError('Local client disconnected')
            try:
                return await asyncio.wait_for(queue.get(), timeout=1)
            except asyncio.TimeoutError:
                if self.failed.is_set():
                    raise BridgeError(self.failure)

    async def close(self):
        self.closing = True
        if self.tunnel and not self.tunnel.closed and self.ready.is_set() and not self.failed.is_set():
            with contextlib.suppress(Exception):
                # Closing the shared transport does not require the SSH
                # per-connection cancellation shield. Keep shutdown bounded.
                await asyncio.wait_for(Bridge.send_event(self, {'type': 'shutdown'}), timeout=3)
        if self.tunnel and not self.tunnel.closed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.tunnel.send('end', self.input_stream), timeout=2)
        for task in (self.maintainer, self.reader):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.maintainer, self.reader) if t), return_exceptions=True)
        if self.tunnel:
            await self.tunnel.close()
        self.finish_input_acks()
        for future, timer in list(self.ack_deadlines.items()):
            timer.cancel()
            if not future.done():
                future.cancel()
        self.ack_deadlines.clear()
        self.pending_input.clear()


async def serve(args):
    validate_update(args, 'guac-llm-server')
    username = resolve_username(args)
    if getattr(args, 'forget_password', False):
        forget_credentials(args.url, username)
        return
    base, conn, kind, source = parse_url(args.url, args.connection_id, args.data_source)
    command = bootstrap(args)  # Validate helper availability/size before login.
    del command
    tls = ssl.create_default_context(cafile=args.ca_file)
    if args.insecure:
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        LOG.warning('TLS verification disabled by --insecure')
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=args.connect_timeout)
    connector = aiohttp.TCPConnector(ssl=tls)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                                     trust_env=True, auto_decompress=False,
                                     headers={'Accept-Encoding': 'identity'}) as session:
        token = await authenticate(session, base, username, args)
        bridge = Bridge(args, session, base, conn, kind, source, token)
        runner = None
        try:
            await bridge.start()
            app = web.Application(client_max_size=args.max_body_mib * 1024 * 1024)
            for path, method in PATHS.items():
                app.router.add_route(method, path, bridge.proxy)

            async def health(request):
                return web.json_response({'ready': bridge.ready.is_set() and not bridge.failed.is_set()},
                                         status=503 if bridge.failed.is_set() else 200)
            app.router.add_get('/health', health)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', args.port).start()
            LOG.info('API available at http://127.0.0.1:%s/v1', args.port)
            stop_hint()
            await bridge.failed.wait()
            raise BridgeError(bridge.failure)
        finally:
            if runner:
                await runner.cleanup()
            await bridge.close()
            with contextlib.suppress(Exception):
                async with session.delete(base + 'api/tokens/' + urllib.parse.quote(token, safe=''),
                                          timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as response:
                    await response.read()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('--url', required=True, help='Guacamole URL, including #/client/...')
    parser.add_argument('--username', help='Guacamole login username; otherwise prompted')
    add_credential_arguments(parser)
    add_flow_arguments(parser)
    add_update_argument(parser)
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument('--require-server-version', action='store_true',
                          help='Require matching client/server package versions instead of warning')
    versions.add_argument('--ignore-server-version', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=8000, help='Local loopback port (default: 8000)')
    parser.add_argument('--upstream', default='http://127.0.0.1:8000', help='Server-side loopback vLLM URL')
    parser.add_argument('--connection-id', help='Override URL connection identifier')
    parser.add_argument('--data-source', help='Override URL data source, e.g. mysql')
    parser.add_argument('--server-command', default='~/.local/bin/guac-llm-server',
                        help='Installed helper executable on server; absolute path recommended')
    parser.add_argument('--startup-delay', type=float, default=2, help='Seconds for remote shell startup')
    parser.add_argument('--connect-timeout', type=float, default=30)
    parser.add_argument('--request-timeout', type=float, default=1800, help='Maximum request duration, seconds')
    parser.add_argument('--max-requests', type=int, default=4)
    parser.add_argument('--max-body-mib', type=int, default=16)
    parser.add_argument('--ca-file', help='Corporate CA PEM file for HTTPS verification')
    parser.add_argument('--insecure', action='store_true', help='Disable HTTPS certificate verification')
    args = parser.parse_args()
    args.upstream_api_key = os.environ.get('VLLM_API_KEY')
    args.local_api_key = os.environ.get('BRIDGE_API_KEY')
    if not 1 <= args.port <= 65535 or not 1 <= args.max_requests <= 16:
        parser.error('Port must be 1..65535; --max-requests must be 1..16')
    if min(args.connect_timeout, args.request_timeout, args.max_body_mib) <= 0 or args.startup_delay < 0:
        parser.error('Timeouts/body limit must be positive; startup delay cannot be negative')
    if args.max_body_mib > 16:
        parser.error('--max-body-mib cannot exceed 16 (remote framing limit)')
    upstream = urllib.parse.urlsplit(args.upstream)
    if (upstream.scheme != 'http' or upstream.hostname not in ('127.0.0.1', 'localhost', '::1')
            or upstream.username or upstream.password or upstream.query or upstream.fragment):
        parser.error('--upstream must be HTTP on server loopback, with no credentials/query/fragment')
    setup_console('LLM')
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except (BridgeError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # aiohttp exception strings can include authenticated tunnel URLs. Do not log them.
        LOG.error('%s', str(exc) if isinstance(exc, BridgeError) else type(exc).__name__ +
                  ': connection failed; check network, proxy, certificate, and local port')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())



