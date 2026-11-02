#!/usr/bin/env python3
"""Reproduce ignored libssh2 writes using isolated loopback SSH and synthetic frames.
Requires AsyncSSH and libssh2.so. No root required.
No production credentials or remote systems are used. All test servers are loopback.
"""
import argparse
import asyncio
import asyncssh
import ctypes as C
import hashlib
import json
from pathlib import Path
import random
import select
import socket
import tempfile
import threading
import time


def bind(lib, name, result, *args):
    f = getattr(lib, 'libssh2_' + name)
    f.restype, f.argtypes = result, args
    return f


class SSH:
    def __init__(self, library, port, capture):
        self.lib = C.CDLL(library)
        p, i, z, s = C.c_void_p, C.c_int, C.c_size_t, C.c_char_p
        self.init = bind(self.lib, 'init', i, i)
        self.session_init = bind(self.lib, 'session_init_ex', p, p, p, p, p)
        self.handshake = bind(self.lib, 'session_handshake', i, p, i)
        self.auth = bind(self.lib, 'userauth_password_ex', i, p, s, C.c_uint, s, C.c_uint, p)
        self.open = bind(self.lib, 'channel_open_ex', p, p, s, C.c_uint, C.c_uint, C.c_uint, s, C.c_uint)
        self.pty = bind(self.lib, 'channel_request_pty_ex', i, p, s, C.c_uint, s, C.c_uint, i, i, i, i)
        self.start = bind(self.lib, 'channel_process_startup', i, p, s, C.c_uint, s, C.c_uint)
        self.blocking = bind(self.lib, 'session_set_blocking', None, p, i)
        self.write = bind(self.lib, 'channel_write_ex', C.c_ssize_t, p, i, p, z)
        self.read = bind(self.lib, 'channel_read_ex', C.c_ssize_t, p, i, p, z)
        self.eof = bind(self.lib, 'channel_send_eof', i, p)
        self.version = bind(self.lib, 'version', s, i)(0).decode()
        assert self.init(0) == 0
        self.sock = socket.create_connection(('127.0.0.1', port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
        self.session = self.session_init(None, None, None, None)
        self.blocking(self.session, 1)
        rc=self.handshake(self.session, self.sock.fileno())
        if rc:
            error=C.c_char_p(); length=C.c_int()
            bind(self.lib,'session_last_error',i,p,C.POINTER(s),C.POINTER(i),i)(self.session,C.byref(error),C.byref(length),0)
            raise RuntimeError(f'handshake failed {rc}: {error.value}')
        assert self.auth(self.session, b'test', 4, b'test', 4, None) == 0, 'SSH authentication failed'
        self.channel = self.open(self.session, b'session', 7, 2*1024*1024, 32768, None, 0)
        assert self.channel
        assert self.pty(self.channel, b'xterm', 5, None, 0, 80, 24, 0, 0) == 0
        command = capture.name
        assert self.start(self.channel, b'exec', 4, command.encode(), len(command)) == 0
        buf = C.create_string_buffer(8192)
        ready = b''
        while b'READY' not in ready:
            n = self.read(self.channel, 0, buf, 8192)
            assert n > 0
            ready += buf.raw[:n]
        self.blocking(self.session, 0)
        self.sock.setblocking(False)

    def send(self, wire, retry, burst):
        # One reused 8 KiB buffer, matching ssh_input_thread().
        buffer = C.create_string_buffer(8192)
        calls, short, eagain, other, positive = 0, 0, 0, 0, 0
        pending_size = None
        for start in range(0, len(wire), burst):
            group = wire[start:start+burst]
            for offset in range(0, len(group), 8192):
                data = group[offset:offset+8192]
                C.memmove(buffer, data, len(data))
                done = 0
                while done < len(data):
                    n = self.write(self.channel, 0, C.byref(buffer, done), len(data)-done)
                    calls += 1
                    if n > 0:
                        positive += n
                        if n < len(data)-done:
                            short += 1
                        done += n
                        pending_size = None
                    elif n == -37:  # LIBSSH2_ERROR_EAGAIN
                        eagain += 1
                        pending_size = len(data)-done
                    else:
                        other += 1
                        if retry:
                            raise RuntimeError(f'write error {n}')
                    if not retry:
                        break  # Upstream loop discards the result and reads new data.
                    if n <= 0:
                        select.select([self.sock], [self.sock], [], .01)
                        time.sleep(.001)
            # Emulate a paced producer for the small-burst case. There is no
            # artificial byte dropping: the only fault is ignoring write results.
            time.sleep(.25)
        # Give pending transport sends a chance to complete before EOF.
        deadline = time.monotonic()+5
        if pending_size:
            while time.monotonic() < deadline:
                n = self.write(self.channel, 0, buffer, pending_size)
                if n != -37:
                    break
                time.sleep(.005)
        while True:
            n = self.eof(self.channel)
            if n == 0:
                break
            assert n == -37, f"send_eof failed: {n}"
            assert time.monotonic() < deadline
            time.sleep(.005)
        return dict(calls=calls, short_writes=short, eagain=eagain,
                    other_errors=other, positive_write_returns=positive)


class Proxy:
    """Pause socket consumption once, causing actual kernel/libssh2 backpressure."""
    def __init__(self, target):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.pause = threading.Event()
        self.stop = threading.Event()
        self.target = target
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        client, _ = self.listener.accept()
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
        with client, socket.create_connection(('127.0.0.1', self.target)) as server:
            paused = False
            try:
                while not self.stop.is_set():
                    if self.pause.is_set() and not paused:
                        time.sleep(.2)
                        paused = True
                    ready, _, _ = select.select([client, server], [], [], .01)
                    for src in ready:
                        data = src.recv(8192)
                        if not data:
                            return
                        (server if src is client else client).sendall(data)
            except (ConnectionResetError, BrokenPipeError):
                pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(2)


class TestServer(asyncssh.SSHServer):
    def password_auth_supported(self):
        return True
    def validate_password(self, username, password):
        return username == 'test' and password == 'test'

class CaptureServer:
    def __init__(self, root):
        self.root = root
        self.finished = {}
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        assert self.ready.wait(10)
    async def capture(self, process):
        # Command is an identifier only; never executed as shell text.
        if process.command not in {'ignored_large_burst.bin', 'ignored_small_burst.bin', 'retried_large_burst.bin'}:
            process.exit(1)
            return
        finished = self.finished.setdefault(process.command, threading.Event())
        path = self.root / process.command
        with path.open('wb') as output:
            process.stdout.write(b'READY')
            while data := await process.stdin.read(65536):
                output.write(data)
                output.flush()
        process.exit(0)
        finished.set()
    def run(self):
        asyncio.set_event_loop(self.loop)
        async def start():
            self.server = await asyncssh.create_server(
                TestServer, '127.0.0.1', 0,
                server_host_keys=[asyncssh.generate_private_key('ecdsa-sha2-nistp256')],
                process_factory=self.capture, encoding=None, line_editor=False)
            self.port = self.server.get_port()
            self.ready.set()
        self.loop.run_until_complete(start())
        self.loop.run_forever()
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())
        self.loop.close()
    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def wire_frames():
    rng = random.Random(1729)
    out = bytearray()
    for _ in range(12):
        data = rng.randbytes(65536)
        raw = b'GB\x05\x04'+(1).to_bytes(4,'big')+len(data).to_bytes(4,'big')+data
        for b in raw:
            out.extend(bytes((0x1d, b ^ 0x20)) if b in (0x1b, 0x1d) else bytes((b,)))
    return bytes(out)


def main(args):
    results = []
    with tempfile.TemporaryDirectory(prefix='guac-write-repro-') as temp:
        root = Path(temp)
        server = CaptureServer(root)
        port = server.port
        try:
            wire = wire_frames()
            for name, retry, burst in [('ignored_large_burst',False,64*6048),
                                       ('ignored_small_burst',False,8*6048),
                                       ('retried_large_burst',True,64*6048)]:
                capture = root/(name+'.bin')
                proxy = Proxy(port)
                ssh = SSH(args.libssh2, proxy.port, capture)
                proxy.pause.set()
                stats = ssh.send(wire, retry, burst)
                assert server.finished[capture.name].wait(5), "SSH receiver did not reach EOF"
                ssh.sock.close()
                proxy.close()
                time.sleep(.1)
                received = capture.read_bytes()
                first = next((i for i,(a,b) in enumerate(zip(wire,received)) if a!=b),
                             min(len(wire),len(received)) if wire!=received else None)
                entry = dict(case=name, libssh2=ssh.version, burst_bytes=burst,
                             wire_bytes=len(wire), received_bytes=len(received), exact_match=wire==received,
                             first_mismatch_offset=first, expected_sha256=hashlib.sha256(wire).hexdigest(),
                             received_sha256=hashlib.sha256(received).hexdigest(),
                             **stats)
                results.append(entry)
                print(json.dumps(entry), flush=True)
            assert results[0]['eagain'] > 0
            assert results[1]['exact_match']
            assert not results[0]['exact_match'], 'Large burst did not reproduce loss; increase backpressure'
            assert results[2]['exact_match'], 'Retry control failed'
        finally:
            server.close()
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2)+'\n')

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--libssh2',required=True)
    p.add_argument('--output')
    main(p.parse_args())
