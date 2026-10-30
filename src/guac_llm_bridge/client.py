#!/usr/bin/env python3
"""Local OpenAI HTTP endpoint over an authenticated Apache Guacamole terminal."""
import argparse
import asyncio
import base64
import contextlib
import getpass
import json
import logging
import os
import shlex
import ssl
import sys
import time
import urllib.parse
import uuid

import aiohttp
from aiohttp import web

LOG = logging.getLogger('guac-llm')
PATHS = {'/v1/models': 'GET', '/v1/chat/completions': 'POST', '/v1/completions': 'POST'}


class BridgeError(Exception):
    pass


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
    command = executable + ' ' + ' '.join(shlex.quote(str(value)) for value in (
        '--upstream', args.upstream,
        '--request-timeout', args.request_timeout, '--max-requests', args.max_requests)) + '\n'
    if len(command.encode()) > 3900:
        raise BridgeError('Remote bootstrap exceeds terminal canonical line limit')
    return command.encode()


class Tunnel:
    def __init__(self, session, base, params, mode):
        self.session, self.base, self.params, self.mode = session, base, params, mode
        self.ws = None
        self.ident = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def connect(self):
        if self.mode != 'http':
            url = self.base.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
            try:
                self.ws = await self.session.ws_connect(
                    url + 'websocket-tunnel', params=self.params, protocols=('guacamole',),
                    heartbeat=20, max_msg_size=16 * 1024 * 1024,
                    origin=urllib.parse.urlunsplit((*urllib.parse.urlsplit(self.base)[:2], '', '', '')))
                self.mode = 'websocket'
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if self.mode == 'websocket':
                    raise BridgeError('WebSocket tunnel failed; try --transport http') from exc
                LOG.info('WebSocket unavailable; trying Guacamole HTTP tunnel')
        self.mode = 'http'
        async with self.session.post(self.base + 'tunnel?connect', data=self.params,
                                     allow_redirects=False) as response:
            if response.status != 200:
                raise BridgeError('HTTP tunnel connect returned status ' + str(response.status))
            self.ident = (await response.text()).strip()
        try:
            uuid.UUID(self.ident)
        except ValueError as exc:
            raise BridgeError('HTTP tunnel did not return a tunnel UUID') from exc

    async def send(self, *args):
        payload = instruction(*args)
        async with self.lock:
            if self.closed:
                raise BridgeError('Guacamole tunnel is closed')
            if self.ws:
                await self.ws.send_str(payload)
            else:
                async with self.session.post(self.base + 'tunnel?write:' + self.ident,
                                             data=payload.encode(),
                                             headers={'Content-Type': 'application/octet-stream'},
                                             allow_redirects=False) as response:
                    if response.status != 200:
                        raise BridgeError('HTTP tunnel write failed: ' + str(response.status))
                    await response.read()

    async def messages(self):
        if self.ws:
            async for message in self.ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    yield message.data
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise BridgeError('WebSocket tunnel failed')
            return
        counter = 0
        import codecs
        decoder = codecs.getincrementaldecoder('utf-8')()
        while not self.closed:
            async with self.session.get(self.base + 'tunnel?read:' + self.ident + ':' + str(counter),
                                        allow_redirects=False) as response:
                if response.status != 200:
                    raise BridgeError('HTTP tunnel read failed: ' + str(response.status))
                async for chunk in response.content.iter_any():
                    yield decoder.decode(chunk)
            counter += 1

    async def close(self):
        self.closed = True
        if self.ws:
            await self.ws.close()


