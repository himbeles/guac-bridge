use base64::{Engine, engine::general_purpose::STANDARD};
use std::{io::Write, time::Instant};
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
struct MeasuredWriter<W> {
    output: W,
    writes: u64,
    flushes: u64,
}
impl<W: Write> Write for MeasuredWriter<W> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.writes += 1;
        self.output.write(bytes)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.flushes += 1;
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
    let output: Box<dyn Write> = if flags.iter().any(|s| s == "--stdio") {
        Box::new(ConsoleStdout {
            _console: stdout,
            output: std::io::stdout().lock(),
        })
    } else {
        Box::new(stdout)
    };
    let mut stdout = MeasuredWriter {
        output,
        writes: 0,
        flushes: 0,
    };
    let payload: Vec<u8> = if flags.iter().any(|s| s == "--mixed") {
        let mut seed = 0x1794_a5c2_u64;
        (0..4 * 1048576)
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
    let encode_start = Instant::now();
    let bytes = guac_bridge::terminal_wire::osc_bytes("GBW1:", &payload);
    let encode_s = encode_start.elapsed().as_secs_f64();
    let start = Instant::now();
    if flags.iter().any(|s| s == "--envelopes") {
        guac_bridge::terminal_wire::write_output(&mut stdout, &bytes, true).unwrap();
    } else if flags.iter().any(|s| s == "--pairs") {
        let mut offset = 0;
        let mut terminators = 0;
        for (index, &byte) in bytes.iter().enumerate() {
            if byte == 7 {
                terminators += 1;
                if terminators % 2 == 0 {
                    stdout.write_all(&bytes[offset..=index]).unwrap();
                    stdout.flush().unwrap();
                    offset = index + 1;
                }
            }
        }
        assert_eq!(offset, bytes.len());
    } else if flags.iter().any(|s| s == "--bulk") {
        stdout.write_all(&bytes).unwrap();
        stdout.flush().unwrap();
    } else {
        for part in payload.chunks(120) {
            let frame = format!(
                "\x1b]482202;2;GBW1:{}\x07\x1b]482203;\x07",
                STANDARD.encode(part)
            );
            stdout.write_all(frame.as_bytes()).unwrap();
        }
        stdout.flush().unwrap();
    }
    let seconds = start.elapsed().as_secs_f64();
    let stats = serde_json::json!({"payload_bytes": payload.len(), "wire_bytes": bytes.len(),
        "write_calls": stdout.writes, "flush_calls": stdout.flushes, "encode_s": encode_s,
        "write_s": seconds, "write_mib_s": payload.len() as f64 / 1048576.0 / seconds});
    stdout
        .write_all(format!("\r\nGBPROBE:{stats}\r\n").as_bytes())
        .unwrap();
    stdout.flush().unwrap();
}
