use base64::{Engine,engine::general_purpose::STANDARD};
use std::io::Write;
fn main() {
    #[cfg(windows)]
    let mut stdout={
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Console::*;
        let file=std::fs::OpenOptions::new().read(true).write(true).open("CONOUT$").unwrap();
        unsafe {let mut mode=0;assert_ne!(GetConsoleMode(file.as_raw_handle(),&mut mode),0);assert_ne!(SetConsoleMode(file.as_raw_handle(),mode|ENABLE_VIRTUAL_TERMINAL_PROCESSING),0);}
        file
    };
    #[cfg(not(windows))]
    let mut stdout=std::io::stdout().lock();
    let payload:Vec<u8>=(0..65536).map(|n|(n%256) as u8).collect();
    for part in payload.chunks(120) {
        let frame=format!("\x1b]482202;2;GBW1:{}\x07\x1b]482203;\x07",STANDARD.encode(part));
        stdout.write_all(frame.as_bytes()).unwrap();
    }
    stdout.flush().unwrap();
}
