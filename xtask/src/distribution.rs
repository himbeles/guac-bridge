//! Portable archives contain exactly one CLI, the README and applicable license notices.
//! Wheel extraction preserves the manylinux binary instead of rebuilding on the host.
use crate::project::Project;
use anyhow::{Context, Result, bail, ensure};
use flate2::{Compression, write::GzEncoder};
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::Path,
};
use zip::{ZipArchive, ZipWriter, write::SimpleFileOptions};

// Maturin normalizes Cargo prereleases to PEP 440 in distribution filenames.
fn wheel_version(version: &str) -> Result<String> {
    let Some((base, prerelease)) = version.split_once('-') else {
        return Ok(version.into());
    };
    let (phase, number) = prerelease
        .split_once('.')
        .context("Use alpha.N, beta.N or rc.N for Python prereleases")?;
    let phase = match phase {
        "alpha" => "a",
        "beta" => "b",
        "rc" => "rc",
        _ => bail!("Unsupported Python prerelease phase"),
    };
    let number: u64 = number.parse().context("Invalid prerelease number")?;
    Ok(format!("{base}{phase}{number}"))
}

fn archive_suffix(wheel: Option<&str>) -> Result<&'static str> {
    if let Some(name) = wheel {
        return match name {
            n if n.ends_with("-win_amd64.whl") => Ok("windows-x86_64.zip"),
            n if n.contains("-manylinux") && n.ends_with("_x86_64.whl") => {
                Ok("linux-x86_64.tar.gz")
            }
            n if n.contains("-macosx_") && n.ends_with("_arm64.whl") => Ok("macos-aarch64.tar.gz"),
            n if n.contains("-macosx_") && n.ends_with("_x86_64.whl") => Ok("macos-x86_64.tar.gz"),
            _ => bail!("Unsupported wheel platform: {name}"),
        };
    }
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => Ok("windows-x86_64.zip"),
        ("linux", "x86_64") => Ok("linux-x86_64.tar.gz"),
        ("macos", "aarch64") => Ok("macos-aarch64.tar.gz"),
        _ => bail!("Unsupported native archive platform"),
    }
}

pub(crate) fn portable(
    project: &Project,
    binary: Option<&Path>,
    wheel: Option<&Path>,
) -> Result<()> {
    let wheel_name = wheel.map(|p| p.to_string_lossy());
    let suffix = archive_suffix(wheel_name.as_deref())?;
    let windows = suffix.ends_with(".zip");
    let executable = if windows {
        "guac-bridge.exe"
    } else {
        "guac-bridge"
    };
    let bytes = match wheel {
        Some(path) => {
            ensure!(
                path.file_name()
                    .context("Missing wheel filename")?
                    .to_string_lossy()
                    .starts_with(&format!(
                        "guac_bridge-{}-",
                        wheel_version(&project.version)?
                    )),
                "Wheel version must match Cargo workspace version"
            );
            let mut archive = ZipArchive::new(File::open(path)?)?;
            wheel_binary(&mut archive, windows)?
        }
        None => fs::read(binary.context("Missing native binary")?)?,
    };
    let files = [
        (executable, bytes),
        ("README.md", fs::read(project.root.join("README.md"))?),
        ("LICENSE", fs::read(project.root.join("LICENSE"))?),
        (
            "THIRD-PARTY-NOTICES.txt",
            fs::read(project.assets().join("THIRD-PARTY-NOTICES.txt"))?,
        ),
    ];
    let output = project
        .dist()?
        .join(format!("guac-bridge-{}-{suffix}", project.version));
    if windows {
        let mut archive = ZipWriter::new(File::create(&output)?);
        for (name, bytes) in files {
            archive.start_file(
                name,
                SimpleFileOptions::default()
                    .compression_method(zip::CompressionMethod::Deflated)
                    .unix_permissions(if name == executable { 0o755 } else { 0o644 }),
            )?;
            archive.write_all(&bytes)?;
        }
        archive.finish()?;
    } else {
        let mut archive = tar::Builder::new(GzEncoder::new(
            File::create(&output)?,
            Compression::default(),
        ));
        for (name, bytes) in files {
            let mut header = tar::Header::new_gnu();
            header.set_size(bytes.len() as u64);
            header.set_mode(if name == executable { 0o755 } else { 0o644 });
            header.set_cksum();
            archive.append_data(&mut header, name, bytes.as_slice())?;
        }
        archive.into_inner()?.finish()?;
    }
    println!("Created {}", output.display());
    Ok(())
}

