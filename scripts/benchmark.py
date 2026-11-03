"""Measure bridge throughput, latency and CPU through the real-PTY test gateway."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests/native'))
from gateway import Gateway


async def trial(args):
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp)
        if args.warm_cache:
            agent = (ROOT/'crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64').read_bytes()
            cache = home/'cache/guac-bridge/helpers'
            cache.mkdir(parents=True)
            dest = cache/hashlib.sha256(agent).hexdigest()
            dest.write_bytes(agent)
            dest.chmod(0o700)
        gateway = Gateway(home)
        gateway.one_way_latency = args.rtt_ms/2000
        url = await gateway.start()
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
                writer.write_eof()
            finally:
                writer.close()
        server = await asyncio.start_server(echo, '127.0.0.1', 0)
        env = dict(os.environ, GUAC_BRIDGE_PASSWORD='test', NO_PROXY='127.0.0.1')
        command_line = [str(args.client.resolve()), 'ssh', '--guacamole-url', url, '--guacamole-user', 'test', '--no-keyring', '--local-port', '0', '--remote-port', str(server.sockets[0].getsockname()[1]), '--startup-delay', '0', '--window', str(args.window), '--frame-size', str(args.frame_size), '--input-window', str(args.input_window), '--benchmark-control']
        child = await asyncio.create_subprocess_exec(*command_line, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        async def command(text):
            child.stdin.write(text.encode()+b'\n')
            await child.stdin.drain()
            line = await asyncio.wait_for(child.stdout.readline(), 120)
            if not line:
                raise RuntimeError((await child.stderr.read()).decode())
            return json.loads(line)
        try:
            line = await asyncio.wait_for(child.stdout.readline(), 120)
            if not line:
                raise RuntimeError((await child.stderr.read()).decode())
            endpoint = json.loads(line)['endpoint']
            term = gateway.terminals[0]
            term.drop_every = args.drop_every
            payload = bytes(range(256))*4096
            results = []
            async def exchange():
                reader, writer = await asyncio.open_connection('127.0.0.1', endpoint)
                sent, received = hashlib.sha256(), hashlib.sha256()
                async def upload():
                    for _ in range(args.mib):
                        writer.write(payload)
                        sent.update(payload)
                        await writer.drain()
                    writer.write_eof()
                async def download():
                    total = 0
                    while data := await reader.read(65536):
                        received.update(data)
                        total += len(data)
                    return total
                _, total = await asyncio.gather(upload(), download())
                writer.close()
                await writer.wait_closed()
                assert sent.digest() == received.digest() and total == args.mib*1048576
                return total
            for _ in range(args.repeats):
                await command('start')
                wire_start = len(term.all_input)
                start = time.perf_counter()
                latencies = []
                if args.idle_seconds:
                    await asyncio.sleep(args.idle_seconds)
                    total = 0
                elif args.pings:
                    reader, writer = await asyncio.open_connection('127.0.0.1', endpoint)
                    for _ in range(args.pings):
                        ping_start = time.perf_counter()
                        writer.write(payload[:64])
                        await writer.drain()
                        assert await reader.readexactly(64) == payload[:64]
                        latencies.append((time.perf_counter()-ping_start)*1000)
                    writer.write_eof()
                    assert await reader.read() == b''
                    writer.close()
                    await writer.wait_closed()
                    total = args.pings*64
                else:
                    totals = await asyncio.wait_for(asyncio.gather(*(exchange() for _ in range(args.connections))), 240)
                    total = sum(totals)
                elapsed = time.perf_counter()-start
                counters = await command('stop')
                results.append({**counters, 'bytes_each_direction': total, 'elapsed_s': elapsed,
                    'mib_per_s_each_direction': total/1048576/elapsed, 'input_wire_bytes': len(term.all_input)-wire_start,
                    **({'ping_median_ms': statistics.median(latencies), 'ping_p95_ms': sorted(latencies)[int(len(latencies)*.95)]} if latencies else {})})
            child.stdin.write(b'quit\n')
            await child.stdin.drain()
            await asyncio.wait_for(child.wait(), 10)
            error = (await child.stderr.read()).decode()
            assert child.returncode == 0, error
            cpu_file = home/'helper-cpu.txt'
            for _ in range(50):
                if cpu_file.exists(): break
                await asyncio.sleep(.01)
            return {'trials': results, 'helper_cpu_s': float(cpu_file.read_text()) if cpu_file.exists() else None,
                    'injected_drops': len(term.applied_faults), 'warnings': error}
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            server.close()
            await server.wait_closed()
            await gateway.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client', type=Path, default=ROOT/'target/release/guac-bridge')
    p.add_argument('--mib', type=int, default=32)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--window', type=int, choices=range(1,65), default=4)
    p.add_argument('--input-window', type=int, choices=range(1,129), default=8)
    p.add_argument('--frame-size', type=int, choices=(4096,8192,16384,32768,65536), default=65536)
    p.add_argument('--rtt-ms', type=float, default=0)
    p.add_argument('--drop-every', type=int, default=0)
    p.add_argument('--warm-cache', action='store_true')
    p.add_argument('--connections', type=int, default=1)
    p.add_argument('--pings', type=int, default=0)
    p.add_argument('--idle-seconds', type=float, default=0)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    if min(a.mib,a.repeats,a.connections)<1 or min(a.rtt_ms,a.drop_every,a.pings,a.idle_seconds)<0:
        p.error('sizes/repeats/connections must be positive and delays/counts nonnegative')
    result = {'client_sha256': hashlib.sha256(a.client.read_bytes()).hexdigest(),
              'settings': {k: str(v) if isinstance(v,Path) else v for k,v in vars(a).items()},
              **asyncio.run(trial(a))}
    text = json.dumps(result, indent=2)
    print(text)
    if a.output:
        a.output.write_text(text+'\n')

if __name__ == '__main__':
    main()
