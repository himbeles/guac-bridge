"""Authenticated local shutdown for managed SSH bridge processes."""
import asyncio
import contextlib
import hmac
import json
import os
import secrets
import socket

from .errors import BridgeError


def read_state(path):
    try:
        state = json.loads(path.read_text())
        if (not isinstance(state['control_port'], int) or not 1 <= state['control_port'] <= 65535
                or not isinstance(state['token'], str) or len(state['token']) != 64
                or not isinstance(state['instance'], str)):
            raise ValueError()
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


def request(state, command):
    try:
        with socket.create_connection(('127.0.0.1', state['control_port']), timeout=1) as sock:
            sock.sendall(json.dumps({'token': state['token'], 'command': command}).encode() + b'\n')
            data = bytearray()
            while b'\n' not in data and len(data) <= 1024:
                part = sock.recv(1024)
                if not part:
                    break
                data.extend(part)
            response = json.loads(data)
            if response.get('ok') is not True or response.get('instance') != state['instance']:
                raise ValueError()
            return response
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BridgeError('Cannot contact the managed bridge; it may have already stopped') from None


class ControlServer:
    def __init__(self, path, instance, *, version_verified=False, version_warning=None):
        self.path, self.instance = path, instance
        self.token = secrets.token_hex(32)
        self.stopped = asyncio.Event()
        self.server = None
        self.handlers = set()
        self.version_verified = version_verified
        self.version_warning = version_warning

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, limit=1024)
        state = dict(control_port=self.server.sockets[0].getsockname()[1], token=self.token,
                     instance=self.instance, pid=os.getpid(), version_verified=self.version_verified,
                     version_warning=self.version_warning)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(self.path.name + '.' + self.instance)
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
                json.dump(state, output)
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            await self.close()
            raise
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        stop = False
        try:
            data = json.loads(await asyncio.wait_for(reader.readline(), 2))
            token = data.get('token', '')
            if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
                return
            command = data.get('command')
            if command not in ('status', 'stop'):
                return
            writer.write(json.dumps(dict(ok=True, instance=self.instance, pid=os.getpid(),
                                         version_verified=self.version_verified,
                                         version_warning=self.version_warning)).encode() + b'\n')
            await writer.drain()
            stop = command == 'stop'
        except (ValueError, TypeError, AttributeError, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.handlers.discard(task)
            if stop:
                self.stopped.set()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state = read_state(self.path)
        if state and state['instance'] == self.instance:
            with contextlib.suppress(OSError):
                self.path.unlink()
