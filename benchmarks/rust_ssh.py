"""Independent Rust SSH bridge versus Python, actual helpers in real PTYs.

Build rust-ssh in release mode first. Run with `uv run python -m benchmarks.rust_ssh`.
No external host or credential is used. Client and helper CPU are separate.
"""
import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shlex
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


async def python_worker(config):
    import aiohttp
    from guac_bridge.client import login
    from guac_bridge.ssh_client import SSHBridge
    args = SimpleNamespace(server_command=config['helper'], connect_timeout=30,
        startup_delay=.03, transport='websocket', max_connections=8,
        target_host='127.0.0.1', target_port=config['target_port'])
    async with aiohttp.ClientSession() as session:
        token = await login(session, config['base'], 'test', 'test', 10)
        bridge = SSHBridge(args, session, config['base'], '1', 'c', 'mysql', token)
        await bridge.start()
        listener = await asyncio.start_server(bridge.accept, '127.0.0.1', 0)
        print(json.dumps({'endpoint': listener.sockets[0].getsockname()[1]}), flush=True)
        try:
            while True:
                command = (await asyncio.to_thread(sys.stdin.readline)).strip()
                if command == 'start':
                    cpu, wall = time.process_time(), time.perf_counter()
                    print('{}', flush=True)
                elif command == 'stop':
                    print(json.dumps(dict(client_cpu_s=time.process_time()-cpu,
                        wall_s=time.perf_counter()-wall,
                        client_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)), flush=True)
                else:
                    break
        finally:
            listener.close()
            await listener.wait_closed()
            await bridge.close()


def fixture_types():
    from aiohttp import web, WSMsgType
    from .transport import TimedGateway, TimedTerminal
    from guac_bridge.client import instruction, Parser

    class MeteredGateway(TimedGateway):
        def __init__(self):
            super().__init__(True)
            self.input_bytes = self.input_instructions = 0
            self.server_bytes = self.server_instructions = 0

        async def ws_handler(self, request):
            assert request.query['GUAC_ID'] == '1'
            assert request.query['GUAC_DATA_SOURCE'] == 'mysql'
            ws = self.ws = web.WebSocketResponse(protocols=('guacamole',))
            await ws.prepare(request)
            terminal = TimedTerminal()
            self.terminals.append(terminal)
            await terminal.start()

            async def output():
                while True:
                    item = await terminal.output.get()
                    values = Parser().feed(item)[0]
                    if values[0] == 'blob':
                        data = base64.b64decode(values[2])
                        messages = [instruction('blob', values[1], base64.b64encode(data[i:i+6048]).decode())
                                    for i in range(0, len(data), 6048)]
                    else:
                        messages = [item]
                    for item in messages:
                        assert len(item.encode()) <= 8192, 'Guacamole instruction size limit'
                        self.server_bytes += len(item.encode())
                        self.server_instructions += 1
                        await ws.send_str(item)
            task = asyncio.create_task(output())
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        assert len(msg.data.encode()) <= 8192, 'Guacamole instruction size limit'
                        self.input_bytes += len(msg.data.encode())
                        self.input_instructions += 1
                        await terminal.feed(msg.data)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return ws
    return MeteredGateway


