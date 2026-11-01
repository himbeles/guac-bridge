# Building

One workspace contains the standalone `guac-bridge` Cargo package and the optional Python launcher. Application logic, authentication, native keychain access, background management, provisioning and forwarding are Rust code. The helper-only feature set omits client networking and keychain dependencies.

## Cargo installation

Install directly from the repository on Windows or Linux x86-64:

```shell
cargo install --git https://github.com/himbeles/guac-bridge --tag v1.0.0 --bin guac-bridge --bin guac-ssh-client --bin guac-llm-client
```

The source build prepares the helper automatically. The commands below also support development and packaging.

## Linux x86-64

Install a current stable Rust toolchain. Build the static Linux helper and native clients:

```shell
python scripts/build-native.py
cargo test -p guac-bridge
cargo clippy -p guac-bridge --all-targets -- -D warnings
cargo install --path crates/guac-bridge --bin guac-bridge --bin guac-ssh-client --bin guac-llm-client
```

The Python build script is developer convenience, not a runtime requirement. Its native equivalent is:

```shell
rustup target add x86_64-unknown-linux-musl
cargo build --release -p guac-bridge --no-default-features --bin guac-bridge-agent --target x86_64-unknown-linux-musl --config 'target.x86_64-unknown-linux-musl.linker="rust-lld"'
mkdir -p crates/guac-bridge/assets
cp target/x86_64-unknown-linux-musl/release/guac-bridge-agent crates/guac-bridge/assets/guac-bridge-agent-linux-x86_64
cargo build --release -p guac-bridge --bins
```

Client builds embed the helper bytes and reject a non-x86-64 ELF asset. If a source checkout has no prepared asset, the Cargo build script compiles the Linux helper itself using Rust’s musl target and bundled linker, including when building on Windows. This requires rustup to obtain that target, but no Python or Linux C compiler. Cargo packages and source distributions prepared by CI already contain the agent. Rebuild the helper after changing its source or package version. Release CI always builds it from the same commit first. `GUAC_BRIDGE_AGENT` can select a prepared agent during development.

## Windows x86-64

With Rust’s MSVC toolchain, use the static Linux agent produced by the Linux build:

```powershell
python scripts/build-native.py --agent C:\build\guac-bridge-agent-linux-x86_64
cargo test -p guac-bridge
iscc installer\guac-bridge.iss
```

The installer is built with Inno Setup, installs per user without administrator privileges, and offers a PATH checkbox. All three client executables embed the Linux agent. A portable zip is also produced by CI. Native clients have no Python dependency.

## Python distribution

After preparing the helper asset:

```shell
python -m pip install maturin
python -m maturin build --release
python -m maturin sdist --out dist
```

Maturin `bin` bindings package the native executables, with a small optional `guac_bridge.run(arguments)` subprocess launcher and `python -m guac_bridge`. There is no PyO3 extension, Python ABI dependency or Python data path. Binary distributions need no Rust compiler on the client. Building from source requires Rust and the bundled agent asset, which is included in source distributions and Cargo packages prepared by release CI.

The workflow builds a static Linux helper, Windows executables and installer, Linux clients, and platform-specific Python distributions. A `v` tag, or a main-branch commit beginning with `Release v`, whose version matches both manifests publishes through PyPI Trusted Publishing and creates a GitHub release after checks pass. Branch builds create reviewable artifacts without publishing.
