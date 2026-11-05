use base64::{Engine, engine::general_purpose::STANDARD};
use std::io::Write;
struct ConsoleStdout<W> {
    _console: W,
    output: std::io::StdoutLock<'static>,
}
impl<W> Write for ConsoleStdout<W> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.output.write(bytes)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.output.flush()
    }
}
fn main() {
    #[cfg(windows)]
    let stdout = {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Console::*;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONOUT$")
            .unwrap();
        unsafe {
            let mut mode = 0;
            assert_ne!(GetConsoleMode(file.as_raw_handle(), &mut mode), 0);
            assert_ne!(
                SetConsoleMode(
                    file.as_raw_handle(),
                    mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                ),
                0
            );
            assert_ne!(SetStdHandle(STD_OUTPUT_HANDLE, file.as_raw_handle()), 0);
        }
        file
    };
    #[cfg(not(windows))]
    let stdout = std::io::stdout().lock();
    let flags: Vec<_> = std::env::args().collect();
    let mut stdout: Box<dyn Write> = if flags.iter().any(|s| s == "--stdio") {
        // Keep the file owning the installed console handle alive for stdout.
        Box::new(ConsoleStdout {
            _console: stdout,
            output: std::io::stdout().lock(),
        })
    } else {
        Box::new(stdout)
    };
    let payload: Vec<u8> = if flags.iter().any(|s| s == "--mixed") {
        let mut seed = 0x1794_a5c2_u64;
        (0..524288)
            .map(|_| {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                seed as u8
            })
            .collect()
    } else {
        (0..65536).map(|n| (n % 256) as u8).collect()
    };
    if flags.iter().any(|s| s == "--envelopes") {
        let bytes = guac_bridge::terminal_wire::osc_bytes("GBW1:", &payload);
        guac_bridge::terminal_wire::write_output(&mut stdout, &bytes, true).unwrap();
        return;
    }
    if flags.iter().any(|s| s == "--bulk") {
        let bytes = guac_bridge::terminal_wire::osc_bytes("GBW1:", &payload);
        stdout.write_all(&bytes).unwrap();
        stdout.flush().unwrap();
        return;
    }
    for part in payload.chunks(120) {
        let frame = format!(
            "\x1b]482202;2;GBW1:{}\x07\x1b]482203;\x07",
            STANDARD.encode(part)
        );
        stdout.write_all(frame.as_bytes()).unwrap();
    }
    stdout.flush().unwrap();
}