class BridgeWorker:
    def __init__(self, variant, rtt_ms=0, timeout=30):
        self.variant, self.rtt_ms, self.timeout = variant, rtt_ms, timeout
        self.gateway = fixture_types()()
        self.directory = tempfile.TemporaryDirectory()
        self.report_path = Path(self.directory.name)/'helper-usage.json'
        self.client_report_path = Path(self.directory.name)/'client-usage.json'
        self.child = self.runner = self.proxy = None
        self.helper_usage = None
        self.client_usage = None

    async def start(self, target_port):
        from .transport import start_app
        from .latency import LatencyProxy
        native = self.variant != 'python'
        command = ([os.environ.get('GUAC_RUST_HELPER', str(ROOT/'rust-ssh/target/release/guac-ssh-server-rs'))] if native else
                   [sys.executable, '-m', 'guac_bridge.ssh_server'])
        helper = Path(self.directory.name)/'helper'
        wrapper = [sys.executable, '-m', 'benchmarks.rust_ssh_helper', str(self.report_path), 'helper', '--', *command]
        helper.write_text('#!/bin/sh\nexec '+' '.join(map(shlex.quote, wrapper))+' "$@"\n')
        helper.chmod(0o755)
        self.runner, base = await start_app(self.gateway.app())
        if self.rtt_ms:
            from urllib.parse import urlsplit
            parts = urlsplit(base)
            self.proxy = await LatencyProxy(parts.hostname, parts.port, self.rtt_ms).start()
            base = f'http://127.0.0.1:{self.proxy.port}/'
        base += 'guacamole/'
        config = dict(helper=str(helper), base=base, target_port=target_port)
        environment = dict(os.environ, PYTHONPATH=str(ROOT/'src'), GUAC_BRIDGE_PASSWORD='test')
        if native:
            url = base+'#/client/'+base64.b64encode(b'1\0c\0mysql').decode()
            command = [str(ROOT/'rust-ssh/target/release/guac-ssh-client-rs'), '--url', url,
                       '--username', 'test', '--port', '0', '--server-command', str(helper),
                       '--target-port', str(target_port), '--startup-delay', '.03',
                       '--connect-timeout', str(self.timeout), '--benchmark-control']
            if self.variant == 'rust-matched':
                command += ['--frame-size', '16384', '--window', '32', '--input-window', '32', '--input-chunk', '3072']
        else:
            command = [sys.executable, '-m', 'benchmarks.rust_ssh', '--worker', json.dumps(config)]
        command = [sys.executable, '-m', 'benchmarks.rust_ssh_helper', str(self.client_report_path), 'client', '--', *command]
        self.child = await asyncio.create_subprocess_exec(*command, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(self.child.stdout.readline(), 45)
        if not line:
            raise RuntimeError(f'{self.variant} did not start; exit={self.child.returncode}')
        self.port = json.loads(line)['endpoint']
        return self

    async def command(self, command):
        self.child.stdin.write(command.encode()+b'\n')
        await self.child.stdin.drain()
        line = await asyncio.wait_for(self.child.stdout.readline(), 30)
        if not line:
            raise RuntimeError('Client worker exited during measurement')
        return json.loads(line)

    def wire(self):
        terminal = self.gateway.terminals[0]
        return dict(client_wire_bytes=self.gateway.input_bytes,
                    client_instructions=self.gateway.input_instructions,
                    server_wire_bytes=self.gateway.server_bytes,
                    server_instructions=self.gateway.server_instructions)

    async def close(self):
        try:
            if self.child and self.child.returncode is None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.child.stdin.write(b'quit\n')
                    await self.child.stdin.drain()
                try:
                    await asyncio.wait_for(self.child.wait(), 6)
                except asyncio.TimeoutError:
                    self.child.kill()
                    await self.child.wait()
            # STDIN ACK confirms delivery to the PTY, not helper process exit.
            # Let the supervisor reap the helper before closing its terminal.
            for _ in range(60):
                if self.report_path.exists():
                    value = json.loads(self.report_path.read_text())
                    if value['stdio']:
                        self.helper_usage = value
                        break
                await asyncio.sleep(.05)
            await self.gateway.close()
            if self.runner:
                await self.runner.cleanup()
            if self.proxy:
                await self.proxy.close()
            if self.report_path.exists():
                value = json.loads(self.report_path.read_text())
                if value['stdio']:
                    self.helper_usage = value
            if self.client_report_path.exists():
                self.client_usage = json.loads(self.client_report_path.read_text())
        finally:
            self.directory.cleanup()


async def write_paced(writer, payload, seconds):
    started = time.perf_counter()
    for offset in range(0, len(payload), 16384):
        if seconds:
            await asyncio.sleep(max(0, started+seconds*offset/len(payload)-time.perf_counter()))
        writer.write(payload[offset:offset+16384])
        await writer.drain()


async def trial(variant, direction, *, mib=16, seconds=4, rtt_ms=0):
    payload = hashlib.shake_256(b'guac-rust-efficiency').digest(mib*2**20)
    handlers = set()
    duration = seconds

    async def target(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            if direction == 'upload':
                received = await reader.read()
                writer.write(hashlib.sha256(received).digest())
                await writer.drain()
            else:
                await write_paced(writer, payload, duration)
            writer.write_eof()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(target, '127.0.0.1', 0)
    bridge = BridgeWorker(variant, rtt_ms)
    result = None
    try:
        await bridge.start(server.sockets[0].getsockname()[1])

        async def transfer():
            if direction == 'idle':
                await asyncio.sleep(duration)
                return
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.port)
            if direction == 'upload':
                await write_paced(writer, payload, duration)
            writer.write_eof()
            received = await reader.read()
            expected = hashlib.sha256(payload).digest() if direction == 'upload' else payload
            assert received == expected, 'Payload corruption or truncation'
            writer.close()
            await writer.wait_closed()

        # Warm up one full transfer at saturation before resetting counters.
        duration = 0
        if direction != 'idle':
            await transfer()
        await asyncio.sleep(.1)
        duration = seconds
        before = bridge.wire()
        await bridge.command('start')
        await transfer()
        result = await bridge.command('stop')
        result.update({k: v-before[k] for k, v in bridge.wire().items()})
        result.update(variant=variant, direction=direction, bytes=0 if direction=='idle' else len(payload),
                      rtt_ms=rtt_ms, paced_seconds=seconds, integrity=True)
    finally:
        await bridge.close()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*list(handlers), return_exceptions=True)
    assert bridge.child.returncode == 0, 'Client failed on shutdown'
    assert bridge.helper_usage is not None, 'Helper usage counter missing'
    assert bridge.helper_usage['helper_exit'] == 0, 'Helper failed on shutdown'
    assert bridge.client_usage is not None and bridge.client_usage['client_exit'] == 0
    result.update(bridge.client_usage)
    result.update(bridge.helper_usage)
    result.pop('stdio')
    result['bridge_lifetime_cpu_s'] = result['client_lifetime_cpu_s']+result['helper_lifetime_cpu_s']
    result['client_mib_s'] = result['bytes']/2**20/result['wall_s']
    return result


