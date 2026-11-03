//! Paths and checked process execution shared by the repository tasks.
use anyhow::{Context, Result, ensure};
use std::{
    env, fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

pub(crate) struct Project {
    pub root: PathBuf,
    pub target: PathBuf,
    pub version: String,
}

impl Project {
    pub fn load() -> Result<Self> {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .context("xtask must live in the workspace")?
            .to_owned();
        let manifest: toml::Value = toml::from_str(&fs::read_to_string(root.join("Cargo.toml"))?)?;
        let version = manifest["workspace"]["package"]["version"]
            .as_str()
            .context("Missing workspace version")?
            .to_owned();
        let output = Command::new(cargo())
            .current_dir(&root)
            .args(["metadata", "--locked", "--no-deps", "--format-version=1"])
            .output()?;
        ensure!(
            output.status.success(),
            "cargo metadata failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let metadata: serde_json::Value = serde_json::from_slice(&output.stdout)?;
        let target = metadata["target_directory"]
            .as_str()
            .context("Missing Cargo target directory")?
            .into();
        Ok(Self {
            root,
            target,
            version,
        })
    }
    pub fn command(&self, program: impl AsRef<std::ffi::OsStr>) -> Command {
        let mut command = Command::new(program);
        command.current_dir(&self.root);
        command
    }
    pub fn cargo(&self, args: &[&str]) -> Result<()> {
        run(self.command(cargo()).args(args))
    }
    pub fn python(&self, args: &[&str]) -> Result<()> {
        run(self
            .command(env::var_os("PYTHON").unwrap_or_else(|| "python".into()))
            .args(args))
    }
    /// Read Cargo's artifact path rather than guessing target/profile/global-config paths.
    pub fn artifact(&self, args: &[&str], binary: &str) -> Result<PathBuf> {
        let mut command = self.command(cargo());
        command
            .args(args)
            .arg("--message-format=json-render-diagnostics")
            .stdout(Stdio::piped());
        eprintln!("Running {command:?}");
        let mut child = command.spawn().context("Cannot start Cargo")?;
        let mut found = None;
        for line in BufReader::new(child.stdout.take().context("Missing Cargo output")?).lines() {
            let line = line?;
            let Ok(value) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            if value["reason"] == "compiler-artifact"
                && value["target"]["name"] == binary
                && let Some(path) = value["executable"].as_str()
            {
                found = Some(PathBuf::from(path));
            }
        }
        ensure!(child.wait()?.success(), "Cargo failed building {binary}");
        found.with_context(|| format!("Cargo did not produce {binary}"))
    }
    pub fn assets(&self) -> PathBuf {
        self.root.join("crates/guac-bridge/assets")
    }
    pub fn dist(&self) -> Result<PathBuf> {
        let path = self.root.join("dist");
        fs::create_dir_all(&path)?;
        Ok(path)
    }
}

fn cargo() -> std::ffi::OsString {
    env::var_os("CARGO").unwrap_or_else(|| "cargo".into())
}
pub(crate) fn run(command: &mut Command) -> Result<()> {
    eprintln!("Running {command:?}");
    let status = command
        .status()
        .with_context(|| format!("Cannot start {:?}", command.get_program()))?;
    ensure!(
        status.success(),
        "{:?} failed with {status}",
        command.get_program()
    );
    Ok(())
}

#[cfg(windows)]
pub(crate) fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        ensure!(
            !kind.is_symlink(),
            "Unexpected symlink in build payload: {}",
            entry.path().display()
        );
        if kind.is_dir() {
            copy_tree(&entry.path(), &destination.join(entry.file_name()))?;
        } else {
            fs::copy(entry.path(), destination.join(entry.file_name()))?;
        }
    }
    Ok(())
}
