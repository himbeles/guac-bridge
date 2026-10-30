"""Print median comparisons from a transport benchmark JSON file."""
import argparse
import json
import statistics


def summarize(data):
    rows = data['rows']
    print('| Transport | ACK delay ms | Workload | Baseline MiB/s | Pipeline MiB/s | Speedup |')
    print('| --- | ---: | --- | ---: | ---: | ---: |')
    for transport, delay, workload in sorted({(r['transport'], r['ack_delay_ms'], r['workload']) for r in rows}):
        if workload == 'llm-paced':
            continue
        values = {}
        for variant in ('baseline', 'pipeline'):
            group = [r['mib_s'] for r in rows if r['transport'] == transport and
                     r['ack_delay_ms'] == delay and r['workload'] == workload and r['variant'] == variant]
            if group:
                values[variant] = statistics.median(group)
        if len(values) == 2:
            b, p = values['baseline'], values['pipeline']
            print(f'| {transport} | {delay:g} | {workload} | {b:.3f} | {p:.3f} | {p / b:.2f}x |')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results')
    args = parser.parse_args()
    with open(args.results) as source:
        summarize(json.load(source))
