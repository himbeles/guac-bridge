//! Private-runtime staging and Inno Setup invocation. Windows-only FFI is isolated here.
use crate::{BuildOptions, project::Project};
use anyhow::Result;
#[cfg(not(windows))]
use anyhow::bail;

pub(crate) fn build(project: &Project, options: &BuildOptions) -> Result<()> {
    #[cfg(windows)]
    {
        windows::build(project, options)
    }
    #[cfg(not(windows))]
    {
        let _ = (project, options);
        bail!("The installer requires Windows MSVC and Inno Setup 6")
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use crate::{
        build,
        project::{copy_tree, run},
    };
    use anyhow::{Context, ensure};
    use std::{
        env,
        fs::{self, File},
        io::Write,
        path::{Path, PathBuf},
    };

    const PACKAGES: [&str; 2] = [
        "Microsoft.WindowsAppSDK.Runtime.2.4.0",
        "Microsoft.Web.WebView2.1.0.4078.44",
    ];
    const REQUIRED: [&str; 5] = [
        "Microsoft.WindowsAppRuntime.dll",
        "Microsoft.UI.Xaml.dll",
        "Microsoft.UI.Windowing.dll",
        "Microsoft.Web.WebView2.Core.dll",
        "resources.pri",
    ];

    pub(super) fn build(project: &Project, options: &BuildOptions) -> Result<()> {
        let binary = build::client(project, options, true)?;
        let directory = binary.parent().context("Binary has no parent")?;
        let payload = project.target.join("installer-payload");
        if payload.exists() {
            fs::remove_dir_all(&payload)?;
        }
        fs::create_dir_all(&payload)?;
        fs::copy(&binary, payload.join("guac-bridge.exe"))?;
        fs::copy(
            project.assets().join("THIRD-PARTY-NOTICES.txt"),
            payload.join("THIRD-PARTY-NOTICES.txt"),
        )?;
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if path.is_file() && path.extension().is_some_and(|e| e == "dll" || e == "pri") {
                fs::copy(&path, payload.join(&name))?;
            }
            if path.is_dir() && runtime_directory(&name) {
                copy_tree(&path, &payload.join(name))?;
            }
        }
        for name in REQUIRED {
            ensure!(
                payload.join(name).is_file(),
                "Missing bundled UI runtime file: {name}"
            );
        }
        let xml = manifest(&binary)?;
        let document = roxmltree::Document::parse(&xml)?;
        let description = document
            .descendants()
            .find(|n| n.has_tag_name("description"))
            .and_then(|n| n.text());
        let classes = document
            .descendants()
            .filter(|n| n.has_tag_name("activatableClass"))
            .count();
        ensure!(
            description == Some("windows-reactor-self-contained") && classes > 0,
            "The executable lost its private WinUI activation manifest"
        );
        fs::write(project.target.join("installer-manifest.xml"), &xml)?;
        println!("Embedded runtime activation classes: {classes}");
        let cache = PathBuf::from(env::var_os("LOCALAPPDATA").context("LOCALAPPDATA is missing")?)
            .join("windows-reactor-setup/temp");
        for package in PACKAGES {
            let mut archive =
                zip::ZipArchive::new(File::open(cache.join(format!("{package}.nupkg")))?)?;
            for (kind, bytes) in redistribution_notices(&mut archive)? {
                fs::write(payload.join(format!("{package}-{kind}.txt")), bytes)?;
            }
        }
        let iscc = env::var_os("ISCC").map(PathBuf::from).unwrap_or_else(|| {
            PathBuf::from(
                env::var_os("ProgramFiles(x86)").unwrap_or_else(|| "C:/Program Files (x86)".into()),
            )
            .join("Inno Setup 6/ISCC.exe")
        });
        ensure!(
            iscc.is_file(),
            "Install Inno Setup 6 or set ISCC to ISCC.exe"
        );
        run(project
            .command(iscc)
            .arg(format!("/DVersion={}", project.version))
            .arg(format!(
                "/DNumericVersion={}",
                project.version.split('-').next().unwrap()
            ))
            .arg(format!("/DPayloadDir={}", payload.display()))
            .arg("installer/guac-bridge.iss"))?;
        let setup = project.dist()?.join(format!(
            "guac-bridge-{}-windows-x86_64-setup.exe",
            project.version
        ));
        let size = fs::metadata(setup)?.len();
        let installed = tree_size(&payload)?;
        let baseline = 10_349_518_u64;
        let report = serde_json::json!({"version":project.version, "installer_bytes":size,"previous_installer_bytes":baseline,"increase_bytes":size as i64-baseline as i64,"installed_payload_bytes":installed,"runtime":"private Windows App Runtime 2.4.0 x64"});
        fs::write(
            project.dist()?.join("installer-size.json"),
            serde_json::to_vec_pretty(&report)?,
        )?;
        let summary = format!(
            "Installer: {:.2} MB. Installed payload: {:.2} MB.\n",
            size as f64 / 1e6,
            installed as f64 / 1e6
        );
        print!("{summary}");
        if let Some(path) = env::var_os("GITHUB_STEP_SUMMARY") {
            fs::OpenOptions::new()
                .append(true)
                .create(true)
                .open(path)?
                .write_all(summary.as_bytes())?;
        }
        Ok(())
    }

    fn runtime_directory(name: &str) -> bool {
        if name == "microsoft.ui.xaml" {
            return true;
        }
        let mut parts = name.split('-');
        let language = parts.next().unwrap_or_default();
        let rest: Vec<_> = parts.collect();
        (2..=3).contains(&language.len())
            && language.chars().all(|c| c.is_ascii_lowercase())
            && !rest.is_empty()
            && rest.iter().all(|s| {
                !s.is_empty()
                    && s.chars()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            })
    }
    fn tree_size(path: &Path) -> Result<u64> {
        let mut size = 0;
        for entry in fs::read_dir(path)? {
            let path = entry?.path();
            size += if path.is_dir() {
                tree_size(&path)?
            } else {
                fs::metadata(path)?.len()
            };
        }
        Ok(size)
    }

    fn manifest(path: &Path) -> Result<String> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::{Foundation::FreeLibrary, System::LibraryLoader::*};
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        // Load as data only: inspecting the private activation manifest must not activate the UI.
        unsafe {
            let module = LoadLibraryExW(
                wide.as_ptr(),
                std::ptr::null_mut(),
                LOAD_LIBRARY_AS_DATAFILE,
            );
            ensure!(!module.is_null(), "Cannot inspect executable manifest");
            struct Module(windows_sys::Win32::Foundation::HMODULE);
            impl Drop for Module {
                fn drop(&mut self) {
                    unsafe {
                        FreeLibrary(self.0);
                    }
                }
            }
            let module = Module(module);
            let resource = FindResourceW(
                module.0,
                std::ptr::without_provenance(1),
                std::ptr::without_provenance(24),
            );
            ensure!(!resource.is_null(), "Missing embedded application manifest");
            let size = SizeofResource(module.0, resource) as usize;
            let data = LockResource(LoadResource(module.0, resource));
            ensure!(
                !data.is_null() && size > 0,
                "Cannot load application manifest"
            );
            let xml =
                String::from_utf8(std::slice::from_raw_parts(data.cast::<u8>(), size).to_vec())?;
            Ok(xml
                .trim_end_matches('\0')
                .trim_start_matches('\u{feff}')
                .to_owned())
        }
    }
}

