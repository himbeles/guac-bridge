#!/usr/bin/env python3
"""Regenerate third-party notices with cargo-about 0.9.3, or check their inputs."""
import argparse
import hashlib
import gzip
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'crates/guac-bridge/assets'
REPORT = ASSETS / 'THIRD-PARTY-NOTICES.txt'
MANIFEST = ROOT / 'licensing/manifest.json'
INPUTS = ('Cargo.lock', 'licensing/about.toml', 'licensing/notices.hbs', 'scripts/licenses.py', 'LICENSE')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RustNotices(HTMLParser):
    def __init__(self):
        super().__init__()
        self.body = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'body': self.body = True
        if self.body and tag in ('p', 'div', 'pre', 'h1', 'h2', 'h3', 'li', 'br'): self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'body': self.body = False
        if self.body and tag in ('p', 'div', 'pre', 'h1', 'h2', 'h3', 'li'): self.parts.append('\n')

    def handle_data(self, data):
        if self.body: self.parts.append(data)


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    inputs = {name: digest(ROOT / name) for name in INPUTS}
    if args.check:
        manifest = json.loads(MANIFEST.read_text())
        if manifest['inputs'] != inputs or manifest['report_sha256'] != digest(REPORT) or manifest['compressed_sha256'] != digest(REPORT.with_suffix('.txt.gz')):
            raise SystemExit('License notices are stale. Install cargo-about 0.9.3 and run python scripts/licenses.py.')
        if manifest['rustc'] != run('rustc', '--version').strip():
            raise SystemExit('Rust toolchain changed; regenerate standard-library notices.')
        if (ASSETS / 'LICENSE').read_bytes() != (ROOT / 'LICENSE').read_bytes():
            raise SystemExit('Packaged project license differs from LICENSE; regenerate notices.')
        print('License inputs, report and packaged MIT license are current.')
        return
    if run('cargo', 'about', '--version').strip() != 'cargo-about 0.9.3':
        raise SystemExit('Install cargo-about 0.9.3 with --locked --features cli.')
    metadata = json.loads(run('cargo', 'metadata', '--locked', '--all-features', '--format-version', '1'))
    packages = {p['name']: p for p in metadata['packages']}
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / 'notices.txt'
        subprocess.run(['cargo', 'about', 'generate', '--locked', '--workspace', '--all-features',
                        '--fail', '-c', 'licensing/about.toml', 'licensing/notices.hbs', '-o', str(output)],
                       cwd=ROOT, check=True)
        report = output.read_text()
    # Cargo metadata describes Rust wrappers; preserve notices for vendored C code too.
    supplements = {
        'libdbus-sys': ('vendor/dbus/COPYING', 'vendor/dbus/AUTHORS'),
        'openssl-src': ('openssl/LICENSE.txt',),
        'ring': ('LICENSE', 'LICENSE-BoringSSL', 'LICENSE-other-bits', 'third_party/fiat/LICENSE'),
    }
    report += '\nVENDORED NATIVE COMPONENTS\nD-Bus library: distributed under the AFL-2.1 option of its dual license.\nOpenSSL: Apache-2.0. ring: retain the component terms below.\n'
    for name, files in supplements.items():
        package = packages[name]
        source = Path(package['manifest_path']).parent
        for filename in files:
            report += f'\n=== {name} {package["version"]}: {filename} ===\n'
            report += (source / filename).read_text() + '\n'
    version = run('rustc', '--version').strip()
    sysroot = Path(run('rustc', '--print', 'sysroot').strip())
    rust = RustNotices()
    rust.feed((sysroot / 'share/doc/rust/COPYRIGHT-library.html').read_text())
    report += '\nRUST STANDARD LIBRARY AND BUNDLED COMPONENTS\n' + version + '\n'
    report += f'Source: https://github.com/rust-lang/rust/tree/{version.split()[1]}/library\n'
    report += re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', ''.join(rust.parts)).strip() + '\n'
    REPORT.write_text(report, encoding='utf-8')
    REPORT.with_suffix('.txt.gz').write_bytes(gzip.compress(report.encode('utf-8'), mtime=0))
    (ASSETS / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
    MANIFEST.write_text(json.dumps({'generator': 'cargo-about 0.9.3', 'rustc': version,
                                   'inputs': inputs, 'report_sha256': digest(REPORT),
                                   'compressed_sha256': digest(REPORT.with_suffix('.txt.gz'))}, indent=2) + '\n')
    print(f'Generated {REPORT.relative_to(ROOT)} ({REPORT.stat().st_size:,} bytes).')


if __name__ == '__main__':
    main()
