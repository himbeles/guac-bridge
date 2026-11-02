use base64::{Engine,engine::general_purpose::STANDARD};
use std::io::Write;
fn main() {
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::System::Console::*;
        let handle=GetStdHandle(STD_OUTPUT_HANDLE);let mut mode=0;
        if GetConsoleMode(handle,&mut mode)!=0 {SetConsoleMode(handle,mode|ENABLE_VIRTUAL_TERMINAL_PROCESSING);}
    }
    let payload:Vec<u8>=(0..65536).map(|n|(n%256) as u8).collect();
    let mut stdout=std::io::stdout().lock();
    for part in payload.chunks(120) {
        let frame=format!("\x1b]482202;2;GBW1:{}\x07\x1b]482203;\x07",STANDARD.encode(part));
        stdout.write_all(frame.as_bytes()).unwrap();
    }
    stdout.flush().unwrap();
}