async def run(args):
    result = dict(python=platform.python_version(), platform=platform.platform(),
        build='cargo build --release --locked; current-thread Tokio; TCP_NODELAY enabled',
        native_binary_sha256={name: hashlib.sha256((ROOT/'rust-ssh/target/release'/name).read_bytes()).hexdigest()
                              for name in ('guac-ssh-client-rs', 'guac-ssh-server-rs')},
        profiles={'python': dict(frame=16384, window=32, input_chunk=3072, input_window=32, protocol='ASCII JSON'),
                  'rust-matched': dict(frame=16384, window=32, input_chunk=3072, input_window=32, protocol='ESC-safe binary'),
                  'rust': dict(frame=65536, window=32, input_chunk=6048, input_window=64, protocol='ESC-safe binary')},
        method='Separate client process CPU; helper wait4 CPU includes startup, warmup, transfer and shutdown',
        rows=[])
    for direction in args.directions.split(','):
        for rtt in map(float, args.rtts.split(',')):
            for repeat in range(args.repeats):
                variants = args.variants.split(',')
                if repeat%2:
                    variants.reverse()
                for variant in variants:
                    row = await trial(variant, direction, mib=args.mib, seconds=args.seconds, rtt_ms=rtt)
                    row['repeat'] = repeat
                    result['rows'].append(row)
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(result, indent=2)+'\n')
                    print(direction, rtt, repeat, variant, round(row['client_cpu_s'],4),
                          round(row['client_mib_s'],2), flush=True)
    for direction in args.directions.split(','):
        for rtt in map(float, args.rtts.split(',')):
            rows = [r for r in result['rows'] if r['direction']==direction and r['rtt_ms']==rtt]
            baseline = {r['repeat']:r for r in rows if r['variant']=='python'}
            for variant in args.variants.split(','):
                ratios = [baseline[r['repeat']]['client_cpu_s']/r['client_cpu_s'] for r in rows
                          if r['variant']==variant and r['repeat'] in baseline]
                if ratios:
                    print(direction,rtt,variant,'CPU efficiency median',round(statistics.median(ratios),2),
                          'range',round(min(ratios),2),round(max(ratios),2),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker')
    parser.add_argument('--output', type=Path, default=Path('benchmarks/results/rust-ssh.json'))
    parser.add_argument('--mib', type=int, default=16)
    parser.add_argument('--seconds', type=float, default=4)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--variants', default='python,rust-matched,rust')
    parser.add_argument('--directions', default='upload,download')
    parser.add_argument('--rtts', default='0')
    args = parser.parse_args()
    if args.worker:
        asyncio.run(python_worker(json.loads(args.worker)))
    else:
        if args.mib<1 or args.seconds<0 or args.repeats<1:
            parser.error('Invalid workload sizes')
        if set(args.variants.split(','))-{'python','rust-matched','rust'}:
            parser.error('Unknown variant')
        if set(args.directions.split(','))-{'upload','download','idle'}:
            parser.error('Unknown direction')
        asyncio.run(run(args))


if __name__ == '__main__':
    main()
