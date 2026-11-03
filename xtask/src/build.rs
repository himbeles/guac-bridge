//! Build ordering: matching helpers → embedded client. No nested shell scripts.
use crate::{
    BuildOptions, Suite,
    project::{Project, run},
};
use anyhow::{Result, ensure};
use clap::ValueEnum;
use std::{fs, path::PathBuf};

#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum ServerOs {
    Linux,
    Windows,
}
pub(crate) const LINUX_HELPER: &str = "guac-bridge-agent-linux-x86_64";
pub(crate) const WINDOWS_HELPER: &str = "guac-bridge-agent-windows-x86_64.exe";

pub(crate) fn helper(project: &Project, os: ServerOs) -> Result<PathBuf> {
    let (target, name) = match os {
        ServerOs::Linux => ("x86_64-unknown-linux-musl", LINUX_HELPER),
        ServerOs::Windows => {
            ensure!(
                cfg!(windows),
                "Build the Windows MSVC helper on Windows; on Linux pass --windows-agent PATH"
            );
            ("x86_64-pc-windows-msvc", WINDOWS_HELPER)
        }
    };
    run(project.command("rustup").args(["target", "add", target]))?;
    let mut args = vec![
        "build",
        "--locked",
        "--release",
        "-p",
        "guac-bridge",
        "--no-default-features",
        "--features",
        "agent",
        "--bin",
        "guac-bridge-agent",
        "--target",
        target,
    ];
    if matches!(os, ServerOs::Linux) {
        args.extend([
            "--config",
            "target.x86_64-unknown-linux-musl.linker=\"rust-lld\"",
        ]);
    }
    let binary = project.artifact(&args, "guac-bridge-agent")?;
    let staged = project.assets().join(name);
    fs::create_dir_all(project.assets())?;
    fs::copy(binary, &staged)?;
    // Windows ConPTY tests need the standalone agent beside the host client.
    if matches!(os, ServerOs::Windows) {
        fs::create_dir_all(project.target.join("release"))?;
        fs::copy(
            &staged,
            project.target.join("release/guac-bridge-agent.exe"),
        )?;
    }
    Ok(staged)
}

pub(crate) fn prepare(project: &Project, options: &BuildOptions) -> Result<()> {
    if !options.prepared {
        helper(project, ServerOs::Linux)?;
        if let Some(path) = &options.windows_agent {
            fs::copy(path, project.assets().join(WINDOWS_HELPER))?;
        } else if cfg!(windows) {
            helper(project, ServerOs::Windows)?;
        }
    }
    ensure!(
        project.assets().join(LINUX_HELPER).is_file(),
        "Missing Linux helper; run cargo xtask helper linux"
    );
    if cfg!(windows) || std::env::var_os("GUAC_BRIDGE_REQUIRE_WINDOWS_AGENT").is_some() {
        ensure!(
            project.assets().join(WINDOWS_HELPER).is_file(),
            "Missing Windows helper; run cargo xtask helper windows or pass --windows-agent PATH"
        );
    }
    Ok(())
}

pub(crate) fn client(project: &Project, options: &BuildOptions, bundled: bool) -> Result<PathBuf> {
    prepare(project, options)?;
    let mut args = vec![
        "build",
        "--locked",
        "--release",
        "-p",
        "guac-bridge",
        "--bin",
        "guac-bridge",
    ];
    if bundled {
        args.extend(["--features", "bundled-ui"]);
    } else {
        args.push("--examples");
    }
    let binary = project.artifact(&args, "guac-bridge")?;
    if cfg!(windows) && !bundled {
        fs::copy(
            project.assets().join(WINDOWS_HELPER),
            binary.parent().unwrap().join("guac-bridge-agent.exe"),
        )?;
    }
    Ok(binary)
}

pub(crate) fn integration(project: &Project, suite: Suite) -> Result<()> {
    if matches!(suite, Suite::All | Suite::Native) {
        project.python(&[
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/native",
            "-p",
            if cfg!(windows) {
                "test_windows.py"
            } else {
                "test_native.py"
            },
            "-v",
        ])?;
    }
    if matches!(suite, Suite::All | Suite::Console) && cfg!(windows) {
        project.python(&["tests/windows/probe.py"])?;
        project.python(&[
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/windows",
            "-p",
            "test_*.py",
            "-v",
        ])?;
        let python = std::env::var_os("PYTHON").unwrap_or_else(|| "python".into());
        run(project
            .command(python)
            .env("GUAC_TEST_SHELL", "cmd.exe")
            .args([
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests/windows",
                "-p",
                "test_bootstrap.py",
                "-v",
            ]))?;
    }
    if matches!(suite, Suite::Console) {
        ensure!(cfg!(windows), "Console integration requires Windows");
    }
    Ok(())
}
