//! Shutdown before replacement. A Windows uv updater runs outside the installation
//! so neither its own image nor the tray can keep installed binaries locked.
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use std::{
    fs::{File, OpenOptions},
    path::{Path, PathBuf},
    process::Command,
};

fn gate() -> Result<File> {
    Ok(OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(crate::background::directory()?.join("update.lock"))?)
}
pub(crate) fn startup_guard() -> Result<File> {
    let file = gate()?;
    FileExt::try_lock_shared(&file)
        .context("An update is in progress; wait until it finishes before starting a bridge")?;
    Ok(file)
}
fn update_guard() -> Result<File> {
    let file = gate()?;
    file.try_lock_exclusive().context(
        "A bridge is still starting or another update is running; retry when it finishes",
    )?;
    Ok(file)
}
pub(crate) fn prepare() -> Result<()> {
    let _guard = update_guard()?;
    crate::background::stop_all()?;
    crate::console::line("Bridges and tray stopped. Ready to update.");
    Ok(())
}
fn tool_directory(uv: &Path, bin: bool) -> Result<PathBuf> {
    let mut command = Command::new(uv);
    command.args(["tool", "dir"]);
    if bin {
        command.arg("--bin");
    }
    let output = command
        .output()
        .context("Cannot query uv tool installation")?;
    ensure!(
        output.status.success(),
        "uv tool dir failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(PathBuf::from(String::from_utf8(output.stdout)?.trim()))
}
fn uv_installation() -> Result<PathBuf> {
    let uv = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|path| path.join(if cfg!(windows) { "uv.exe" } else { "uv" }))
        .find(|path| path.is_file())
        .context("uv is not on PATH")?;
    let root = tool_directory(&uv, false)?.join("guac-bridge");
    ensure!(
        root.join("uv-receipt.toml").is_file(),
        "This is not a uv tool installation. Use the Windows installer to update an installer-based installation."
    );
    let exe = std::env::current_exe()?.canonicalize()?;
    let root = root.canonicalize()?;
    let mut installed = exe.starts_with(&root);
    if !installed {
        let bin = tool_directory(&uv, true)?.canonicalize()?;
        let source = root.join(if cfg!(windows) {
            "Scripts/guac-bridge.exe"
        } else {
            "bin/guac-bridge"
        });
        installed = exe.parent() == Some(bin.as_path())
            && source.is_file()
            && std::fs::read(&exe)? == std::fs::read(source)?;
    }
    ensure!(
        installed,
        "This executable does not belong to the guac-bridge uv tool installation; use its original installer"
    );
    Ok(uv)
}
pub(crate) fn start() -> Result<()> {
    let uv = uv_installation()?; // Validate before interrupting any connections.
    crate::console::line(
        "Updating will stop all running SSH/LLM bridges and disconnect their active sessions.",
    );
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let directory = std::env::temp_dir().join("guac-bridge-updaters");
        std::fs::create_dir_all(&directory)?;
        // Remove previous completed helpers; an in-use file simply stays for next time.
        for entry in std::fs::read_dir(&directory)? {
            let path = entry?.path();
            if path
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with("guac-update-"))
            {
                let _ = std::fs::remove_file(path);
            }
        }
        let helper = directory.join(format!("guac-update-{}.exe", uuid::Uuid::new_v4()));
        std::fs::copy(std::env::current_exe()?, &helper)?;
        Command::new(helper)
            .arg("internal-update")
            .arg(std::process::id().to_string())
            .arg(uv)
            .creation_flags(0x00000010)
            .spawn()
            .context("Cannot start the update window")?;
        crate::console::line(
            "Update opened in a separate window; that window will report success or failure.",
        );
        Ok(())
    }
    #[cfg(not(windows))]
    {
        upgrade(&uv)
    }
}
fn upgrade(uv: &Path) -> Result<()> {
    let _guard = update_guard()?;
    crate::background::stop_all()?;
    let status = Command::new(uv)
        .args(["tool", "upgrade", "guac-bridge"])
        .status()
        .context("Cannot run uv upgrade")?;
    ensure!(
        status.success(),
        "uv upgrade failed ({status}); bridges remain stopped. Retry after resolving the uv error."
    );
    crate::console::line("Guac Bridge updated. Start bridges again with your usual command.");
    Ok(())
}
pub(crate) fn worker(parent: u32, uv: &Path) -> Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::io::{AsRawHandle, FromRawHandle};
        use windows_sys::Win32::{Foundation::*, System::Threading::*, UI::WindowsAndMessaging::*};
        let result = (|| -> Result<()> {
            crate::background::visible_console()?;
            let process = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, parent) };
            if !process.is_null() {
                let process =
                    unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(process) };
                ensure!(
                    unsafe { WaitForSingleObject(process.as_raw_handle(), 30000) } == WAIT_OBJECT_0,
                    "The original launcher did not exit; update cancelled"
                );
            } else {
                ensure!(
                    unsafe { GetLastError() } == ERROR_INVALID_PARAMETER,
                    "Cannot wait for the original launcher to exit; update cancelled"
                );
            }
            upgrade(uv)
        })();
        let message = match &result {
            Ok(()) => "Update completed. Start bridges again with your usual command.".into(),
            Err(error) => format!(
                "Update failed: {error:#}\n\nBridges may have been stopped. Resolve the error and retry."
            ),
        };
        let message: Vec<u16> = message.encode_utf16().chain(Some(0)).collect();
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                message.as_ptr(),
                windows_sys::core::w!("Guac Bridge update"),
                MB_OK
                    | if result.is_ok() {
                        MB_ICONINFORMATION
                    } else {
                        MB_ICONERROR
                    },
            );
        }
        result
    }
    #[cfg(not(windows))]
    {
        let _ = (parent, uv);
        anyhow::bail!("Internal updater is only used on Windows")
    }
}
