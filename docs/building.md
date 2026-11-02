# Building

The workspace contains one standalone Cargo package, `guac-bridge`, and an optional Python launcher. The CLI, credentials, background processes, helper deployment and forwarding are implemented in Rust.

The instructions below build from a checkout without GitHub Actions or the Python build scripts. Run commands from the repository root. Build both helpers from the same commit and version as the clients: helper bytes are embedded at client compile time.

## One-command local builds

From an extracted source checkout, with Rust/rustup and native build tools installed:

```powershell
# Windows: both server helpers, Windows clients and portable ZIP
.\build.ps1
# Also run Rust checks and create the installer (Inno Setup 6 required)
.\build.ps1 -Check -Installer
```

```sh
# Linux: static Linux helper and Linux clients
make build
make check
make package
# Optionally embed a Windows helper built from the same source revision
make build WINDOWS_AGENT=/path/to/guac-bridge-agent-windows-x86_64.exe
```

These commands rebuild and embed the helper before the clients. They do not push, publish, call GitHub Actions or use Python. `make integration` additionally runs the local PTY integration tests and requires Python with aiohttp. Windows integration tests still need a Windows machine; the PowerShell build script is provided for local verification there.

## Prerequisites

- Current stable Rust installed with rustup, plus Git.
- On Windows x86-64: the MSVC Rust toolchain, Visual Studio Build Tools with **Desktop development with C++**, and a Windows SDK. Use a developer PowerShell if the MSVC linker is not found.
- On Linux x86-64: a C compiler and the usual native build tools for the client. The Linux helper itself uses Rust's musl target and bundled `rust-lld` linker.
- To create the Windows installer: Inno Setup 6. Cargo builds the application; Inno Setup packages the installer.

Initial compilation downloads Rust targets and Cargo dependencies. The remote server does not need Rust, a package manager or internet access.

## Build everything on Windows with Cargo

A Windows x86-64 development machine can build **both server helpers and the Windows clients**. No Linux machine or CI artifact is needed for this path.

### 1. Check out the source and select MSVC

```powershell
git clone https://github.com/himbeles/guac-bridge.git
Set-Location guac-bridge
# Until Windows server support is merged:
git switch feat/windows-server

rustup toolchain install stable-x86_64-pc-windows-msvc
rustup override set stable-x86_64-pc-windows-msvc
rustup target add x86_64-unknown-linux-musl x86_64-pc-windows-msvc
New-Item -ItemType Directory -Force crates/guac-bridge/assets | Out-Null
```

For a reproducible build, check out the desired tag or commit instead of a moving branch. All subsequent commands use that checkout.

### 2. Build and stage the static Linux helper

```powershell
$env:CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER = 'rust-lld'
cargo build  --release -p guac-bridge --no-default-features --bin guac-bridge-agent --target x86_64-unknown-linux-musl
Copy-Item target/x86_64-unknown-linux-musl/release/guac-bridge-agent crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64 -Force
```

`--no-default-features` leaves out client networking and keychain dependencies. The musl helper is a Linux x86-64 executable; it is bundled by the Windows clients but runs only on the remote Linux server.

### 3. Build and stage the Windows helper

```powershell
cargo build  --release -p guac-bridge --no-default-features --bin guac-bridge-agent --target x86_64-pc-windows-msvc
Copy-Item target/x86_64-pc-windows-msvc/release/guac-bridge-agent.exe crates/guac-bridge/assets/guac-bridge-agent-windows-x86_64.exe -Force
```

The repository's `.cargo/config.toml` enables the static MSVC CRT for this explicit Windows target. The helper therefore does not need a separately installed Visual C++ runtime on the server.

### 4. Build and check the Windows clients

```powershell
$env:GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT = '1'
cargo build  --release -p guac-bridge --bins --examples
cargo test  -p guac-bridge
cargo clippy  -p guac-bridge --all-targets -- -D warnings

& ./target/release/guac-bridge.exe --version
& ./target/release/guac-ssh-client.exe --help
& ./target/release/guac-llm-client.exe --help
```

The three clients in `target/release` embed both staged helpers. `GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT=1` makes a missing Windows asset a build error rather than silently producing a Linux-server-only client. A missing or invalid Linux asset also prevents a complete client build.

For a local Cargo installation of those entry points:

```powershell
cargo install  --path crates/guac-bridge --bin guac-bridge --bin guac-ssh-client --bin guac-llm-client --force
```

This installs into Cargo's binary directory, usually `%USERPROFILE%\.cargo\bin`. The installer below is the alternative for distributing to machines without Rust.

### 5. Create the Windows installer and portable archive

Use the version in the checkout's manifests. The commands read it from `Cargo.toml`, so no Python is needed:

```powershell
$version = ((Select-String -Path Cargo.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches.Groups[1].Value)
if (-not $version) { throw 'Cannot read workspace version' }
$inno = "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"
& $inno "/DVersion=$version" installer/guac-bridge.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }

New-Item -ItemType Directory -Force dist | Out-Null
Compress-Archive -Path target/release/guac-bridge.exe,target/release/guac-ssh-client.exe,target/release/guac-llm-client.exe,LICENSE,README.md -DestinationPath "dist/guac-bridge-$version-windows-x86_64.zip" -Force
Get-FileHash dist/* -Algorithm SHA256
```

