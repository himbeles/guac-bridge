//! Release preparation is local and inspectable. CI alone holds publication credentials.
use crate::project::Project;
use anyhow::{Context, Result, ensure};
use sha2::{Digest, Sha256};
use std::{fs, io::Read};

pub(crate) fn verify(project: &Project, tag: Option<&str>) -> Result<()> {
    let python: toml::Value =
        toml::from_str(&fs::read_to_string(project.root.join("pyproject.toml"))?)?;
    ensure!(
        python["project"].get("version").is_none(),
        "Python version must come from Cargo, not a second version field"
    );
    ensure!(
        python["project"]["dynamic"]
            .as_array()
            .is_some_and(|v| v.iter().any(|s| s.as_str() == Some("version"))),
        "maturin must use Cargo's version"
    );
    if let Some(tag) = tag {
        ensure!(
            tag == "main" || tag == format!("v{}", project.version),
            "Release tag does not match Cargo workspace version"
        );
    }
    println!("Release version: {}", project.version);
    Ok(())
}
pub(crate) fn notes(project: &Project) -> Result<()> {
    verify(project, std::env::var("RELEASE_TAG").ok().as_deref())?;
    let changelog = fs::read_to_string(project.root.join("CHANGELOG.md"))?;
    let heading = format!("## {}\n", project.version);
    let section = changelog
        .split_once(&heading)
        .context("No changelog section for this version")?
        .1
        .split("\n## ")
        .next()
        .unwrap()
        .trim();
    fs::write(
        project.root.join("release-notes.md"),
        format!("{section}\n"),
    )?;
    let mut files: Vec<_> = fs::read_dir(project.dist()?)?.collect::<std::io::Result<_>>()?;
    files.sort_by_key(|entry| entry.file_name());
    let mut checksums = String::new();
    for entry in files {
        if entry.file_name() == "SHA256SUMS" || !entry.file_type()?.is_file() {
            continue;
        }
        let mut file = fs::File::open(entry.path())?;
        let mut hash = Sha256::new();
        let mut buffer = [0; 65536];
        loop {
            let read = file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            hash.update(&buffer[..read]);
        }
        checksums.push_str(&format!(
            "{:x}  {}\n",
            hash.finalize(),
            entry.file_name().to_string_lossy()
        ));
    }
    ensure!(!checksums.is_empty(), "No release artifacts in dist/");
    fs::write(project.dist()?.join("SHA256SUMS"), checksums)?;
    if let Some(path) = std::env::var_os("GITHUB_ENV") {
        use std::io::Write;
        writeln!(
            fs::OpenOptions::new().append(true).open(path)?,
            "RELEASE_TAG=v{}",
            project.version
        )?;
    }
    Ok(())
}
