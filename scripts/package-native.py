"""Extract native entry points from a maturin wheel and make a portable release archive."""
from pathlib import Path
import argparse
import os
import tarfile
import tempfile
import zipfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('wheel',type=Path)
p.add_argument('--out',type=Path,default=Path('dist'))
a = p.parse_args()
a.out.mkdir(exist_ok=True,parents=True)
windows = 'win_amd64' in a.wheel.name
suffix = 'windows-x86_64' if windows else 'linux-x86_64'
version = a.wheel.name.split('-')[1]
with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)
    root = Path(__file__).resolve().parents[1]
    for document in ('LICENSE', 'README.md'):
        (folder/document).write_bytes((root/document).read_bytes())
    with zipfile.ZipFile(a.wheel) as wheel:
        for name in ('guac-bridge','guac-ssh-client','guac-llm-client'):
            name += '.exe' if windows else ''
            paths = [n for n in wheel.namelist() if n.endswith('/scripts/'+name)]
            if len(paths)!=1:
                raise SystemExit(f'Missing or duplicate native binary: {name}')
            dest = folder/name
            dest.write_bytes(wheel.read(paths[0]))
            dest.chmod(0o755)
    if windows:
        with zipfile.ZipFile(a.out/f'guac-bridge-{version}-{suffix}.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for file in folder.iterdir():
                archive.write(file,file.name)
    else:
        with tarfile.open(a.out/f'guac-bridge-{version}-{suffix}.tar.gz','w:gz') as archive:
            for file in folder.iterdir():
                archive.add(file,arcname=file.name)