class Bridge:
    helper_name = 'guac-llm-server'

    def helper_command(self):
        return bootstrap(self.args)

    def __init__(self, args, session, base, conn, kind, data_source, token):
        self.args, self.session = args, session
        self.base, self.conn, self.kind, self.data_source = base, conn, kind, data_source
        self.token = token
        self.tunnel = None
        self.reader = None
        self.maintainer = None
        self.synced = asyncio.Event()
        self.ready = asyncio.Event()
        self.failed = asyncio.Event()
        self.failure = None
        self.input_lock = asyncio.Lock()
        self.acks = asyncio.Queue()
        self.input_stream = 0
        self.output_stream = None
        self.output_buffer = b''
        self.requests = {}
        self.last_seen = time.monotonic()
        self.closing = False

    async def start(self):
        params = [('token', self.token), ('GUAC_DATA_SOURCE', self.data_source),
                  ('GUAC_ID', self.conn), ('GUAC_TYPE', self.kind),
                  ('GUAC_WIDTH', '1024'), ('GUAC_HEIGHT', '768'), ('GUAC_DPI', '96'),
                  ('GUAC_TIMEZONE', 'Europe/Berlin'), ('GUAC_IMAGE', 'image/png')]
        self.tunnel = Tunnel(self.session, self.base, params, self.args.transport)
        await self.tunnel.connect()
        LOG.info('Connected via %s; waiting for terminal', self.tunnel.mode)
        self.reader = asyncio.create_task(self.read_loop())
        await self.wait_for(self.synced.wait(), self.args.connect_timeout)
        await self.wait_for(asyncio.sleep(self.args.startup_delay), self.args.connect_timeout)
        ack = self.new_ack()
        await self.tunnel.send('pipe', self.input_stream, 'text/plain', 'STDIN')
        await self.wait_for(ack, self.args.connect_timeout)
        await self.send_input(self.helper_command())
        try:
            await self.wait_for(self.ready.wait(), self.args.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise BridgeError('Remote helper did not become ready. Check ' + self.helper_name +
                              ' installation/PATH, shell readiness, and Guacamole STDIN/output pipes; '
                              'try --startup-delay 5.') from exc
        self.maintainer = asyncio.create_task(self.maintain())
        LOG.info('Remote helper ready')

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
        return future

    async def send_input(self, data):
        async with self.input_lock:
            pending = []
            try:
                for offset in range(0, len(data), 3072):
                    pending.append(self.new_ack())
                    await self.tunnel.send('blob', self.input_stream,
                                           base64.b64encode(data[offset:offset + 3072]).decode())
                    if len(pending) >= 8:
                        await self.wait_for(pending.pop(0), self.args.connect_timeout)
                for future in pending:
                    await self.wait_for(future, self.args.connect_timeout)
            except BaseException:
                self.fail('Terminal input failed; restart the bridge')
                raise

    async def send_event(self, event):
        await self.send_input(base64.b64encode(json.dumps(event, separators=(',', ':')).encode()) + b'\n')

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
                                    future.set_exception(BridgeError('Guacamole rejected STDIN pipe: ' + values[2]))
                    elif opcode == 'pipe':
                        if values[2] == 'LLM' and self.output_stream is None:
                            self.output_stream = int(values[0])
                            await self.tunnel.send('ack', values[0], 'OK', 0)
                        else:
                            await self.tunnel.send('ack', values[0], 'Unsupported stream', 256)
                    elif opcode == 'blob' and int(values[0]) == self.output_stream:
                        self.output_buffer += base64.b64decode(values[1], validate=True)
                        if len(self.output_buffer) > 128 * 1024:
                            raise BridgeError('Remote frame exceeds limit')
                        while b'\n' in self.output_buffer:
                            line, self.output_buffer = self.output_buffer.split(b'\n', 1)
                            if line.strip():
                                event = json.loads(base64.b64decode(line.strip(), validate=True))
                                self.dispatch(event)
                        await self.tunnel.send('ack', values[0], 'OK', 0)
                    elif opcode == 'end' and int(values[0]) == self.output_stream:
                        raise BridgeError('Remote helper output pipe closed')
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

    def dispatch(self, event):
        self.last_seen = time.monotonic()
        if event['type'] == 'ready':
            if event.get('version') != 1:
                raise BridgeError('Unsupported remote helper version')
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
        ident = uuid.uuid4().hex
        queue = asyncio.Queue(maxsize=16)
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
                                       'path': request.path, 'body': base64.b64encode(body).decode(),
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
                        await response.write(base64.b64decode(event['body'], validate=True))
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
            self.requests.pop(ident, None)
            if not complete and not self.failed.is_set():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.send_event({'type': 'cancel', 'id': ident}), timeout=5)

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
        if self.tunnel and self.ready.is_set() and not self.failed.is_set():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.send_event({'type': 'shutdown'}), timeout=3)
        if self.tunnel and not self.tunnel.closed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.tunnel.send('end', self.input_stream), timeout=2)
        for task in (self.maintainer, self.reader):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.maintainer, self.reader) if t), return_exceptions=True)
        if self.tunnel:
            await self.tunnel.close()


async def login(session, base, username, password, timeout):
    async with session.post(base + 'api/tokens', data={'username': username, 'password': password},
                            timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as response:
        if response.status != 200:
            raise BridgeError('Guacamole login returned HTTP ' + str(response.status) +
                              '. This tool supports the standard username/password login API; '
                              'SSO/MFA or an external login portal requires adaptation.')
        try:
            payload = await response.json(content_type=None)
            return payload['authToken']
        except (ValueError, KeyError) as exc:
            raise BridgeError('Login did not return a Guacamole authToken; check the URL base path') from exc


async def serve(args):
    base, conn, kind, source = parse_url(args.url, args.connection_id, args.data_source)
    command = bootstrap(args)  # Validate helper availability/size before login.
    del command
    username = args.username or input('Guacamole username: ')
    password = os.environ.get('GUAC_PASSWORD') or getpass.getpass('Guacamole password: ')
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
        token = await login(session, base, username, password, args.connect_timeout)
        password = None
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
            LOG.info('Local API: http://127.0.0.1:%s/v1 (Ctrl+C to stop)', args.port)
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
    parser.add_argument('--url', required=True, help='Guacamole URL, including #/client/...')
    parser.add_argument('--username', help='Guacamole login username; otherwise prompted')
    parser.add_argument('--port', type=int, default=8000, help='Local loopback port (default: 8000)')
    parser.add_argument('--upstream', default='http://127.0.0.1:8000', help='Server-side loopback vLLM URL')
    parser.add_argument('--connection-id', help='Override URL connection identifier')
    parser.add_argument('--data-source', help='Override URL data source, e.g. mysql')
    parser.add_argument('--transport', choices=('auto', 'websocket', 'http'), default='auto')
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
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
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

