"""Binary TCP relay to a loopback SSH service, over a Guacamole terminal."""
import argparse
import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
import threading

from . import __version__
CHUNK = 4096
WINDOW = 8


async def relay(args):
    events = asyncio.Queue(maxsize=64)
    channels = {}
    loop = asyncio.get_running_loop()
    stopping = threading.Event()

    def emit(event):
        data = base64.b64encode(json.dumps(event, separators=(',', ':')).encode())
        sys.stdout.buffer.write(data + b'\n')
        sys.stdout.buffer.flush()

    def stdin_reader():
        try:
            while not stopping.is_set():
                line = sys.stdin.buffer.readline(65536)
                if not line:
                    event = {'type': 'shutdown'}
                else:
                    if not line.endswith(b'\n'):
                        raise ValueError('Frame too large')
                    event = json.loads(base64.b64decode(line.strip(), validate=True))
                future = asyncio.run_coroutine_threadsafe(events.put(event), loop)
                future.result()
                if event.get('type') == 'shutdown':
                    return
        except Exception:
            if not stopping.is_set() and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(events.put({'type': 'shutdown'}), loop)

    async def connection(ident, state):
        writer = None
        tasks = []
        terminal_event = {'type': 'closed', 'id': ident}
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(args.target_host, args.target_port), args.connect_timeout)
            emit({'type': 'opened', 'id': ident})

            async def receive_tcp():
                while True:
                    await state['credits'].acquire()
                    data = await reader.read(CHUNK)
                    if not data:
                        state['credits'].release()
                        emit({'type': 'eof', 'id': ident})
                        return
                    emit({'type': 'data', 'id': ident, 'body': base64.b64encode(data).decode()})

            async def send_tcp():
                while True:
                    data = await state['input'].get()
                    if data is None:
                        if writer.can_write_eof():
                            writer.write_eof()
                            await writer.drain()
                        return
                    writer.write(data)
                    await writer.drain()
                    emit({'type': 'credit', 'id': ident})

            tasks = [asyncio.create_task(receive_tcp()), asyncio.create_task(send_tcp())]
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            terminal_event = {'type': 'error', 'id': ident,
                              'message': 'SSH target connection failed: ' + type(exc).__name__}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 2)
            channels.pop(ident, None)
            emit(terminal_event)

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            emit({'type': 'heartbeat'})

    # LLM is the established transport pipe name, shared by both relay modes.
    sys.stdout.buffer.write(b'\x1b]482202;2;LLM\x07')
    sys.stdout.buffer.flush()
    emit({'type': 'ready', 'version': 1, 'protocol': 'ssh-tcp', 'package_version': __version__,
          'capabilities': ['cleanup-ack']})
    main = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, main.cancel)
    thread = threading.Thread(target=stdin_reader, daemon=True)
    thread.start()
    pulse = asyncio.create_task(heartbeat())
    try:
        while True:
            event = await events.get()
            kind, ident = event.get('type'), event.get('id')
            if kind == 'shutdown':
                break
            if kind == 'open':
                if not isinstance(ident, str) or not ident or len(ident) > 64:
                    raise ValueError('Invalid connection ID')
                if ident in channels:
                    raise ValueError('Duplicate connection ID')
                if len(channels) >= args.max_connections:
                    emit({'type': 'error', 'id': ident, 'message': 'SSH connection limit reached'})
                    continue
                state = {'input': asyncio.Queue(maxsize=WINDOW + 1),
                         'credits': asyncio.BoundedSemaphore(WINDOW), 'eof': False}
                channels[ident] = state
                state['task'] = asyncio.create_task(connection(ident, state))
                continue
            state = channels.get(ident)
            if state is None:
                if kind == 'close':
                    emit({'type': 'closed', 'id': ident})
                continue
            if kind == 'data':
                data = base64.b64decode(event['body'], validate=True)
                if len(data) > CHUNK or state['eof']:
                    raise ValueError('Invalid TCP data frame')
                state['input'].put_nowait(data)
            elif kind == 'credit':
                state['credits'].release()
            elif kind == 'eof':
                if state['eof']:
                    raise ValueError('Duplicate TCP EOF')
                state['eof'] = True
                state['input'].put_nowait(None)
            elif kind == 'close':
                state['task'].cancel()
    finally:
        stopping.set()
        pulse.cancel()
        tasks = [state['task'] for state in list(channels.values())]
        for task in tasks:
            task.cancel()
        await asyncio.gather(pulse, *tasks, return_exceptions=True)
        sys.stdout.buffer.write(b'\x1b]482203;\x07')
        sys.stdout.buffer.flush()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            loop.remove_signal_handler(sig)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('--stdio', action='store_true', help='Run the machine protocol; supplied by guac-ssh-client')
    parser.add_argument('--target-host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    parser.add_argument('--target-port', type=int, default=22)
    parser.add_argument('--connect-timeout', type=float, default=30)
    parser.add_argument('--max-connections', type=int, default=8)
    args = parser.parse_args()
    if not args.stdio:
        parser.error('This helper is started automatically by guac-ssh-client. Do not run it manually.')
    if not 1 <= args.target_port <= 65535 or not 1 <= args.max_connections <= 16 or args.connect_timeout <= 0:
        parser.error('Invalid target port, connection limit (1..16), or timeout')
    old = None
    try:
        if os.isatty(0):
            import termios
            import tty
            old = termios.tcgetattr(0)
            tty.setraw(0)
        asyncio.run(relay(args))
    except (asyncio.CancelledError, KeyboardInterrupt, BrokenPipeError, OSError, ValueError):
        return 1
    finally:
        if old is not None:
            with contextlib.suppress(OSError):
                termios.tcsetattr(0, termios.TCSANOW, old)
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