#[cfg(any(windows, test))]
// Both pinned NuGet packages carry their redistribution terms at the archive root.
fn redistribution_notices<R: std::io::Read + std::io::Seek>(
    archive: &mut zip::ZipArchive<R>,
) -> Result<Vec<(&'static str, Vec<u8>)>> {
    use anyhow::Context;
    use std::io::Read;
    let mut files = Vec::new();
    for kind in ["LICENSE", "NOTICE"] {
        let mut found = None;
        for index in 0..archive.len() {
            let mut entry = archive.by_index(index)?;
            let matches = {
                let name = entry.name()?;
                [kind.to_owned(), format!("{kind}.txt"), format!("{kind}.md")]
                    .iter()
                    .any(|candidate| name.eq_ignore_ascii_case(candidate))
            };
            if matches {
                let mut bytes = Vec::new();
                entry.read_to_end(&mut bytes)?;
                found = Some(bytes);
                break;
            }
        }
        files.push((
            kind,
            found.with_context(|| format!("Missing Microsoft redistribution {kind}"))?,
        ));
    }
    Ok(files)
}

#[cfg(test)]
mod license_tests {
    use super::*;
    use std::io::{Cursor, Write};
    fn package(notice: bool) -> zip::ZipArchive<Cursor<Vec<u8>>> {
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        archive
            .start_file("license.txt", zip::write::SimpleFileOptions::default())
            .unwrap();
        archive
            .write_all(b"Microsoft redistribution terms")
            .unwrap();
        if notice {
            archive
                .start_file("NOTICE.txt", zip::write::SimpleFileOptions::default())
                .unwrap();
            archive.write_all(b"Third-party copyrights").unwrap();
        }
        zip::ZipArchive::new(archive.finish().unwrap()).unwrap()
    }
    #[test]
    fn microsoft_packages_require_both_terms_and_notices() {
        let files = redistribution_notices(&mut package(true)).unwrap();
        assert_eq!(files[1], ("NOTICE", b"Third-party copyrights".to_vec()));
        assert!(redistribution_notices(&mut package(false)).is_err());
    }
}
