"""Client-only CPU counters, real helpers, simulated Guacamole, paced equal work.

uv run --with orjson --with msgspec python -m benchmarks.json_bridge_cpu --output live.json
Linux/Bash/PTY required. No external hosts. The helper remains unchanged.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
import platform
import shlex
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from .json_codecs import codecs, conformance


async def client_worker(config):
    from guac_bridge import client
    from guac_bridge.ssh_client import SSHBridge
    from .transport import start_app
    client.encode_event, client.decode_event = codecs(config.get('fast', False))[config['codec']]
    args = SimpleNamespace(server_command=config['helper'], connect_timeout=30,
        startup_delay=.03, transport='websocket', max_connections=8,
        target_host='127.0.0.1', target_port=config['target_port'], max_requests=4,
        upstream=config['upstream'], request_timeout=120,
        local_api_key=None, upstream_api_key=None)
    async with aiohttp.ClientSession() as session:
        token = await client.login(session, config['base'], 'test', 'test', 10)
        cls = SSHBridge if config['workload'].startswith('ssh') else client.Bridge
        bridge = cls(args, session, config['base'], '1', 'c', 'mysql', token)
        await bridge.start()
        runner = listener = None
        try:
            if config['workload'].startswith('ssh'):
                listener = await asyncio.start_server(bridge.accept, '127.0.0.1', 0)
                endpoint = listener.sockets[0].getsockname()[1]
            else:
                app = web.Application()
                app.router.add_post('/v1/chat/completions', bridge.proxy)
                runner, endpoint = await start_app(app)
            print(json.dumps({'endpoint': endpoint}), flush=True)
            while True:
                command = (await asyncio.to_thread(sys.stdin.readline)).strip()
                if command == 'start':
                    cpu, wall = time.process_time_ns(), time.perf_counter_ns()
                    print('{}', flush=True)
                elif command == 'stop':
                    result = dict(client_cpu_s=(time.process_time_ns()-cpu)/1e9,
                                  wall_s=(time.perf_counter_ns()-wall)/1e9)
                    print(json.dumps(result), flush=True)
                else:
                    break
        finally:
            if listener:
                listener.close()
                await listener.wait_closed()
            if runner:
                await runner.cleanup()
            await bridge.close()


async def paced_write(writer, payload, seconds):
    started = time.perf_counter()
    for offset in range(0, len(payload), 16384):
        await asyncio.sleep(max(0, started + seconds*offset/len(payload) - time.perf_counter()))
        writer.write(payload[offset:offset+16384])
        await writer.drain()


async def trial(codec, workload, seconds, mib=4):
    from .transport import TimedGateway, start_app, child_cpu
    gateway, runners, servers, tasks = TimedGateway(True), [], [], set()
    child = None
    # A deterministic nontrivial binary payload; application bytes are checked.
    payload = hashlib.shake_256(b'guac-json-cpu').digest(mib*1024*1024)
    duration = seconds
    async def target(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            if workload == 'ssh-upload':
                received = await reader.read()
                writer.write(hashlib.sha256(received).digest())
                await writer.drain()
            else:
                await paced_write(writer, payload, duration)
            writer.write_eof()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    async def upstream(request):
        await request.read()
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        started = time.perf_counter()
        for index in range(round(duration*100)):
            await asyncio.sleep(max(0, started+index/100-time.perf_counter()))
            event = dict(index=index, sent=time.perf_counter(), text='x'*80)
            await response.write(b'data: '+json.dumps(event).encode()+b'\n\n')
        await response.write_eof()
        return response

    async def command(value):
        child.stdin.write(value.encode()+b'\n')
        await child.stdin.drain()
        line = await asyncio.wait_for(child.stdout.readline(), 30)
        if not line:
            raise RuntimeError('Client worker exited; inspect stderr')
        return json.loads(line)

    with tempfile.TemporaryDirectory() as directory:
        helper = Path(directory)/'helper'
        module = 'guac_bridge.ssh_server' if workload.startswith('ssh') else 'guac_bridge.server'
        helper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -m '+module+' "$@"\n')
        helper.chmod(0o755)
        try:
            port, upstream_url = 22, ''
            if workload.startswith('ssh'):
                server = await asyncio.start_server(target, '127.0.0.1', 0)
                servers.append(server)
                port = server.sockets[0].getsockname()[1]
            else:
                app = web.Application()
                app.router.add_post('/v1/chat/completions', upstream)
                runner, upstream_url = await start_app(app)
                runners.append(runner)
            runner, base = await start_app(gateway.app())
            runners.append(runner)
            config = dict(codec=codec, workload=workload, helper=str(helper),
                          base=base+'guacamole/', target_port=port, upstream=upstream_url.rstrip('/'),
                          fast=True)
            child = await asyncio.create_subprocess_exec(sys.executable, '-m',
                'benchmarks.json_bridge_cpu', '--worker', json.dumps(config),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            ready = await asyncio.wait_for(child.stdout.readline(), 40)
            endpoint = json.loads(ready)['endpoint']
            async with aiohttp.ClientSession() as session:
                async def transfer():
                    if workload == 'ssh-idle':
                        await asyncio.sleep(duration)
                        return dict(idle=True)
                    if workload.startswith('ssh'):
                        reader, writer = await asyncio.open_connection('127.0.0.1', endpoint)
                        if workload == 'ssh-upload':
                            await paced_write(writer, payload, duration)
                        writer.write_eof()
                        result = await reader.read()
                        expected = hashlib.sha256(payload).digest() if workload == 'ssh-upload' else payload
                        assert result == expected, 'SSH integrity failure'
                        writer.close()
                        await writer.wait_closed()
                        return dict(bytes=len(payload), integrity=True)
                    lags = []
                    async with session.post(endpoint+'v1/chat/completions', json={}) as response:
                        assert response.status == 200
                        async for line in response.content:
                            if line.startswith(b'data: '):
                                event = json.loads(line[6:])
                                assert event['index'] == len(lags)
                                assert event['text'] == 'x'*80
                                lags.append((time.perf_counter()-event['sent'])*1000)
                    assert len(lags) == round(duration*100)
                    return dict(events=len(lags), integrity=True,
                                p95_lag_ms=sorted(lags)[int(.95*(len(lags)-1))])
                # Same payload warmup at a higher rate, outside measurement.
                duration = .2
                await transfer()
                await asyncio.sleep(.1)
                duration = seconds
                helper_start = child_cpu(gateway.terminals[0].proc.pid)
                await command('start')
                result = await transfer()
                result.update(await command('stop'))
                helper_end = child_cpu(gateway.terminals[0].proc.pid)
                result['helper_cpu_s'] = (None if helper_start is None or helper_end is None
                                          else helper_end-helper_start)
                result.update(codec=codec, workload=workload)
                return result
        finally:
            if child:
                if child.returncode is None:
                    child.stdin.write(b'quit\n')
                    with contextlib.suppress(BrokenPipeError):
                        await child.stdin.drain()
                    try:
                        await asyncio.wait_for(child.wait(), 5)
                    except asyncio.TimeoutError:
                        child.kill()
                        await child.wait()
                assert child.returncode == 0, f'Worker failed: {child.returncode}'
            for server in servers:
                server.close()
                await server.wait_closed()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*list(tasks), return_exceptions=True)
            await gateway.close()
            for runner in reversed(runners):
                await runner.cleanup()


async def run(args):
    import orjson, msgspec
    result = dict(python=platform.python_version(), platform=platform.platform(),
                  orjson=orjson.__version__, msgspec=msgspec.__version__,
                  encoder_output_check='isascii+DEL; serializers escape C0 controls',
                  seconds=args.seconds, repeats=args.repeats, mib=args.mib,
                  conformance=conformance(), rows=[])
    for workload in args.workloads.split(','):
        for repeat in range(args.repeats):
            names = args.codecs.split(',')
            if repeat % 2:
                names.reverse()
            for name in names:
                row = await trial(name, workload, args.seconds, args.mib)
                row['repeat'] = repeat
                result['rows'].append(row)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2)+'\n')
                print(workload, repeat, name, round(row['client_cpu_s'], 4), flush=True)
    for workload in args.workloads.split(','):
        rows = [row for row in result['rows'] if row['workload'] == workload]
        baseline = {row['repeat']: row['client_cpu_s'] for row in rows if row['codec'] == 'stdlib'}
        for name in args.codecs.split(','):
            ratios = [baseline[row['repeat']]/row['client_cpu_s'] for row in rows
                      if row['codec'] == name and row['repeat'] in baseline]
            if ratios:
                print(f'{workload} {name}: median paired CPU efficiency '
                      f'{statistics.median(ratios):.2f}x '
                      f'(range {min(ratios):.2f}–{max(ratios):.2f})', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seconds', type=float, default=4)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--mib', type=int, default=4)
    parser.add_argument('--codecs', default='stdlib,orjson,msgspec')
    parser.add_argument('--workloads', default='ssh-upload,ssh-download,llm-paced')
    args = parser.parse_args()
    if args.worker:
        asyncio.run(client_worker(json.loads(args.worker)))
    else:
        if not args.output:
            parser.error('--output is required')
        if args.seconds <= 0 or args.repeats < 1 or args.mib < 1:
            parser.error('--seconds, --repeats and --mib must be positive')
        if set(args.codecs.split(',')) - set(codecs()):
            parser.error('Unknown codec')
        if set(args.workloads.split(',')) - {'ssh-upload', 'ssh-download', 'llm-paced', 'ssh-idle'}:
            parser.error('Unknown workload')
        asyncio.run(run(args))


if __name__ == '__main__':
    main()
