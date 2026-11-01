"""Measure production flow control with delay in both directions."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import platform
import statistics
import aiohttp
from guac_bridge import __version__
from .transport import trial
from .latency_check import check_latency_model


async def main(args):
    payload = os.urandom(args.kib * 1024)
    result = dict(environment=dict(python=platform.python_version(), aiohttp=aiohttp.__version__,
                                  package=__version__, platform=platform.platform()),
                  parameters=vars(args), network_checks=[], rows=[])
    for rtt in map(float, args.rtts.split(',')):
        result['network_checks'].append(await check_latency_model(rtt))
    for repeat in range(args.repeats):
        for rtt in map(float, args.rtts.split(',')):
            for workload in args.workloads.split(','):
                row = await asyncio.wait_for(trial(payload, 'pipeline', 'websocket', 0,
                    workload, rtt_ms=rtt), 180)
                row['repeat'] = repeat
                result['rows'].append(row)
                print(json.dumps(row), flush=True)
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print('Median MiB/s:')
    for rtt in map(float, args.rtts.split(',')):
        for workload in args.workloads.split(','):
            values = [r['mib_s'] for r in result['rows']
                      if r['application_rtt_ms'] == rtt and r['workload'] == workload]
            print(rtt, workload, statistics.median(values))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kib', type=int, default=512)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--rtts', default='0,20,50')
    parser.add_argument('--workloads', default='ssh-upload,ssh-download,llm-download')
    parser.add_argument('--output', default='benchmarks/results/flow.json')
    args = parser.parse_args()
    if args.kib < 4 or args.repeats < 1 or any(not 0 <= float(r) <= 10000 for r in args.rtts.split(',')):
        parser.error('Invalid size, repeat count, or RTT')
    if any(w not in ('ssh-upload', 'ssh-download', 'llm-upload', 'llm-download', 'llm-paced') for w in args.workloads.split(',')):
        parser.error('Unknown workload')
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(main(args))
