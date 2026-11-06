#!/usr/bin/env python3
"""Check license content in built wheels, source packages and portable archives."""
import argparse
from email.parser import BytesParser
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'LICENSE': (ROOT / 'LICENSE').read_bytes(),
    'THIRD-PARTY-NOTICES.txt': (ROOT / 'crates/guac-bridge/assets/THIRD-PARTY-NOTICES.txt').read_bytes(),
}


def check(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist()
                     if Path(name).name in EXPECTED or name.endswith('.dist-info/METADATA')}
    else:
        with tarfile.open(path) as archive:
            files = {member.name: archive.extractfile(member).read() for member in archive.getmembers()
                     if member.isfile() and Path(member.name).name in EXPECTED}
    for basename, content in EXPECTED.items():
        matches = [data for name, data in files.items() if Path(name).name == basename]
        if not matches or any(data != content for data in matches):
            raise ValueError(f'{path.name}: missing or incorrect {basename}')
    if path.suffix == '.whl':
        metadata = [data for name, data in files.items() if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError(f'{path.name}: expected one package metadata file')
        headers = BytesParser().parsebytes(metadata[0])
        if headers['License-Expression'] != 'MIT':
            raise ValueError(f'{path.name}: project license metadata is not MIT')
        declared = {Path(name).name for name in headers.get_all('License-File', [])}
        if not EXPECTED.keys() <= declared:
            raise ValueError(f'{path.name}: both license files must be declared in wheel metadata')
    print(f'{path.name}: MIT license and full third-party notices verified')


def check_helper(path):
    binary = path.read_bytes()
    compressed = (ROOT / 'crates/guac-bridge/assets/THIRD-PARTY-NOTICES.txt.gz').read_bytes()
    if compressed in binary or b'THIRD-PARTY SOFTWARE NOTICES' in binary:
        raise ValueError(f'{path.name}: helper must exclude the embedded license report')
    print(f'{path.name}: helper excludes the embedded license report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*')
    parser.add_argument('--helper', action='append', type=Path, default=[])
    args = parser.parse_args()
    for helper in args.helper:
        check_helper(helper)
    paths = []
    for arg in args.archives:
        path = Path(arg)
        if path.is_dir():
            paths.extend(p for p in path.iterdir() if p.name.endswith(('.whl', '.tar.gz', '.zip', '.crate')))
        else:
            paths.append(path)
    if not paths and not args.helper:
        raise SystemExit('No distribution archives to check')
    for path in sorted(paths):
        check(path)


if __name__ == '__main__':
    main()
