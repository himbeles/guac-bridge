#!/usr/bin/env python3
"""Local real-PTY throughput comparison; pass a v1.1.2 and a modified client."""
import argparse
import asyncio
import json
import hashlib
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests/native'))
import test_native

async def run(args):
    payload = bytes(range(256)) * (args.mib * 4096)
    results = {name: [] for name in ('baseline', 'debug_off', 'debug_on')}
    for trial in range(args.trials):
        # Alternate order to reduce thermal/cache ordering bias.
        names = list(results)
        if trial % 2:
            names.reverse()
        for name in names:
            test_native.BINARY = Path(args.baseline if name == 'baseline' else args.modified).resolve()
            test_native.NativeTests.child.__kwdefaults__["binary"] = test_native.BINARY
            case = test_native.NativeTests()
            await case.asyncSetUp()
            try:
                target = await case.target(case.echo)
                options = [] if name != 'debug_on' else [
                    '--debug-log', str(case.home/'client.log'),
                    '--server-debug-log', str(case.home/'helper.log')]
                child, port = await case.bridge(target, 'ssh', *options)
                start = time.perf_counter()
                await case.roundtrip(port, payload)
                seconds = time.perf_counter() - start
                await case.finish(child)
                results[name].append(seconds)
                print(f'{name} trial={trial+1} seconds={seconds:.4f}', file=sys.stderr)
            finally:
                await case.asyncTearDown()
    print(json.dumps({
        'baseline_sha256': hashlib.sha256(Path(args.baseline).read_bytes()).hexdigest(),
        'modified_sha256': hashlib.sha256(Path(args.modified).read_bytes()).hexdigest(),
        'payload_bytes_each_direction': len(payload),
        'transport': 'local Guacamole simulator + real Linux Bash PTY; exact payload equality checked',
        'cases': {name: {'seconds': samples, 'median_seconds': statistics.median(samples),
                         'median_mib_per_second_bidirectional': 2*args.mib/statistics.median(samples)}
                  for name, samples in results.items()}
    }, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--modified', required=True)
    parser.add_argument('--mib', type=int, default=16)
    parser.add_argument('--trials', type=int, default=5)
    asyncio.run(run(parser.parse_args()))
