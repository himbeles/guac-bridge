fn main() -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::process::{Command, Stdio};
        let mut args = std::env::args_os().skip(1);
        let program = args.next().expect("shell executable");
        let input = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONIN$")?;
        let output = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONOUT$")?;
        let status = Command::new(program)
            .args(args)
            .stdin(Stdio::from(input))
            .stdout(Stdio::from(output.try_clone()?))
            .stderr(Stdio::from(output))
            .status()?;
        std::process::exit(status.code().unwrap_or(1));
    }
    #[cfg(not(windows))]
    Ok(())
}