If Inno Setup is installed elsewhere, change `$inno`. The installer is written to `dist/guac-bridge-<version>-windows-x86_64-setup.exe`. It installs per user, requires no administrator privileges, and offers **Add Guac Bridge to my user PATH**. Both the installer and portable archive contain clients with their helpers embedded; separate helper files are unnecessary on end-user machines.

## Build Linux clients with Cargo

Run these commands on the Linux x86-64 client build machine, from the same source revision used for the Windows helper:

```sh
rustup target add x86_64-unknown-linux-musl
mkdir -p crates/guac-bridge/assets
cargo build  --release -p guac-bridge --no-default-features --bin guac-bridge-agent --target x86_64-unknown-linux-musl --config 'target.x86_64-unknown-linux-musl.linker="rust-lld"'
cp target/x86_64-unknown-linux-musl/release/guac-bridge-agent crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64
```

Copy `crates/guac-bridge/assets/guac-bridge-agent-windows-x86_64.exe` from step 3 on the Windows build machine into the same path in the Linux checkout. This guide builds the Windows MSVC helper on Windows; it does not assume Linux can build an MSVC executable without an additional cross-compilation setup.

Then build clients that support **both** remote operating systems:

```sh
export GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT=1
cargo build  --release -p guac-bridge --bins
cargo test  -p guac-bridge
cargo clippy  -p guac-bridge --all-targets -- -D warnings
cargo install  --path crates/guac-bridge --bin guac-bridge --bin guac-ssh-client --bin guac-llm-client --force

mkdir -p dist
tar -czf dist/guac-bridge-linux-x86_64.tar.gz LICENSE README.md -C target/release guac-bridge guac-ssh-client guac-llm-client
sha256sum dist/guac-bridge-linux-x86_64.tar.gz
```

Linux clients use the build machine's normal native target and system library compatibility. Building on a newer distribution does not make the client compatible with every older distribution. The **server helper** is the separate static musl executable.

For Linux-server-only development, omit the Windows asset and leave `GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT` unset. Such a build reports a missing bundled helper if `--server-os windows` is selected.

## Rebuilding and selecting helper assets

Repeat the helper build and copy steps whenever helper source or the package version changes, then rebuild the clients. Cargo notices changed staged files and re-embeds their bytes, but it does not rebuild an already-staged helper automatically.

You may select prepared files without copying them into the asset directory:

| Variable | Purpose |
| --- | --- |
| `GUAC_BRIDGE_AGENT` | Path to a Linux x86-64 ELF helper |
| `GUAC_BRIDGE_WINDOWS_AGENT` | Path to a Windows x86-64 PE helper |
| `GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT=1` | Fail a client build if the Windows helper is missing |

If no Linux asset is present, a source client build can build it automatically using rustup and `rust-lld`. On Windows, a missing Windows asset is also built automatically with MSVC. Explicit staging is useful when producing distributable builds and keeping both platforms at the same source revision.

Cargo's `--target` changes the output directory. The examples above stage helpers from `target/<target-triple>/release`, then build host clients into `target/release`, which is where the installer expects them. If you set `CARGO_TARGET_DIR` or a global `build.target`, adjust the copy and installer input paths accordingly.

## Integration checks outside CI

Cargo tests cover framing, validation and management utilities. The end-to-end test supervisors additionally require Python and `aiohttp`; neither is needed by the built native programs:

```sh
python -m pip install aiohttp
```

On Linux, after the client and helper builds:

```sh
python -m unittest discover -s tests/native -p test_native.py -v
```

On Windows, after step 4 builds the clients and console test examples:

```powershell
python tests/windows/probe.py
python -m unittest discover -s tests/windows -p test_agent.py -v
python -m unittest discover -s tests/windows -p test_bootstrap.py -v
$env:GUAC_TEST_SHELL = 'cmd.exe'
python -m unittest discover -s tests/windows -p test_bootstrap.py -v
Remove-Item Env:GUAC_TEST_SHELL
```

These use a simulated Guacamole gateway with a real Bash PTY or Windows ConPTY. They do not replace checking your actual Guacamole SSH connection. Windows RDP connections do not provide the SSH terminal pipes used here.

## Optional uv/Python distribution

After staging **both** helper assets, install maturin and build the platform wheel and source archive:

```sh
cargo install  maturin
maturin build --release  --out dist
maturin sdist --out dist
```

This optional step requires a Python interpreter. Maturin's `bin` bindings package the same three native client executables, with the small `guac_bridge.run(arguments)` launcher and `python -m guac_bridge`. There is no PyO3 extension or Python data path. Build the Windows wheel on Windows and the Linux wheel on Linux. CI uses a manylinux build environment for the Linux wheel; a normal native Linux Cargo build alone does not reproduce that compatibility guarantee.

## Cargo installation from Git and CI publication

Once a version containing these changes is released, users can install its native commands directly with Cargo:

```sh
cargo install  --git https://github.com/himbeles/guac-bridge --bin guac-bridge --bin guac-ssh-client --bin guac-llm-client
```

Use `--tag <release-tag>` to select an exact release. The automatic helper build behavior above applies to Git source installs; prepared Cargo packages and source distributions can contain the helper assets already.

None of the manual commands above publishes a release. The repository workflow builds and tests the helpers, clients, installers and Python distributions. A matching `v` tag, or a main-branch commit beginning with `Release v`, publishes through PyPI Trusted Publishing and creates a GitHub release after checks pass. Other branch builds only upload reviewable artifacts.
