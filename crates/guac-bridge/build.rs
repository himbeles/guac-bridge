use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

fn build_agent(manifest: &Path, out: &Path) -> PathBuf {
    // cargo install --git works on Windows and Linux without Python or a Linux C compiler.
    // Use an independent target directory to avoid nesting Cargo's build-directory lock.
    let status=Command::new("rustup").args(["target","add","x86_64-unknown-linux-musl"])
        .status().expect("A source build without a bundled helper requires rustup; install the Linux x86-64 musl Rust target, or set GUAC_BRIDGE_AGENT");
    assert!(
        status.success(),
        "Cannot install the Linux musl Rust target; set GUAC_BRIDGE_AGENT to a prepared static Linux x86-64 helper"
    );
    let target = out.join("agent-build");
    let status = Command::new(env::var_os("CARGO").unwrap_or_else(|| "cargo".into()))
        .args([
            "build",
            "--offline",
            "--release",
            "--no-default-features",
            "--bin",
            "guac-bridge-agent",
            "--target",
            "x86_64-unknown-linux-musl",
            "--manifest-path",
        ])
        .arg(manifest.join("Cargo.toml"))
        .arg("--target-dir")
        .arg(&target)
        .args([
            "--config",
            "target.x86_64-unknown-linux-musl.linker=\"rust-lld\"",
        ])
        .env_remove("CARGO_FEATURE_CLIENT")
        .env_remove("CARGO_FEATURE_DEFAULT")
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env_remove("RUSTFLAGS")
        .status()
        .expect("Cannot compile bundled helper");
    assert!(
        status.success(),
        "Linux helper source build failed; set GUAC_BRIDGE_AGENT to a prepared static Linux x86-64 helper"
    );
    target.join("x86_64-unknown-linux-musl/release/guac-bridge-agent")
}
fn main() {
    println!("cargo:rerun-if-env-changed=GUAC_BRIDGE_AGENT");
    if env::var_os("CARGO_FEATURE_CLIENT").is_none() {
        return;
    }
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let override_path = env::var_os("GUAC_BRIDGE_AGENT").map(PathBuf::from);
    let bundled = manifest.join("assets/guac-bridge-agent-linux-x86_64");
    let source = if let Some(path) = override_path {
        path
    } else if bundled.exists() {
        bundled
    } else {
        for file in ["src/agent.rs", "src/lib.rs", "Cargo.toml"] {
            println!("cargo:rerun-if-changed={}", manifest.join(file).display());
        }
        build_agent(&manifest, &out)
    };
    println!("cargo:rerun-if-changed={}", source.display());
    let bytes = fs::read(&source).expect("Cannot read bundled Linux helper");
    assert!(
        bytes.starts_with(b"\x7fELF")
            && bytes.get(4) == Some(&2)
            && bytes.get(18..20) == Some(&[62, 0]),
        "Bundled agent must be Linux x86-64 ELF"
    );
    fs::write(out.join("agent"), bytes).unwrap();
}
