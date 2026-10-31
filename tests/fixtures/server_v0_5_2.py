"""Server-side stdin/stdout relay, launched inside a Guacamole SSH terminal."""
import base64
import http.client
import json
import os
import socket
import sys
import threading
import urllib.parse

__version__ = '0.5.2'

def run(config):
    upstream = urllib.parse.urlsplit(config['upstream'])
    if upstream.scheme != 'http' or upstream.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Upstream must be HTTP on server loopback')
    stop = threading.Event()
    lock = threading.Lock()
    states = {}
    state_lock = threading.Lock()
    old_tty = None
    if os.isatty(0):
        import termios
        import tty
        old_tty = termios.tcgetattr(0)
        tty.setraw(0)

    def output(data):
        with lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def emit(event):
        with lock:
            if not stop.is_set():
                sys.stdout.buffer.write(base64.b64encode(json.dumps(event, separators=(',', ':')).encode()) + b'\n')
                sys.stdout.buffer.flush()

    def worker(req, state):
        ident = req['id']
        conn = None
        terminal_event = None
        try:
            path = req['path']
            if path not in ('/v1/chat/completions', '/v1/completions', '/v1/models'):
                raise ValueError('Unsupported endpoint')
            conn = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=5)
            state['conn'] = conn
            conn.connect()
            state['sock'] = conn.sock
            conn.sock.settimeout(config['timeout'])
            if state['cancel'].is_set():
                return
            conn.request(req['method'], upstream.path.rstrip('/') + path,
                         body=base64.b64decode(req['body'], validate=True), headers=req['headers'])
            response = conn.getresponse()
            headers = {k: v for k, v in response.getheaders()
                       if k.lower() in ('content-type', 'content-encoding', 'retry-after', 'x-request-id')}
            emit({'type': 'headers', 'id': ident, 'status': response.status, 'headers': headers})
            while not state['cancel'].is_set():
                # read1 returns available bytes; read(4096) would delay SSE tokens.
                data = response.read1(4096)
                if not data:
                    break
                while not state['cancel'].is_set():
                    if state['credits'].acquire(timeout=1):
                        break
                if state['cancel'].is_set():
                    break
                emit({'type': 'data', 'id': ident, 'body': base64.b64encode(data).decode()})
            if not state['cancel'].is_set():
                terminal_event = {'type': 'end', 'id': ident}
        except Exception as exc:
            if not state['cancel'].is_set():
                terminal_event = {'type': 'error', 'id': ident,
                                  'message': 'Upstream HTTP failed: ' + type(exc).__name__}
        finally:
            if conn:
                try:
                    conn.close()
                except OSError:
                    pass
            with state_lock:
                states.pop(ident, None)
            # A terminal event certifies that the remote concurrency slot and
            # upstream socket have been released, not just that EOF was read.
            if state['cancel'].is_set():
                emit({'type': 'cancelled', 'id': ident})
            elif terminal_event:
                emit(terminal_event)

    def cancel(state):
        state['cancel'].set()
        state['credits'].release()
        sock = state.get('sock')
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def heartbeat():
        while not stop.wait(10):
            emit({'type': 'heartbeat'})

    # Flag 2 enables per-frame autoflush, essential for readiness and SSE latency.
    output(b'\x1b]482202;2;LLM\x07')
    try:
        emit({'type': 'ready', 'version': 1, 'package_version': __version__, 'capabilities': ['cleanup-ack']})
        threading.Thread(target=heartbeat, daemon=True).start()
        while True:
            line = sys.stdin.buffer.readline(48 * 1024 * 1024)
            if not line:
                break
            req = json.loads(base64.b64decode(line.strip(), validate=True))
            kind = req.get('type')
            if kind == 'shutdown':
                break
            ident = req.get('id')
            if kind in ('credit', 'cancel'):
                with state_lock:
                    state = states.get(ident)
                if state:
                    if kind == 'credit':
                        state['credits'].release()
                    else:
                        cancel(state)
                elif kind == 'cancel':
                    emit({'type': 'cancelled', 'id': ident})
            elif kind == 'request':
                with state_lock:
                    if ident in states or len(states) >= config['max_requests']:
                        emit({'type': 'error', 'id': ident, 'message': 'Remote concurrency limit'})
                        continue
                    state = {'credits': threading.Semaphore(8), 'cancel': threading.Event()}
                    states[ident] = state
                threading.Thread(target=worker, args=(req, state), daemon=True).start()
    finally:
        stop.set()
        with state_lock:
            active = list(states.values())
        for state in active:
            cancel(state)
        # Serialize closing with writes from the daemon threads.
        with lock:
            sys.stdout.buffer.write(b'\x1b]482203;\x07')
            sys.stdout.buffer.flush()
        if old_tty is not None:
            termios.tcsetattr(0, termios.TCSANOW, old_tty)


def cli():
    import argparse
    parser = argparse.ArgumentParser(description='Relay vLLM HTTP through a Guacamole SSH terminal.')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('--upstream', default='http://127.0.0.1:8000')
    parser.add_argument('--request-timeout', type=float, default=1800)
    parser.add_argument('--max-requests', type=int, default=4)
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.upstream)
    if (url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1', '::1')
            or url.username or url.password or url.query or url.fragment):
        parser.error('--upstream must be HTTP on loopback, without credentials/query/fragment')
    if not 1 <= args.max_requests <= 16 or args.request_timeout <= 0:
        parser.error('--max-requests must be 1..16 and --request-timeout must be positive')
    try:
        run({'upstream': args.upstream, 'timeout': args.request_timeout,
             'max_requests': args.max_requests})
    except (KeyboardInterrupt, BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
