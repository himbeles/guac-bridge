//! Portable archives contain exactly one CLI, the README and the license.
//! Wheel extraction preserves the manylinux binary instead of rebuilding on the host.
use crate::project::Project;
use anyhow::{Context, Result, ensure};
use flate2::{Compression, write::GzEncoder};
use std::{
    fs::{self, File},
    io::{Read, Write},
    path::Path,
};
use zip::{ZipArchive, ZipWriter, write::SimpleFileOptions};

pub(crate) fn portable(
    project: &Project,
    binary: Option<&Path>,
    wheel: Option<&Path>,
) -> Result<()> {
    let windows = wheel
        .map(|p| p.to_string_lossy().contains("win_amd64"))
        .unwrap_or(cfg!(windows));
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
                    .starts_with(&format!("guac_bridge-{}-", project.version)),
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
    ];
    let suffix = if windows {
        "windows-x86_64.zip"
    } else {
        "linux-x86_64.tar.gz"
    };
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
