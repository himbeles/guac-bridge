# Build and package

Run from the repository root with Rust 1.95 or newer via rustup and native build tools:

```sh
cargo xtask build
cargo xtask check
cargo xtask portable
```

The commands are identical on Linux, macOS and Windows. `cargo xtask --help` is the task
reference. `Cargo.toml` holds the only application version; Python packages and
installer metadata derive theirs from it.

`build` rebuilds the static Linux helper, builds the Windows helper on Windows,
then embeds them in the native client. Its outputs are reported by Cargo.
Linux development can omit Windows server support. To include it, use a Windows
helper built from the same source revision:

```sh
cargo xtask build --windows-agent /path/to/guac-bridge-agent-windows-x86_64.exe
```

CI prepares matching helpers once and runs subsequent tasks with `--prepared`.
This skips rebuilding helpers; it does not verify their provenance. Do not reuse
staged helpers after changing helper code or the application version.

## Requirements

- Linux x86-64: Rust/rustup, a C compiler and native build tools. The helper uses
  musl and Rust's bundled `rust-lld`; no separate musl compiler is needed.
- macOS (Apple Silicon): Rust/rustup and Xcode Command Line Tools
  (`xcode-select --install`). Saved passwords use macOS Keychain.
- Windows x86-64: Rust MSVC, Visual Studio C++ Build Tools and a Windows SDK.
- Windows installer: Inno Setup 6, optionally selected with `ISCC`.
- Python/uv packages: Python and maturin (`python -m pip install maturin`).
- End-to-end tests: Python and aiohttp (`python -m pip install aiohttp`).

Python is optional for native builds. PowerShell remains in Windows test fixtures
and the remote bootstrap; it does not orchestrate builds or packaging.

## Distribution tasks

```sh
cargo xtask portable                         # native ZIP or tar.gz in dist/
cargo xtask installer                        # Windows, bundled UI runtime
cargo xtask python                           # Python/uv wheel, shared UI runtime
cargo xtask python --sdist                   # Python source distribution
cargo xtask portable --wheel dist/FILE.whl    # preserve the wheel's native binary
cargo xtask verify-release --tag vVERSION
cargo xtask release-notes                    # checksums and notes; never publish
```

The installer stages only runtime files and redistribution licenses, validates
its embedded activation manifest, and writes `dist/installer-size.json`.
Portable Windows packages and wheels use the installed shared UI runtime.
Linux CI builds its wheel in manylinux before extracting its portable binary;
a normal host build does not promise that manylinux compatibility.

Cargo's target directory is discovered from `cargo metadata`; binary locations
come from compiler-artifact messages. The existing integration supervisors expect
`target/release`, so use the default target directory and native host target when
running them. `PYTHON` selects their interpreter.

For native installation from this checkout:

```sh
cargo install --path crates/guac-bridge --bin guac-bridge
```

GitHub Actions uses these same tasks. Publishing remains a CI action triggered by
a matching release tag or a main commit beginning with `Release v`.

Stable releases include a macOS Apple Silicon (`macos-aarch64`) archive and Python
wheel. The client embeds the same Linux and Windows helpers. CI builds and checks
the client on a native Apple Silicon runner;
macOS 11 or newer is the deployment target. Beta releases remain Windows-installer-only.

The [website](../site/README.md) is hand-edited HTML/CSS. Its Pages action refreshes
direct download links from the latest stable GitHub release.

See [Licensing](licensing.md) for notice regeneration and package checks.
