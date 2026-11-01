"""Optional Python launcher for the standalone Rust application."""
import shutil
import subprocess
import sys
from pathlib import Path


def run(arguments=()):
    """Run the native CLI and return its exit code; no protocol work occurs in Python."""
    name = 'guac-bridge.exe' if sys.platform == 'win32' else 'guac-bridge'
    executable = Path(sys.executable).parent / name
    if not executable.exists():
        executable = shutil.which(name)
    if not executable:
        raise RuntimeError('The guac-bridge executable is missing from this installation')
    return subprocess.call([str(executable), *arguments])


def cli():
    raise SystemExit(run(sys.argv[1:]))
