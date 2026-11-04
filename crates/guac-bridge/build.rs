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
            "--locked",
            "--release",
            "--no-default-features",
            "--features",
            "agent",
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
fn build_windows_agent(manifest: &Path, out: &Path) -> PathBuf {
    let target = out.join("windows-agent-build");
    let status = Command::new(env::var_os("CARGO").unwrap_or_else(|| "cargo".into()))
        .args([
            "build",
            "--locked",
            "--release",
            "--no-default-features",
            "--features",
            "agent",
            "--bin",
            "guac-bridge-agent",
            "--target",
            "x86_64-pc-windows-msvc",
            "--manifest-path",
        ])
        .arg(manifest.join("Cargo.toml"))
        .arg("--target-dir")
        .arg(&target)
        .env_remove("CARGO_FEATURE_CLIENT")
        .env_remove("CARGO_FEATURE_DEFAULT")
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .env("RUSTFLAGS", "-C target-feature=+crt-static")
        .status()
        .expect("Cannot compile Windows helper");
    assert!(
        status.success(),
        "Windows helper source build failed; set GUAC_BRIDGE_WINDOWS_AGENT to a prepared x86-64 helper"
    );
    target.join("x86_64-pc-windows-msvc/release/guac-bridge-agent.exe")
}
fn main() {
    println!("cargo:rerun-if-env-changed=GUAC_BRIDGE_AGENT");
    println!("cargo:rerun-if-env-changed=GUAC_BRIDGE_WINDOWS_AGENT");
    println!("cargo:rerun-if-env-changed=GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT");
    if env::var_os("CARGO_FEATURE_CLIENT").is_none() {
        return;
    }
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    println!("cargo:rerun-if-changed=src");
    #[cfg(windows)]
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        // Reactor's framework bootstrap uses a Windows 11 API. Delay importing it so
        // command-line bridges and the tray still start on older Windows systems.
        if env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
            println!("cargo:rustc-link-lib=delayimp");
            println!("cargo:rustc-link-arg=/DELAYLOAD:api-ms-win-appmodel-runtime-l1-1-5.dll");
        }
        println!("cargo:rerun-if-changed=assets/frog.ico");
        println!("cargo:rerun-if-changed=assets/app.manifest");
        #[cfg(feature = "bundled-ui")]
        {
            windows_reactor_setup::as_self_contained();
            println!(
                "cargo:rustc-link-arg-bins=/MANIFESTINPUT:{}",
                manifest.join("assets/app.manifest").display()
            );
        }
        let mut resource = winresource::WindowsResource::new();
        resource.set_icon("assets/frog.ico");
        #[cfg(not(feature = "bundled-ui"))]
        resource.set_manifest_file("assets/app.manifest");
        resource
            .set("FileDescription", "Guac Bridge")
            .set("ProductName", "Guac Bridge")
            .compile()
            .expect("Cannot embed Windows icon, version and visual styles");
    }
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
    let mut windows = env::var_os("GUAC_BRIDGE_WINDOWS_AGENT")
        .map(PathBuf::from)
        .unwrap_or_else(|| manifest.join("assets/guac-bridge-agent-windows-x86_64.exe"));
    if !windows.exists() && cfg!(windows) {
        windows = build_windows_agent(&manifest, &out);
    }
    println!("cargo:rerun-if-changed={}", windows.display());
    let bytes = if windows.exists() {
        fs::read(windows).unwrap()
    } else {
        Vec::new()
    };
    if bytes.is_empty() {
        assert!(
            env::var_os("GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT").is_none(),
            "Release clients must bundle the Windows agent"
        );
    } else {
        let offset = u32::from_le_bytes(
            bytes
                .get(0x3c..0x40)
                .expect("Invalid Windows helper")
                .try_into()
                .unwrap(),
        ) as usize;
        assert!(
            bytes.starts_with(b"MZ")
                && bytes.get(offset..offset + 6) == Some(&[b'P', b'E', 0, 0, 0x64, 0x86]),
            "Windows helper must be an x86-64 PE executable"
        );
    }
    fs::write(out.join("agent-windows"), bytes).unwrap();
}
