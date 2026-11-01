"""Experimental Python SSH bridge with the Rust prototype's GR1 binary transport."""
import argparse
import asyncio
import base64
import contextlib
import logging
import math
import ssl
import time

import aiohttp

from .auth import add_credential_arguments, authenticate, forget_credentials, resolve_username
from .binary_framing import Decoder, MAX_FRAME, encode_event
from .client import BridgeError, Parser, Tunnel, parse_url
from .flow import settings
from .ssh_client import SSHBridge, start_listener

LOG = logging.getLogger('guac-ssh-binary')


class BinarySSHBridge(SSHBridge):
    helper_name = 'guac-ssh-server-binary'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flow = settings(self.args, ssh=True)
        self.data_window = self.flow['window']
        self.tcp_chunk = self.flow['frame_size']
        self.input_window = getattr(self.args, 'input_window', 64)
        self.input_chunk = getattr(self.args, 'input_chunk', 6048)
        self.next_channel = 1

    async def start(self):
        params = [('token', self.token), ('GUAC_DATA_SOURCE', self.data_source),
                  ('GUAC_ID', self.conn), ('GUAC_TYPE', self.kind),
                  ('GUAC_WIDTH', '1024'), ('GUAC_HEIGHT', '768'), ('GUAC_DPI', '96'),
                  ('GUAC_TIMEZONE', 'Europe/Berlin'), ('GUAC_IMAGE', 'image/png')]
        self.tunnel = Tunnel(self.session, self.base, params, self.args.transport)
        await self.tunnel.connect()
        self.reader = asyncio.create_task(self.read_loop())
        await self.wait_for(self.synced.wait(), self.args.connect_timeout)
        await asyncio.sleep(self.args.startup_delay)
        ack = self.new_ack()
        await self.tunnel.send('pipe', self.input_stream, 'text/plain', 'STDIN')
        await self.wait_for(ack, self.args.connect_timeout)
        # Keep bootstrap input below canonical TTY line limits; the helper enters
        # raw mode before emitting ready. No production package update is attempted.
        await self.send_input(self.helper_command(), chunk=3072)
        await self.wait_for(self.ready.wait(), self.args.connect_timeout)
        self.maintainer = asyncio.create_task(self.maintain())

    async def send_input(self, data, *, chunk=None):
        chunk = self.input_chunk if chunk is None else chunk
        async with self.input_lock:
            pending = self.pending_input
            try:
                for offset in range(0, len(data), chunk):
                    if self.failed.is_set() or not self.tunnel or self.tunnel.closed:
                        raise BridgeError(self.failure or 'Guacamole tunnel is closed')
                    while pending and pending[0].done():
                        pending.popleft().result()
                    if len(pending) >= self.input_window:
                        await self.wait_for(pending.popleft(), self.args.connect_timeout)
                    pending.append(self.new_ack())
                    await self.tunnel.send('blob', self.input_stream,
                                           base64.b64encode(data[offset:offset+chunk]).decode())
            except BaseException:
                self.fail('Terminal input failed; restart the bridge')
                raise

    async def send_event(self, event):
        async def send():
            await self.send_input(encode_event(event))
            if event['type'] == 'shutdown':
                await self.drain_input()
        task = asyncio.create_task(send())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await task
            raise

    def dispatch(self, event):
        if event['type'] == 'ready':
            if event['flow'] != self.flow:
                raise BridgeError('Binary helper did not confirm the requested transport sizes')
            self.last_seen = time.monotonic()
            self.ready.set()
        else:
            super().dispatch(event)

    async def read_loop(self):
        parser, decoder = Parser(), Decoder()
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
                                    future.set_exception(BridgeError('Guacamole rejected terminal input'))
                    elif opcode == 'pipe':
                        if values[2] == 'LLM' and self.output_stream is None:
                            self.output_stream = int(values[0])
                            await self.tunnel.send('ack', values[0], 'OK', 0)
                        else:
                            await self.tunnel.send('ack', values[0], 'Unsupported stream', 256)
                    elif opcode == 'blob' and int(values[0]) == self.output_stream:
                        data = base64.b64decode(values[1], validate=True)
                        for event in decoder.feed(data):
                            self.dispatch(event)
                        await self.tunnel.send('ack', values[0], 'OK', 0)
                    elif opcode == 'end' and int(values[0]) == self.output_stream:
                        if not self.closing:
                            raise BridgeError('Remote binary helper output pipe closed')
                        self.output_stream = None
                    elif opcode in ('error', 'disconnect'):
                        raise BridgeError('Guacamole disconnected or reported an error')
                    elif opcode in ('img', 'file', 'audio', 'video', 'body') and values:
                        await self.tunnel.send('ack', values[0], 'Not needed', 256)
            self.fail('Guacamole tunnel closed; restart the bridge')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(str(exc) if isinstance(exc, BridgeError) else
                      'Invalid binary transport: ' + type(exc).__name__)
        finally:
            self.finish_input_acks('Guacamole tunnel closed before acknowledging terminal input')

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        if self.failed.is_set() or self.closing or len(self.requests) >= self.args.max_connections:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        self.connections.add(task)
        ident = str(self.next_channel)
        self.next_channel += 1
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
                                           'body': data})

            async def download():
                nonlocal remote_closed
                while True:
                    event = await queue.get()
                    kind = event['type']
                    if kind == 'data':
                        data = event['body']
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


    async def close(self):
        self.closing = True
        connections = list(self.connections)
        for connection in connections:
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        if self.tunnel and not self.tunnel.closed and self.ready.is_set() and not self.failed.is_set():
            with contextlib.suppress(Exception):
                # Closing the shared transport does not require the SSH
                # per-connection cancellation shield. Keep shutdown bounded.
                await asyncio.wait_for(self._shutdown(), timeout=3)
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

    async def _shutdown(self):
        await self.send_input(encode_event(dict(type='shutdown')))
        await self.drain_input()