fn wheel_binary<R: Read + std::io::Seek>(
    archive: &mut ZipArchive<R>,
    windows: bool,
) -> Result<Vec<u8>> {
    let executable = if windows {
        "guac-bridge.exe"
    } else {
        "guac-bridge"
    };
    let suffix = format!("/scripts/{executable}");
    let mut matches = Vec::new();
    for index in 0..archive.len() {
        let entry = archive.by_index(index)?;
        let name = entry.name().to_ascii_lowercase();
        ensure!(
            !windows
                || ![".dll", ".msix", ".msixbundle"]
                    .iter()
                    .any(|extension| name.ends_with(extension)),
            "Portable wheels must use the shared UI runtime"
        );
        if entry.name().ends_with(&suffix) {
            matches.push(index);
        }
    }
    ensure!(
        matches.len() == 1,
        "Missing or duplicate native binary: {executable}"
    );
    let mut bytes = Vec::new();
    archive.by_index(matches[0])?.read_to_end(&mut bytes)?;
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn wheel(files: &[(&str, &[u8])]) -> ZipArchive<Cursor<Vec<u8>>> {
        let mut writer = ZipWriter::new(Cursor::new(Vec::new()));
        for (name, data) in files {
            writer
                .start_file(*name, SimpleFileOptions::default())
                .unwrap();
            writer.write_all(data).unwrap();
        }
        ZipArchive::new(writer.finish().unwrap()).unwrap()
    }
    #[test]
    fn archive_platform_follows_wheel_instead_of_build_host() {
        for (tag, suffix) in [
            ("win_amd64", "windows-x86_64.zip"),
            ("manylinux_2_28_x86_64", "linux-x86_64.tar.gz"),
            ("macosx_11_0_arm64", "macos-aarch64.tar.gz"),
        ] {
            assert_eq!(
                archive_suffix(Some(&format!("guac_bridge-5.0.2-py3-none-{tag}.whl"))).unwrap(),
                suffix
            );
        }
        assert!(archive_suffix(Some("guac_bridge-5.0.2-py3-none-any.whl")).is_err());
        assert!(
            archive_suffix(Some(
                "guac_bridge-5.0.2-py3-none-macosx_11_0_universal2.whl"
            ))
            .is_err()
        );
    }
    #[test]
    fn wheel_versions_match_maturin_prerelease_normalization() {
        for (cargo, python) in [
            ("4.0.2", "4.0.2"),
            ("5.0.0-beta.1", "5.0.0b1"),
            ("5.0.0-alpha.2", "5.0.0a2"),
            ("5.0.0-rc.3", "5.0.0rc3"),
        ] {
            assert_eq!(wheel_version(cargo).unwrap(), python);
        }
        assert!(wheel_version("5.0.0-preview.1").is_err());
        assert!(wheel_version("5.0.0-beta.invalid").is_err());
    }
    #[test]
    fn wheel_extraction_preserves_binary_and_rejects_ambiguous_or_bundled_payloads() {
        let binary = b"\0\xffunchanged executable\r\n";
        assert_eq!(
            wheel_binary(
                &mut wheel(&[("pkg.data/scripts/guac-bridge", binary)]),
                false
            )
            .unwrap(),
            binary
        );
        for files in [
            vec![("other/file", binary.as_slice())],
            vec![
                ("a/scripts/guac-bridge", binary.as_slice()),
                ("b/scripts/guac-bridge", binary.as_slice()),
            ],
        ] {
            assert!(wheel_binary(&mut wheel(&files), false).is_err());
        }
        assert!(
            wheel_binary(
                &mut wheel(&[
                    ("pkg.data/scripts/guac-bridge.exe", binary),
                    ("pkg/runtime.DLL", b"runtime")
                ]),
                true
            )
            .is_err()
        );
    }
}
