"""Measure a real, already configured SSH alias without copying any files.

Works on Windows/macOS/Linux. Uses the existing ssh config, including its
guac-ssh-client --ensure-running hook. Generates/discards synthetic bytes only.
Requires POSIX head, /dev/zero, and wc on the server. Authentication remains
with OpenSSH; the probe does not collect passwords or keys.
"""
import argparse
import json
import subprocess
import time


def run(alias, mib):
    count = mib * 1024 * 1024
    base = ['ssh', '-T', '-o', 'Compression=no', alias]
    started = time.perf_counter()
    with subprocess.Popen(base + [f'head -c {count} /dev/zero'], stdout=subprocess.PIPE) as process:
        received = 0
        while data := process.stdout.read(65536):
            received += len(data)
        if process.wait() != 0 or received != count:
            raise RuntimeError('Download failed or byte count differed')
    elapsed = time.perf_counter() - started
    print(json.dumps(dict(direction='download', bytes=count, wall_s=elapsed, mib_s=mib / elapsed)), flush=True)
    started = time.perf_counter()
    with subprocess.Popen(base + ['wc -c'], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        block = b'\0' * 65536
        for offset in range(0, count, len(block)):
            process.stdin.write(block[:min(len(block), count - offset)])
        process.stdin.close()
        response = process.stdout.read()
        if process.wait() != 0 or int(response.strip()) != count:
            raise RuntimeError('Upload failed or byte count differed')
    elapsed = time.perf_counter() - started
    print(json.dumps(dict(direction='upload', bytes=count, wall_s=elapsed, mib_s=mib / elapsed)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('alias', help='Existing SSH config alias, not a URL or shell command')
    parser.add_argument('--mib', type=int, default=1)
    args = parser.parse_args()
    if args.alias.startswith('-') or args.mib < 1 or args.mib > 1024:
        parser.error('Use a non-option SSH alias and --mib between 1 and 1024')
    run(args.alias, args.mib)
