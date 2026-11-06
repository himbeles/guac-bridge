//! License texts accompany client distributions, covering their bundled helpers too.
use std::io::Read;

pub fn text() -> String {
    let mut text = include_str!("../assets/LICENSE").to_owned();
    text.push_str("\n\n");
    let mut decoder = flate2::read::GzDecoder::new(
        include_bytes!("../assets/THIRD-PARTY-NOTICES.txt.gz").as_slice(),
    );
    decoder
        .read_to_string(&mut text)
        .expect("Bundled license report must be valid UTF-8 gzip");
    // The Windows installer carries Microsoft's redistributable terms separately.
    #[cfg(windows)]
    if let Ok(executable) = std::env::current_exe()
        && let Some(parent) = executable.parent()
        && let Ok(entries) = std::fs::read_dir(parent)
    {
        let mut paths: Vec<_> = entries
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| {
                        name.starts_with("Microsoft.")
                            && (name.ends_with("-LICENSE.txt") || name.ends_with("-NOTICE.txt"))
                    })
            })
            .collect();
        paths.sort();
        for path in paths {
            if let Ok(license) = std::fs::read_to_string(&path) {
                text.push_str(&format!("\n\n=== {} ===\n{license}", path.display()));
            }
        }
    }
    text
}

#[cfg(all(windows, feature = "client"))]
pub(crate) fn open() -> anyhow::Result<()> {
    let path = crate::background::directory()?.join("licenses.txt");
    std::fs::write(&path, text())?;
    std::process::Command::new("notepad.exe")
        .arg(path)
        .spawn()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn standalone_notice_includes_project_native_and_rust_terms() {
        let text = super::text();
        for required in [
            "Copyright (c) 2026 Luis Riegger",
            "Permission is hereby granted",
            "THIRD-PARTY SOFTWARE NOTICES",
            "Academic Free License",
            "RUST STANDARD LIBRARY AND BUNDLED COMPONENTS",
            "MPL-2.0",
            "https://crates.io/api/v1/crates/",
        ] {
            assert!(
                text.contains(required),
                "Missing license information: {required}"
            );
        }
    }
}
