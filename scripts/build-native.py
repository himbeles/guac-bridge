"""Build the Linux static agent, then the native client or optional Python package."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--agent-only', action='store_true')
p.add_argument('--wheel', action='store_true')
p.add_argument('--agent', type=Path, help='Use an existing Linux x86-64 musl agent, e.g. on Windows')
a = p.parse_args()
if a.agent:
    agent = a.agent.resolve()
else:
    subprocess.run(['rustup', 'target', 'add', 'x86_64-unknown-linux-musl'], check=True)
    subprocess.run(['cargo', 'build', '--release', '-p', 'guac-bridge', '--no-default-features', '--bin', 'guac-bridge-agent', '--target', 'x86_64-unknown-linux-musl'], cwd=ROOT, check=True, env={**os.environ, 'CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER': 'rust-lld'})
    agent = ROOT / 'target/x86_64-unknown-linux-musl/release/guac-bridge-agent'
dest = ROOT / 'crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64'
dest.parent.mkdir(exist_ok=True)
shutil.copyfile(agent, dest)
if not a.agent_only:
    command = [sys.executable, '-m', 'maturin', 'build', '--release'] if a.wheel else ['cargo', 'build', '--release', '-p', 'guac-bridge', '--bins']
    subprocess.run(command, cwd=ROOT, check=True)