async def serve(args):
    username = resolve_username(args)
    if args.forget_password:
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
        bridge = BinarySSHBridge(args, session, base, conn, kind, source, token)
        listener = None
        try:
            await bridge.start()
            listener = await start_listener(bridge.accept, args.port)
            LOG.info('Binary SSH bridge listening on 127.0.0.1:%s; Ctrl+C to stop', args.port)
            await bridge.failed.wait()
            raise BridgeError(bridge.failure)
        finally:
            if listener:
                listener.close()
            await bridge.close()
            if listener:
                await listener.wait_closed()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--username')
    parser.add_argument('--connection-id')
    parser.add_argument('--data-source')
    parser.add_argument('--port', type=int, default=2222)
    parser.add_argument('--server-command', default='~/.local/bin/guac-ssh-server-binary')
    parser.add_argument('--target-host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    parser.add_argument('--target-port', type=int, default=22)
    parser.add_argument('--max-connections', type=int, default=8)
    parser.add_argument('--connect-timeout', type=float, default=30)
    parser.add_argument('--startup-delay', type=float, default=1)
    parser.add_argument('--transport', choices=('auto','websocket','http'), default='auto')
    parser.add_argument('--ca-file')
    parser.add_argument('--insecure', action='store_true')
    parser.add_argument('--window', type=int, default=32,
                        help='Data frames in flight per channel; larger windows keep delayed links busy.')
    parser.add_argument('--frame-size', type=int, default=65536,
                        help='Maximum TCP bytes per frame; larger values reduce overhead and buffer more.')
    parser.add_argument('--input-window', type=int, default=64,
                        help='Guacamole input blobs allowed in flight before waiting for ACKs.')
    parser.add_argument('--input-chunk', type=int, default=6048,
                        help='Bytes per input blob; maximum 6048 for Guacamole instruction limits.')
    add_credential_arguments(parser)
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or not 1 <= args.target_port <= 65535
            or not 1 <= args.max_connections <= 16 or not 1 <= args.window <= 128
            or not 1 <= args.input_window <= 128 or not 512 <= args.input_chunk <= 6048
            or not 4096 <= args.frame_size <= MAX_FRAME
            or not math.isfinite(args.connect_timeout) or args.connect_timeout <= 0
            or not math.isfinite(args.startup_delay) or args.startup_delay < 0):
        parser.error('Invalid listener, timeout or transport settings')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0
    except (BridgeError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOG.error('%s', str(exc) if isinstance(exc, BridgeError) else type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
