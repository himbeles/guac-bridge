"""Screen flow-control/frame/batching candidates for >=1.5x throughput gains.

Only use against the local simulator. Matching experimental helper overrides
are mandatory; these values are not negotiated or supported CLI options.
"""
import argparse
import asyncio
import base64
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import platform
import statistics
import time

import aiohttp

from guac_bridge import __version__
from guac_bridge import ssh_client
from guac_bridge.client import Bridge, BridgeError, instruction
from guac_bridge.ssh_client import SSHBridge
from . import transport
from .latency import LatencyProxy


PROFILES = {
    'current': dict(input_window=8, app_window=8, chunk=4096, batch=False),
    'input32': dict(input_window=32, app_window=8, chunk=4096, batch=False),
    'credit32': dict(input_window=8, app_window=32, chunk=4096, batch=False),
    'windows32': dict(input_window=32, app_window=32, chunk=4096, batch=False),
    'chunk16k': dict(input_window=8, app_window=8, chunk=16384, batch=False),
    'windows32-chunk16k': dict(input_window=32, app_window=32, chunk=16384, batch=False),
    'batch': dict(input_window=8, app_window=8, chunk=4096, batch=True),
    'batch-windows32-chunk16k': dict(input_window=32, app_window=32, chunk=16384, batch=True),
}


async def send_batch(tunnel, frames):
    if not frames:
        return
    payload = ''.join(instruction(*frame) for frame in frames)
    metrics = getattr(tunnel, 'benchmark_metrics', None)
    if metrics is not None:
        metrics['client_to_gateway_bytes'] += len(payload.encode())
        metrics['client_to_gateway_instructions'] += len(frames)
        metrics['client_to_gateway_writes'] += 1
    async with tunnel.lock:
        if tunnel.closed:
            raise BridgeError('Guacamole tunnel is closed')
        if tunnel.ws:
            await tunnel.ws.send_str(payload)
        else:
            async with tunnel.session.post(tunnel.base + 'tunnel?write:' + tunnel.ident,
                    data=payload.encode(), headers={'Content-Type': 'application/octet-stream'},
                    allow_redirects=False) as response:
                if response.status != 200:
                    raise BridgeError('HTTP batch write failed')
                await response.read()


class BatchedInput:
    async def send_input(self, data):
        async with self.input_lock:
            pending, batch = self.pending_input, []
            try:
                for offset in range(0, len(data), 3072):
                    if self.failed.is_set() or self.tunnel.closed:
                        raise BridgeError(self.failure or 'Guacamole tunnel is closed')
                    while pending and pending[0].done():
                        pending.popleft().result()
                    if len(pending) >= self.input_window:
                        await send_batch(self.tunnel, batch)
                        batch.clear()
                        await self.wait_for(pending.popleft(), self.args.connect_timeout)
                    pending.append(self.new_ack())
                    batch.append(('blob', self.input_stream,
                                  base64.b64encode(data[offset:offset + 3072]).decode()))
                await send_batch(self.tunnel, batch)
            except BaseException:
                self.fail('Terminal input failed; restart the bridge')
                raise


@contextmanager
def profile_settings(profile):
    saved = (ssh_client.CHUNK, ssh_client.WINDOW, transport.PipelinedBridge, transport.PipelinedSSHBridge)
    attrs = dict(input_window=profile['input_window'], response_queue_size=max(16, profile['app_window'] + 8))
    mixins = (BatchedInput,) if profile['batch'] else ()
    transport.PipelinedBridge = type('MeasuredBridge', (*mixins, Bridge), attrs)
    transport.PipelinedSSHBridge = type('MeasuredSSHBridge', (*mixins, SSHBridge), attrs)
    ssh_client.CHUNK, ssh_client.WINDOW = profile['chunk'], profile['app_window']
    try:
        yield
    finally:
        ssh_client.CHUNK, ssh_client.WINDOW, transport.PipelinedBridge, transport.PipelinedSSHBridge = saved


async def check_latency_model(rtt_ms):
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    proxy = await LatencyProxy('127.0.0.1', server.sockets[0].getsockname()[1], rtt_ms).start()
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
        samples = []
        for _ in range(8):
            start = time.perf_counter()
            writer.write(b'x')
            await writer.drain()
            assert await reader.readexactly(1) == b'x'
            samples.append((time.perf_counter() - start) * 1000)
        # Multiple chunks should overlap their propagation delays; the proxy
        # must not add a full RTT to every chunk of one bulk transfer.
        data = os.urandom(512 * 1024)
        start = time.perf_counter()
        writer.write(data)
        await writer.drain()
        assert await reader.readexactly(len(data)) == data
        bulk = time.perf_counter() - start
        writer.close()
        await writer.wait_closed()
        return dict(configured_rtt_ms=rtt_ms, measured_ping_ms=statistics.median(samples),
                    bulk_echo_bytes=len(data), bulk_echo_wall_s=bulk)
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


async def main(args):
    payload = os.urandom(args.kib * 1024)
    result = dict(environment=dict(python=platform.python_version(), aiohttp=aiohttp.__version__,
                                  package=__version__, platform=platform.platform()),
                  parameters=vars(args), profiles=PROFILES, network_checks=[], rows=[])
    for rtt in map(float, args.rtts.split(',')):
        result['network_checks'].append(await check_latency_model(rtt))
    names = args.profiles.split(',')
    for repeat in range(args.repeats):
        for mode in args.transports.split(','):
            for rtt in map(float, args.rtts.split(',')):
                for workload in args.workloads.split(','):
                    for name in (names if repeat % 2 == 0 else list(reversed(names))):
                        settings = PROFILES[name]
                        print(f'{repeat=} {mode=} {rtt=} {workload=} {name=}', flush=True)
                        with profile_settings(settings):
                            row = await asyncio.wait_for(transport.trial(payload, 'pipeline', mode, 0,
                                workload, rtt_ms=rtt, helper_setup=settings), 180)
                        row.update(profile=name, repeat=repeat)
                        result['rows'].append(row)
                        print(json.dumps(row), flush=True)
                        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                        Path(args.output).write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kib', type=int, default=512)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--rtts', default='0,20,50')
    parser.add_argument('--profiles', default=','.join(PROFILES))
    parser.add_argument('--transports', default='websocket')
    parser.add_argument('--workloads', default='ssh-upload,ssh-download,llm-download')
    parser.add_argument('--output', default='benchmarks/results/windows.json')
    args = parser.parse_args()
    if args.kib < 4 or args.repeats < 1 or any(float(v) < 0 for v in args.rtts.split(',')):
        parser.error('Invalid size, repeat count, or RTT')
    if any(name not in PROFILES for name in args.profiles.split(',')):
        parser.error('Unknown profile')
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main(args))
