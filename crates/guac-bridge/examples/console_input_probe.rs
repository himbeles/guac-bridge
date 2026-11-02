fn main() -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::{
            io::{Read, Write},
            os::windows::io::AsRawHandle,
        };
        use windows_sys::Win32::System::Console::*;
        let mut input = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONIN$")?;
        let mut output = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONOUT$")?;
        unsafe {
            SetConsoleMode(input.as_raw_handle(), ENABLE_EXTENDED_FLAGS);
            SetConsoleMode(
                output.as_raw_handle(),
                ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            );
        }
        output.write_all(b"\x1b]482202;2;INPUT:READY\x07\x1b]482203;\x07")?;
        output.flush()?;
        let mut data = [0; 64];
        let n = input.read(&mut data)?;
        eprintln!("console input read {n} bytes: {:?}", &data[..n]);
        use base64::Engine;
        write!(
            output,
            "\x1b]482202;2;INPUT:{}\x07\x1b]482203;\x07",
            base64::engine::general_purpose::STANDARD.encode(&data[..n])
        )?;
        output.flush()?;
    }
    Ok(())
}
