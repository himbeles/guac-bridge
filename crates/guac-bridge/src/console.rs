//! Human-facing status output, including a single updating upload row.
use std::io::{IsTerminal, Write};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
static PROGRESS: AtomicU8 = AtomicU8::new(0);
static UPLOADING: AtomicBool = AtomicBool::new(false);
static COMPLETED: AtomicBool = AtomicBool::new(false);
fn terminal_write(text: &str) {
    #[cfg(unix)]
    if let Ok(mut terminal) = std::fs::OpenOptions::new().write(true).open("/dev/tty") {
        let _ = terminal.write_all(text.as_bytes());
    }
    #[cfg(windows)]
    if let Ok(terminal) = std::fs::OpenOptions::new().write(true).open("CONOUT$") {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Console::WriteConsoleW;
        let wide: Vec<u16> = text.encode_utf16().collect();
        let mut written = 0;
        // The console handle and UTF-16 buffer remain alive for this call.
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
pub(crate) fn finish_progress() {
    let flags = PROGRESS.swap(0, Ordering::Relaxed);
    if flags & 1 != 0 {
        eprintln!();
    }
    if flags & 2 != 0 {
        terminal_write("\r\n");
    }
}
pub(crate) fn line(text: &str) {
    finish_progress();
    eprintln!("{text}");
    if !std::io::stderr().is_terminal() {
        terminal_write(&format!("{text}\r\n"));
    }
}
fn draw(percent: usize, confirmed: bool, hook: bool) {
    let percent = percent.min(100);
    let filled = percent * 24 / 100;
    let qualifier = if confirmed {
        "remote confirmed"
    } else {
        "gateway acknowledged"
    };
    let row = format!(
        "\rHelper upload: {percent:3}% [{}{}] {qualifier:<20}   ",
        "=".repeat(filled),
        " ".repeat(24 - filled)
    );
    if std::io::stderr().is_terminal() {
        eprint!("{row}");
        let _ = std::io::stderr().flush();
        PROGRESS.fetch_or(1, Ordering::Relaxed);
    } else if hook {
        terminal_write(&row);
        PROGRESS.fetch_or(2, Ordering::Relaxed);
    }
    if percent == 100 {
        finish_progress();
    }
}
pub(crate) fn complete_upload() {
    if UPLOADING.swap(false, Ordering::Relaxed) {
        if PROGRESS.load(Ordering::Relaxed) != 0 {
            draw(100, true, false);
        } else {
            eprintln!("Helper upload: 100% (confirmed by remote shell)");
        }
        COMPLETED.store(true, Ordering::Relaxed);
    }
}
pub(crate) fn hook_upload(text: &str) -> bool {
    let Some(rest) = text.strip_prefix("Helper upload: ") else {
        return false;
    };
    let Some((number, _)) = rest.split_once('%') else {
        return false;
    };
    let Ok(percent) = number.trim().parse() else {
        return false;
    };
    if !std::io::stderr().is_terminal() {
        eprintln!("{text}");
    }
    draw(percent, text.contains("remote shell"), true);
    true
}
pub(crate) struct UploadProgress {
    enabled: bool,
    last_reported: usize,
}
impl UploadProgress {
    pub(crate) fn new(enabled: bool) -> Self {
        if enabled {
            COMPLETED.store(false, Ordering::Relaxed);
            UPLOADING.store(true, Ordering::Relaxed);
        }
        Self {
            enabled,
            last_reported: 0,
        }
    }
    pub(crate) fn update(&mut self, percent: usize, confirmed: bool) {
        if !self.enabled || COMPLETED.load(Ordering::Relaxed) {
            return;
        }
        if std::io::stderr().is_terminal() {
            draw(percent, confirmed, false);
        } else if percent >= self.last_reported + 25 || percent == 100 {
            let qualifier = if confirmed {
                "confirmed by remote shell"
            } else {
                "gateway acknowledged"
            };
            eprintln!("Helper upload: {percent}% ({qualifier})");
            self.last_reported = percent;
        }
        if percent == 100 {
            COMPLETED.store(true, Ordering::Relaxed);
            UPLOADING.store(false, Ordering::Relaxed);
        }
    }
}
impl Drop for UploadProgress {
    fn drop(&mut self) {
        if self.enabled {
            UPLOADING.store(false, Ordering::Relaxed);
            finish_progress();
        }
    }
}
