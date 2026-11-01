"""Benchmark-only supervisor: preserve the PTY and collect helper process usage."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    report, prefix, separator, *command = sys.argv[1:]
    if separator != '--':
        raise SystemExit('Expected report path -- helper command')
    process = subprocess.Popen(command)
    _, status, usage = os.wait4(process.pid, 0)
    process.returncode = os.waitstatus_to_exitcode(status)
    destination = Path(report)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(
        **{prefix+'_lifetime_cpu_s': usage.ru_utime + usage.ru_stime,
           prefix+'_peak_rss_kib': usage.ru_maxrss,
           prefix+'_exit': process.returncode},
        stdio='--stdio' in command,
    )))
    temporary.replace(destination)
    raise SystemExit(process.returncode)


if __name__ == '__main__':
    main()
