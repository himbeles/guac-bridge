use std::{env, fs, path::PathBuf};
fn main() {
    println!("cargo:rerun-if-env-changed=GUAC_BRIDGE_AGENT");
    let bundled = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap())
        .join("assets/guac-bridge-agent-linux-x86_64");
    let source = env::var_os("GUAC_BRIDGE_AGENT")
        .map(PathBuf::from)
        .unwrap_or(bundled);
    println!("cargo:rerun-if-changed={}", source.display());
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("agent");
    // Agent-only builds deliberately do not require an already-built agent.
    if env::var_os("CARGO_FEATURE_CLIENT").is_some() {
        let bytes = fs::read(&source).expect("Build the Linux agent first with scripts/build-native.py, or set GUAC_BRIDGE_AGENT to a static Linux x86-64 agent");
        assert!(
            bytes.starts_with(b"\x7fELF")
                && bytes.get(4) == Some(&2)
                && bytes.get(18..20) == Some(&[62, 0]),
            "Bundled agent must be Linux x86-64 ELF"
        );
        fs::write(out, bytes).unwrap();
    }
}
