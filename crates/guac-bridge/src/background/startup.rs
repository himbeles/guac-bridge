//! Managed child launch, password stdin handoff and startup-log relay.
//! The parent waits for authenticated readiness; it never treats a PID as authority.
use super::{directory, identity, request, state};
use crate::options::BridgeOptions;
use anyhow::{Context, Result, bail, ensure};
use std::{
    fs::OpenOptions,
    io::{Read, Write},
    process::Command,
    time::{Duration, Instant},
};

/// Follow complete log lines while startup is in progress. Keep errors for the
/// caller to print once, and let it print the final ready line with PID/log path.
#[derive(Default)]
struct StartupLog {
    pending: Vec<u8>,
    error: Option<String>,
}
impl StartupLog {
    fn feed(&mut self, bytes: &[u8]) -> Vec<String> {
        self.pending.extend_from_slice(bytes);
        let mut lines = Vec::new();
        while let Some(end) = self.pending.iter().position(|b| *b == b'\n') {
            let line = String::from_utf8_lossy(&self.pending.drain(..=end).collect::<Vec<_>>())
                .trim_end()
                .to_owned();
            if let Some((_, message)) = line.split_once("Error: ") {
                self.error = Some(message.trim_end_matches("\x1b[0m").to_owned());
            } else if !line.starts_with("Bridge ready on") && !line.is_empty() {
                lines.push(line);
            }
        }
        lines
    }
    fn poll(&mut self, file: &mut std::fs::File) -> Result<bool> {
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        let progressed = !bytes.is_empty();
        for line in self.feed(&bytes) {
            if !crate::console::hook_upload(&line) {
                crate::console::line(&line);
            }
        }
        Ok(progressed)
    }
}
pub(crate) fn handle(mut args: BridgeOptions) -> Result<()> {
    let dir = directory()?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(dir.join(format!("{}-{}.lock", args.mode, args.port())))?;
    lock.lock()?;
    if let Ok(s) = state(&args)
        && request(&s, "status").is_ok()
    {
        #[cfg(windows)]
        if !args.stop && !args.status {
            crate::tray::launch()?;
        }
        if args.stop {
            request(&s, "stop")?;
            crate::console::line(&format!("Bridge stopping on port {}", args.port()));
            return Ok(());
        }
        if args.status {
            crate::console::line(&format!(
                "Bridge running on 127.0.0.1:{} (PID {})",
                args.port(),
                s.pid
            ));
            return Ok(());
        }
        ensure!(
            s.identity == identity(&args)
                && args.username.as_ref().is_none_or(|u| u == &s.username)
                && args
                    .server_user
                    .as_ref()
                    .is_none_or(|u| Some(u) == s.server_user.as_ref()),
            "A bridge with different settings already owns this port. Stop it first with --stop --port {}",
            args.port()
        );
        crate::console::line(&format!(
            "Bridge already running on 127.0.0.1:{}",
            args.port()
        ));
        return Ok(());
    }
    if args.stop {
        crate::console::line(&format!(
            "No managed bridge running on port {}",
            args.port()
        ));
        return Ok(());
    }
    if args.status {
        bail!("No managed bridge running on port {}", args.port());
    }
    crate::console::line("Starting bridge…");
    // Prompt in the calling console. The password travels only over the child's stdin pipe.
    let user = crate::credentials::username(&args)?;
    args.username = Some(user.clone());
    let password = crate::credentials::password(&args, &user)?;
    let server_password = if args.gateway == crate::options::Gateway::Fortigate {
        let user = crate::credentials::server_username(&args)?;
        args.server_user = Some(user.clone());
        Some(crate::credentials::server_password(&args, &user)?)
    } else {
        None
    };
    let handoff = if args.gateway == crate::options::Gateway::Guacamole {
        serde_json::to_string(&password)?
    } else {
        serde_json::json!({"gateway_password":password, "server_password":server_password})
            .to_string()
    };
    let exe = std::env::current_exe()?;
    let mut command = Command::new(&exe);
    command
        .arg(&args.mode)
        .arg(args.url.as_deref().context("URL is required")?);
    for (flag, value) in [
        ("--user", args.username.clone()),
        ("--server-host", args.server_host.clone()),
        ("--server-user", args.server_user.clone()),
        ("--connection-id", args.connection_id.clone()),
        ("--data-source", args.data_source.clone()),
        ("--ca-file", args.ca_file.clone()),
        ("--server-command", args.server_command.clone()),
        ("--debug-log", args.debug_log.clone()),
        ("--server-debug-log", args.server_debug_log.clone()),
    ] {
        if let Some(v) = value {
            command.args([flag, &v]);
        }
    }
    command.args([
        "--gateway",
        args.gateway.argument(),
        "--server-port",
        &args.server_port.to_string(),
        "--upload-batch-size",
        &args.upload_batch_size.to_string(),
        "--server-os",
        args.server_os.as_str(),
        "--port",
        &args.port().to_string(),
        "--remote-port",
        &args.target_port().to_string(),
        "--target-host",
        &args.target_host,
        "--window",
        &args.window.to_string(),
        "--input-window",
        &args.input_window.to_string(),
        "--frame-size",
        &args.frame_size.to_string(),
        "--connect-timeout",
        &args.connect_timeout.to_string(),
        "--startup-delay",
        &args.startup_delay.to_string(),
        "--max-connections",
        &args.max_connections.to_string(),
        "--managed-child",
    ]);
    for (flag, set) in [
        ("--save-password", args.save_password),
        ("--save-server-password", args.save_server_password),
        ("--no-keyring", args.no_keyring),
        ("--insecure", args.insecure),
    ] {
        if set {
            command.arg(flag);
        }
    }
    let log_path = dir.join(format!("{}-{}.log", args.mode, args.port()));
    let log = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&log_path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&log_path, std::fs::Permissions::from_mode(0o600))?;
    }
    #[cfg(windows)]
    let mut child = {
        let (input, mut password_pipe) = std::io::pipe()?;
        let child = crate::child_process::spawn(&command, &input, &log, 0x08000000 | 0x00000200)
            .context("Cannot start managed bridge")?;
        drop(input);
        writeln!(password_pipe, "{}", handoff)?;
        child
    };
    #[cfg(unix)]
    let mut child = {
        use std::{os::unix::process::CommandExt, process::Stdio};
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .process_group(0);
        let mut child = command.spawn().context("Cannot start managed bridge")?;
        writeln!(
            child.stdin.take().context("No child input pipe")?,
            "{}",
            handoff
        )?;
        child
    };
    let mut startup_file = std::fs::File::open(&log_path)?;
    let mut startup_log = StartupLog::default();
    let mut deadline = Instant::now() + Duration::from_secs(args.connect_timeout + 135);
    loop {
        if startup_log.poll(&mut startup_file)? {
            deadline = Instant::now() + Duration::from_secs(args.connect_timeout + 135);
        }
        if let Some(exit) = child.try_wait()? {
            startup_log.poll(&mut startup_file)?;
            if let Some(message) = startup_log.error {
                bail!("{message}");
            }
            bail!("Bridge startup failed ({exit}); see {}", log_path.display());
        }
        if let Ok(s) = state(&args)
            && s.pid == child.id()
            && request(&s, "status").is_ok()
        {
            startup_log.poll(&mut startup_file)?;
            crate::console::line(&format!(
                "Bridge ready on 127.0.0.1:{} (PID {}; output: {})",
                args.port(),
                s.pid,
                log_path.display()
            ));
            return Ok(());
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            bail!("Bridge startup timed out; see {}", log_path.display());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
#[cfg(test)]
mod log_tests {
    use super::*;
    #[test]
    fn partial_utf8_and_errors_are_relayed_once() {
        let mut log = StartupLog::default();
        let message = "Checking server helper…\n".as_bytes();
        assert!(log.feed(&message[..message.len() - 2]).is_empty());
        assert_eq!(
            log.feed(&message[message.len() - 2..]),
            vec!["Checking server helper…"]
        );
        assert!(log.feed(b"Bridge ready on 127.0.0.1:2222\n").is_empty());
        assert!(
            log.feed(b"\x1b[31mError: Helper checksum mismatch\x1b[0m\n")
                .is_empty()
        );
        assert_eq!(log.error.as_deref(), Some("Helper checksum mismatch"));
    }
}
