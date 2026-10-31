"""Reproducible local controls and PTY/Guacamole-simulation benchmarks.

Run from the repository root: uv run python -m benchmarks.transport --help
Requires Linux /proc, Bash, and PTYs; uses no corporate hosts or credentials.
ACK delay is injected only into STDIN blob acknowledgements, independently of
other messages. It is NOT a full network RTT/packet-loss/bandwidth simulation.
"""
import argparse
import asyncio
import base64
import cProfile
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import shlex
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.parse

import aiohttp
from aiohttp import web

from guac_bridge.client import Bridge, Parser, instruction, login
from guac_bridge.ssh_client import SSHBridge
from .pipeline import LegacyBridge, LegacySSHBridge, PipelinedBridge, PipelinedSSHBridge
from .latency import LatencyProxy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_bridge import Gateway, Terminal, start_app  # noqa: E402


class OutputQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.delay = 0
        self.wire_bytes = 0
        self.instructions = 0
        self.timers = []

    async def put(self, item):
        if self.delay and item.startswith('3.ack,1.0,'):
            self.timers.append(asyncio.get_running_loop().call_later(self.delay, self.put_nowait, item))
        else:
            await super().put(item)

    def put_nowait(self, item):
        self.wire_bytes += len(item.encode())
        self.instructions += 1
        super().put_nowait(item)


class TimedTerminal(Terminal):
    def __init__(self):
        super().__init__()
        self.output = OutputQueue()

    async def close(self):
        for timer in self.output.timers:
            timer.cancel()
        self.loop.remove_reader(self.fd)
        os.close(self.fd)
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()


class TimedGateway(Gateway):
    async def ws_handler(self, request):
        if not self.websocket:
            return web.Response(status=404)
        ws = self.ws = web.WebSocketResponse(protocols=('guacamole',))
        await ws.prepare(request)
        term = TimedTerminal()
        self.terminals.append(term)
        await term.start()

        async def output():
            while True:
                await ws.send_str(await term.output.get())
        task = asyncio.create_task(output())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await term.feed(msg.data)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ws

    async def http_handler(self, request):
        # Reuse the production test fixture's HTTP behavior with timed terminals.
        # A connect is the only place it constructs its own Terminal.
        if request.query_string == 'connect':
            import uuid
            term = TimedTerminal()
            self.terminals.append(term)
            await term.start()
            return web.Response(text=str(uuid.uuid4()))
        return await super().http_handler(request)


