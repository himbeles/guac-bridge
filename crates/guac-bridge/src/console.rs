//! Hook output goes to stderr and, when SSH redirects it, to the calling terminal.
use std::io::IsTerminal;
#[cfg(unix)]
use std::io::Write;
pub(crate) fn line(text: &str) {
    eprintln!("{text}");
    if std::io::stderr().is_terminal() {
        return;
    }
    #[cfg(unix)]
    if let Ok(mut terminal) = std::fs::OpenOptions::new().write(true).open("/dev/tty") {
        let _ = writeln!(terminal, "{text}");
    }
    #[cfg(windows)]
    if let Ok(terminal) = std::fs::OpenOptions::new().write(true).open("CONOUT$") {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Console::WriteConsoleW;
        let wide: Vec<u16> = format!("{text}\r\n").encode_utf16().collect();
        let mut written = 0;
        // The opened console handle and UTF-16 buffer remain alive for the call.
        unsafe {
            WriteConsoleW(
                terminal.as_raw_handle(),
                wide.as_ptr(),
                wide.len() as u32,
                &mut written,
                std::ptr::null(),
            );
        }
    }
}