def child_cpu(pid):
    """Linux process-tree CPU, including the helper's threads; clock-tick resolution."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        total = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
        children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        return total + sum(child_cpu(int(child)) for child in children)
    except (OSError, ValueError, IndexError):
        return None


def start_meter(bridge, terminal):
    metrics = dict(input_calls=0, input_wait_s=0., ack_wait_s=0., client_to_gateway_bytes=0,
                   client_to_gateway_instructions=0, client_to_gateway_writes=0)
    bridge.tunnel.benchmark_metrics = metrics
    original_send = bridge.tunnel.send
    original_input = bridge.send_input
    original_wait = bridge.wait_for

    async def send(*args):
        metrics['client_to_gateway_bytes'] += len(instruction(*args).encode())
        metrics['client_to_gateway_instructions'] += 1
        metrics['client_to_gateway_writes'] += 1
        return await original_send(*args)

    async def send_input(data):
        metrics['input_calls'] += 1
        start = time.perf_counter()
        try:
            return await original_input(data)
        finally:
            metrics['input_wait_s'] += time.perf_counter() - start

    async def wait_for(awaitable, timeout):
        is_ack = isinstance(awaitable, asyncio.Future)
        start = time.perf_counter()
        try:
            return await original_wait(awaitable, timeout)
        finally:
            if is_ack:
                metrics['ack_wait_s'] += time.perf_counter() - start

    bridge.tunnel.send, bridge.send_input, bridge.wait_for = send, send_input, wait_for
    terminal.output.wire_bytes = terminal.output.instructions = 0
    return metrics


async def trial(payload, variant, transport, ack_ms, workload, *, rtt_ms=0, helper_setup=None):
    runners, handlers, servers = [], set(), []
    gateway, bridge, session, terminal = TimedGateway(transport == 'websocket'), None, None, None
    proxy = None
    with tempfile.TemporaryDirectory() as directory:
        helper = Path(directory) / 'helper'
        is_ssh = workload.startswith('ssh-')
        module = 'guac_bridge.ssh_server' if is_ssh else 'guac_bridge.server'
        if helper_setup:
            if is_ssh:
                code = (f'import guac_bridge.ssh_server as m;m.CHUNK={helper_setup["chunk"]};'
                        f'm.WINDOW={helper_setup["app_window"]};raise SystemExit(m.cli())')
            else:
                code = ('import guac_bridge.server as m;original=m.run;'
                        f'm.run=lambda c:original(dict(c,experimental_window={helper_setup["app_window"]}));'
                        'raise SystemExit(m.cli())')
            invocation = ' -c ' + shlex.quote(code)
        else:
            invocation = ' -m ' + module
        helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + invocation + ' "$@"\n')
        helper.chmod(0o755)
        try:
            if is_ssh:
                async def target(reader, writer):
                    task = asyncio.current_task()
                    handlers.add(task)
                    try:
                        if workload == 'ssh-upload':
                            received = await reader.read()
                            writer.write(hashlib.sha256(received).digest())
                        else:
                            writer.write(payload)
                        await writer.drain()
                        writer.write_eof()
                    finally:
                        writer.close()
                        with contextlib.suppress(OSError):
                            await writer.wait_closed()
                        handlers.discard(task)
                target_server = await asyncio.start_server(target, '127.0.0.1', 0)
                servers.append(target_server)
                target_port = target_server.sockets[0].getsockname()[1]
            else:
                async def upstream(request):
                    body = await request.json()
                    if workload == 'llm-upload':
                        return web.json_response({'length': len(body['prompt'])})
                    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
                    await response.prepare(request)
                    if workload == 'llm-paced':
                        for index in range(64):
                            token = dict(index=index, sent=time.perf_counter(), text='x' * 80)
                            await response.write(b'data: ' + json.dumps(token).encode() + b'\n\n')
                            await asyncio.sleep(.01)
                    else:
                        for offset in range(0, len(payload), 4096):
                            await response.write(payload[offset:offset + 4096])
                    await response.write_eof()
                    return response
                app = web.Application(client_max_size=16 * 1024 * 1024)
                app.router.add_post('/v1/chat/completions', upstream)
                runner, upstream_url = await start_app(app)
                runners.append(runner)
            runner, base = await start_app(gateway.app())
            runners.append(runner)
            if rtt_ms:
                parts = urllib.parse.urlsplit(base)
                proxy = await LatencyProxy(parts.hostname, parts.port, rtt_ms).start()
                base = f'http://127.0.0.1:{proxy.port}/'
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))
            base += 'guacamole/'
            token = await login(session, base, 'test', 'test', 10)
            args = SimpleNamespace(server_command=str(helper), connect_timeout=30, startup_delay=.03,
                                   transport=transport, max_connections=8, target_host='127.0.0.1',
                                   target_port=target_port if is_ssh else 22, max_requests=4,
                                   upstream='' if is_ssh else upstream_url.rstrip('/'),
                                   request_timeout=180, local_api_key=None, upstream_api_key=None)
            cls = ({'baseline': LegacySSHBridge, 'pipeline': PipelinedSSHBridge} if is_ssh else
                   {'baseline': LegacyBridge, 'pipeline': PipelinedBridge})[variant]
            bridge = cls(args, session, base, '1', 'c', 'mysql', token)
            await bridge.start()
            terminal = gateway.terminals[0]
            metrics = start_meter(bridge, terminal)
            terminal.output.delay = ack_ms / 1000
            if is_ssh:
                listener = await asyncio.start_server(bridge.accept, '127.0.0.1', 0)
                servers.append(listener)
                local_port = listener.sockets[0].getsockname()[1]
            else:
                app = web.Application(client_max_size=16 * 1024 * 1024)
                app.router.add_post('/v1/chat/completions', bridge.proxy)
                runner, local_url = await start_app(app)
                runners.append(runner)
            parent_start, child_start = time.process_time(), child_cpu(terminal.proc.pid)
            started = time.perf_counter()
            measured_bytes = len(payload)
            if is_ssh:
                reader, writer = await asyncio.open_connection('127.0.0.1', local_port)
                if workload == 'ssh-upload':
                    writer.write(payload)
                    await writer.drain()
                writer.write_eof()
                result = await reader.read()
                expected = hashlib.sha256(payload).digest() if workload == 'ssh-upload' else payload
                assert result == expected, 'SSH payload corruption'
                writer.close()
                await writer.wait_closed()
            else:
                body = {'prompt': base64.b64encode(payload).decode()} if workload == 'llm-upload' else {}
                if workload == 'llm-upload':
                    measured_bytes = len(json.dumps(body).encode())
                async with session.post(local_url + 'v1/chat/completions', json=body) as response:
                    assert response.status == 200
                    if workload == 'llm-paced':
                        lags = []
                        measured_bytes = 0
                        async for line in response.content:
                            measured_bytes += len(line)
                            if line.startswith(b'data: '):
                                lags.append((time.perf_counter() - json.loads(line[6:])['sent']) * 1000)
                        assert len(lags) == 64
                        metrics.update(delivery_lag_p50_ms=statistics.median(lags),
                                       delivery_lag_p95_ms=sorted(lags)[int(.95 * (len(lags) - 1))])
                    elif workload == 'llm-upload':
                        assert (await response.json())['length'] == len(body['prompt'])
                    else:
                        assert await response.read() == payload, 'LLM payload corruption'
            elapsed = time.perf_counter() - started
            child_end = child_cpu(terminal.proc.pid)
            metrics.update(workload=workload, variant=variant, transport=transport, ack_delay_ms=ack_ms,
                           application_rtt_ms=rtt_ms,
                           payload_bytes=measured_bytes, wall_s=elapsed, mib_s=measured_bytes / elapsed / 2**20,
                           parent_cpu_s=time.process_time() - parent_start,
                           helper_tree_cpu_s=(None if child_start is None or child_end is None
                                              else max(0., child_end - child_start)),
                           gateway_to_client_bytes=terminal.output.wire_bytes,
                           gateway_to_client_instructions=terminal.output.instructions)
            return dict(metrics)
        finally:
            if sys.exception() is not None:
                import traceback
                traceback.print_exception(sys.exception())
            for server in servers:
                server.close()
            if bridge:
                await bridge.close()
            await gateway.close()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*list(handlers), return_exceptions=True)
            for server in servers:
                await server.wait_closed()
            for runner in reversed(runners):
                await runner.cleanup()
            if session:
                await session.close()
            if proxy:
                await proxy.close()


async def websocket_control(payload):
    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for offset in range(0, len(payload), 4096):
            await ws.send_bytes(payload[offset:offset + 4096])
        await ws.close()
        return ws
    app = web.Application()
    app.router.add_get('/', endpoint)
    runner, url = await start_app(app)
    try:
        async with aiohttp.ClientSession() as session:
            start, cpu = time.perf_counter(), time.process_time()
            async with session.ws_connect(url) as ws:
                result = b''.join([m.data async for m in ws if m.type == aiohttp.WSMsgType.BINARY])
            assert result == payload
            elapsed = time.perf_counter() - start
            return dict(workload='websocket-control', payload_bytes=len(payload), wall_s=elapsed,
                        mib_s=len(payload) / elapsed / 2**20, parent_cpu_s=time.process_time() - cpu)
    finally:
        await runner.cleanup()


async def tcp_control(payload):
    async def endpoint(reader, writer):
        try:
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(endpoint, '127.0.0.1', 0)
    try:
        start, cpu = time.perf_counter(), time.process_time()
        reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
        received = await reader.read()
        assert received == payload
        writer.close()
        await writer.wait_closed()
        elapsed = time.perf_counter() - start
        return dict(workload='tcp-control', payload_bytes=len(payload), wall_s=elapsed,
                    mib_s=len(payload) / elapsed / 2**20, parent_cpu_s=time.process_time() - cpu)
    finally:
        server.close()
        await server.wait_closed()


def codec_controls(payload):
    raw, ident = payload[:4096], 'a' * 32
    event = dict(type='data', id=ident, body=base64.b64encode(raw).decode())
    encoded = json.dumps(event, separators=(',', ':')).encode()
    terminal = base64.b64encode(encoded) + b'\n'
    guac = ''.join(instruction('blob', '0', base64.b64encode(terminal[i:i + 3072]).decode())
                   for i in range(0, len(terminal), 3072))
    def roundtrip():
        inner = base64.b64encode(json.dumps(dict(type='data', id=ident,
                    body=base64.b64encode(raw).decode()), separators=(',', ':')).encode()) + b'\n'
        wire = ''.join(instruction('blob', '0', base64.b64encode(inner[i:i + 3072]).decode())
                       for i in range(0, len(inner), 3072))
        received = b''.join(base64.b64decode(args[2], validate=True) for args in Parser().feed(wire))
        decoded = json.loads(base64.b64decode(received.strip(), validate=True))
        assert base64.b64decode(decoded['body'], validate=True) == raw
    operations = dict(base64=lambda: base64.b64decode(base64.b64encode(raw), validate=True),
                      json=lambda: json.loads(json.dumps(event, separators=(',', ':'))),
                      guac_parser=lambda: Parser().feed(guac), full_codec=roundtrip)
    result = []
    for name, operation in operations.items():
        start = time.perf_counter()
        for _ in range(4096):
            operation()
        elapsed = time.perf_counter() - start
        result.append(dict(workload=name, iterations=4096, wall_s=elapsed,
                           mib_s=len(raw) * 4096 / elapsed / 2**20))
    return dict(raw_bytes=len(raw), body_json_bytes=len(encoded), terminal_bytes=len(terminal),
                guac_bytes=len(guac.encode()), wire_expansion=len(guac.encode()) / len(raw), controls=result)


async def main(args):
    payload = os.urandom(args.kib * 1024)
    result = dict(environment=dict(python=sys.version, aiohttp=aiohttp.__version__, platform=platform.platform(),
                                  legacy_send_policy_commit='0d547f8131aedf5b938871376fba749f08c32e53',
                                  client_version=__import__('guac_bridge').__version__),
                  parameters=vars(args), rows=[])
    result['codec'] = codec_controls(payload)
    control_payload = os.urandom(8 * 1024 * 1024)
    result['websocket_controls'] = [await websocket_control(control_payload) for _ in range(3)]
    result['tcp_controls'] = [await tcp_control(control_payload) for _ in range(3)]
    for repeat in range(args.repeats):
        for transport in args.transports.split(','):
            for delay in map(float, args.delays.split(',')):
                for workload in args.workloads.split(','):
                    variants = args.variants.split(',')
                    for variant in (variants if repeat % 2 == 0 else list(reversed(variants))):
                        print(f'Running {repeat=} {transport=} {delay=} {workload=} {variant=}', file=sys.stderr, flush=True)
                        row = await asyncio.wait_for(trial(payload, variant, transport, delay, workload), 180)
                        row['repeat'] = repeat
                        result['rows'].append(row)
                        print(json.dumps(row), flush=True)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kib', type=int, default=256)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--delays', default='0,20,50', help='STDIN ACK delay in ms, not full network RTT')
    parser.add_argument('--transports', default='websocket,http')
    parser.add_argument('--variants', default='baseline,pipeline')
    parser.add_argument('--workloads', default='ssh-upload,ssh-download,llm-upload,llm-download,llm-paced')
    parser.add_argument('--output', default='benchmarks/results/local.json')
    parser.add_argument('--profile', help='Optional cProfile file; profiling changes timings')
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    if args.kib < 4 or args.repeats < 1 or any(float(d) < 0 for d in args.delays.split(',')):
        parser.error('Use at least 4 KiB, one repeat, and nonnegative ACK delays')
    if args.profile:
        profile = cProfile.Profile()
        profile.enable()
    asyncio.run(main(args))
    if args.profile:
        profile.disable()
        profile.dump_stats(args.profile)
