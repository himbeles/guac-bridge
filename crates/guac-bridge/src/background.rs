//! Managed processes use an authenticated loopback control socket, never PID-based killing.
use crate::cli::Args;
use anyhow::{Context, Result, bail, ensure};
use directories::ProjectDirs;
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

#[derive(Clone, Serialize, Deserialize)]
struct State {
    pid: u32,
    control: u16,
    token: String,
    identity: String,
    username: Option<String>,
    #[serde(default)]
    mode: String,
    #[serde(default)]
    port: u16,
    #[serde(default)]
    target: String,
    #[serde(default)]
    version: String,
    #[serde(default)]
    executable: PathBuf,
    #[serde(default)]
    instance: String,
    #[serde(default)]
    log: Option<PathBuf>,
}
pub(crate) fn directory() -> Result<PathBuf> {
    let p = ProjectDirs::from("", "", "guac-bridge")
        .context("Cannot find user data directory")?
        .data_local_dir()
        .to_owned();
    fs::create_dir_all(&p)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o700))?;
    }
    Ok(p)
}
fn path(args: &Args) -> Result<PathBuf> {
    Ok(directory()?.join(format!("{}-{}.json", args.mode, args.port())))
}
fn identity(args: &Args) -> String {
    let data = serde_json::json!([
        args.url,
        args.connection_id,
        args.data_source,
        args.mode,
        args.port(),
        args.target_host,
        args.target_port(),
        args.server_command,
        args.server_os,
        args.debug_log,
        args.server_debug_log,
        args.upload_batch_size,
        args.window,
        args.input_window,
        "reliable-v1",
        args.frame_size,
        args.ca_file,
        args.insecure
    ]);
    format!("{:x}", Sha256::digest(data.to_string().as_bytes()))
}
fn state(args: &Args) -> Result<State> {
    Ok(serde_json::from_slice(&fs::read(path(args)?)?)?)
}
fn exchange(state: &State, command: &str) -> Result<String> {
    let mut stream = TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], state.control)),
        Duration::from_secs(1),
    )?;
    stream.set_read_timeout(Some(Duration::from_secs(1)))?;
    stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    writeln!(
        stream,
        "{}",
        serde_json::json!({"token":state.token,"command":command})
    )?;
    let mut response = String::new();
    BufReader::new(stream)
        .take(16384)
        .read_line(&mut response)?;
    Ok(response)
}
fn request(state: &State, command: &str) -> Result<()> {
    let response = exchange(state, command)?;
    ensure!(
        response.trim() == "OK",
        "Managed process did not accept the command"
    );
    Ok(())
}
use std::io::Read;
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
pub(crate) fn handle(mut args: Args) -> Result<()> {
    let dir = directory()?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(dir.join(format!("{}-{}.lock", args.mode, args.port())))?;
    lock.lock_exclusive()?;
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
                && args
                    .username
                    .as_ref()
                    .is_none_or(|u| s.username.as_ref() == Some(u)),
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
    let exe = std::env::current_exe()?;
    let mut command = Command::new(&exe);
    if !exe
        .file_stem()
        .is_some_and(|s| s == "guac-ssh-client" || s == "guac-llm-client")
    {
        command.arg(&args.mode);
    }
    for (flag, value) in [
        ("--url", args.url.clone()),
        ("--username", args.username.clone()),
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
        "--upload-batch-size",
        &args.upload_batch_size.to_string(),
        "--server-os",
        args.server_os.as_str(),
        "--port",
        &args.port().to_string(),
        "--target-port",
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
        ("--no-keyring", args.no_keyring),
        ("--insecure", args.insecure),
        ("--show-console", args.show_console),
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
        fs::set_permissions(&log_path, fs::Permissions::from_mode(0o600))?;
    }
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(if args.show_console {
            0x00000010 | 0x00000200
        } else {
            0x08000000 | 0x00000200
        });
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    clear_parent_stdio_inheritance()?;
    let mut child = command.spawn().context("Cannot start managed bridge")?;
    writeln!(
        child.stdin.take().context("No child input pipe")?,
        "{}",
        serde_json::to_string(&password)?
    )?;
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
pub(crate) struct Managed {
    path: PathBuf,
    token: String,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Managed {
    fn drop(&mut self) {
        self.task.abort();
        if let Ok(s) = fs::read(&self.path)
            .ok()
            .and_then(|v| serde_json::from_slice::<State>(&v).ok())
            .ok_or(())
            && s.token == self.token
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}
pub(crate) async fn control(
    args: &Args,
    port: u16,
    stop: tokio::sync::mpsc::Sender<()>,
    usage: std::sync::Arc<crate::usage::Usage>,
) -> Result<Option<Managed>> {
    if args.benchmark_control {
        return Ok(None);
    }
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0)).await?;
    let s = State {
        pid: std::process::id(),
        control: listener.local_addr()?.port(),
        token: uuid::Uuid::new_v4().to_string(),
        identity: identity(args),
        username: args.username.clone(),
        mode: args.mode.clone(),
        port,
        target: format!("{}:{}", args.target_host, args.target_port()),
        version: env!("CARGO_PKG_VERSION").into(),
        executable: std::env::current_exe()?,
        instance: uuid::Uuid::new_v4().to_string(),
        log: if args.managed_child {
            Some(directory()?.join(format!("{}-{}.log", args.mode, port)))
        } else {
            args.debug_log.as_ref().map(PathBuf::from)
        },
    };
    let path = directory()?.join(format!("{}-{}.json", args.mode, port));
    let token = s.token.clone();
    let temp = path.with_extension(format!("{}.tmp", s.pid));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    {
        let mut f = options.open(&temp)?;
        f.write_all(&serde_json::to_vec(&s)?)?;
        f.sync_all()?;
    }
    // Windows rename cannot replace an existing destination.
    if path.exists() {
        fs::remove_file(&path)?;
    }
    fs::rename(temp, &path)?;
    let expected = token.clone();
    let task = tokio::spawn(async move {
        while let Ok((socket, _)) = listener.accept().await {
            let expected = expected.clone();
            let stop = stop.clone();
            let usage = usage.clone();
            tokio::spawn(control_connection(socket, expected, stop, usage));
        }
    });
    Ok(Some(Managed { path, token, task }))
}

async fn control_connection(
    socket: tokio::net::TcpStream,
    expected: String,
    stop: tokio::sync::mpsc::Sender<()>,
    usage: std::sync::Arc<crate::usage::Usage>,
) {
    use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt};

    let (reader, mut writer) = socket.into_split();
    let mut line = String::new();
    let read = tokio::io::BufReader::new(reader).take(1024);
    let mut read = tokio::io::BufReader::new(read);
    if !matches!(
        tokio::time::timeout(Duration::from_secs(2), read.read_line(&mut line)).await,
        Ok(Ok(_))
    ) {
        return;
    }
    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line)
        && v["token"] == expected
        && (v["command"] == "status" || v["command"] == "stop" || v["command"] == "inspect")
    {
        if v["command"] == "inspect" {
            if let Ok(mut response) = serde_json::to_vec(&usage.snapshot()) {
                response.push(b'\n');
                let _ = writer.write_all(&response).await;
            }
            return;
        }
        let _ = writer.write_all(b"OK\n").await;
        if v["command"] == "stop" {
            let _ = stop.send(()).await;
        }
    }
}

#[cfg(windows)]
pub(crate) fn visible_console() -> Result<()> {
    use std::os::windows::io::IntoRawHandle;
    use windows_sys::Win32::System::Console::{STD_ERROR_HANDLE, STD_OUTPUT_HANDLE, SetStdHandle};
    let output = OpenOptions::new().write(true).open("CONOUT$")?;
    let error = output.try_clone()?;
    // Keep the handles alive for the process lifetime, matching the OS standard handles.
    unsafe {
        SetStdHandle(STD_OUTPUT_HANDLE, output.into_raw_handle());
        SetStdHandle(STD_ERROR_HANDLE, error.into_raw_handle());
    }
    Ok(())
}

#[cfg(windows)]
fn clear_parent_stdio_inheritance() -> Result<()> {
    use windows_sys::Win32::{
        Foundation::{
            GetHandleInformation, HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE, SetHandleInformation,
        },
        System::Console::{GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE},
    };
    // Stable Rust Command inherits every inheritable process handle, including
    // the SSH hook's original output pipes, even when its child's stdio is redirected.
    // Clear only these inherited originals; Command creates the new child's own
    // stdin pipe and log handles separately. No process-wide handle enumeration.
    for id in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
        unsafe {
            let handle = GetStdHandle(id);
            if handle.is_null() || handle == INVALID_HANDLE_VALUE {
                continue;
            }
            let mut flags = 0;
            if GetHandleInformation(handle, &mut flags) != 0 && flags & HANDLE_FLAG_INHERIT != 0 {
                ensure!(
                    SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0) != 0,
                    "Cannot isolate background process output handles: {}",
                    std::io::Error::last_os_error()
                );
            }
        }
    }
    Ok(())
}

/// A registration is only considered live after its authenticated socket responds.
/// Never remove an unresponsive registration or terminate a PID from this data.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Clone)]
pub(crate) struct Bridge {
    state: State,
    pub key: String,
    pub instance: String,
    pub label: String,
    pub details: String,
    pub usage: Option<crate::usage::Snapshot>,
    pub online: bool,
}
impl Bridge {
    pub fn stop(&self) -> Result<()> {
        request(&self.state, "stop")
    }
    #[cfg(windows)]
    pub fn log_path(&self) -> Option<&PathBuf> {
        self.state.log.as_ref()
    }
}
pub(crate) fn bridges() -> Result<Vec<Bridge>> {
    let mut result = Vec::new();
    for entry in fs::read_dir(directory()?)? {
        let path = entry?.path();
        let Some(key) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.extension().is_none_or(|s| s != "json")
            || !(key.starts_with("ssh-") || key.starts_with("llm-"))
        {
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<State>(&bytes) else {
            continue;
        };
        let usage = exchange(&state, "inspect")
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok());
        let online = usage.is_some() || request(&state, "status").is_ok();
        let tag = if state.mode.is_empty() {
            key.split('-').next().unwrap_or("bridge")
        } else {
            &state.mode
        };
        result.push(Bridge {
            key: key.into(),
            instance: if state.instance.is_empty() {
                format!("{key}-{}", state.pid)
            } else {
                state.instance.clone()
            },
            label: format!(
                "[{}]  127.0.0.1:{} → {}",
                tag.to_uppercase(),
                if state.port == 0 {
                    key.rsplit('-').next().unwrap_or("?").to_string()
                } else {
                    state.port.to_string()
                },
                state.target
            ),
            details: format!(
                "PID: {}   Version: {}\r\nExecutable: {}\r\nInstance: {}",
                state.pid,
                state.version,
                state.executable.display(),
                state.instance
            ),
            state,
            usage,
            online,
        });
    }
    result.sort_by(|a, b| a.key.cmp(&b.key));
    Ok(result)
}

pub(crate) fn stop_all() -> Result<()> {
    let bridges = bridges()?;
    #[cfg(windows)]
    let mut processes = Vec::new();
    for bridge in &bridges {
        if !bridge.online {
            continue;
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::FromRawHandle;
            use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};
            let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, bridge.state.pid) };
            ensure!(
                !handle.is_null(),
                "Cannot wait for {} to exit: {}",
                bridge.label,
                std::io::Error::last_os_error()
            );
            processes.push(unsafe { std::os::windows::io::OwnedHandle::from_raw_handle(handle) });
        }
        bridge
            .stop()
            .with_context(|| format!("Cannot stop {}", bridge.label))?;
    }
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let mut active = false;
        for bridge in bridges.iter().filter(|b| b.online) {
            if request(&bridge.state, "status").is_ok() {
                active = true;
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::System::Threading::WaitForSingleObject;
            active |= processes.iter().any(|p| unsafe { WaitForSingleObject(p.as_raw_handle(), 0) } != windows_sys::Win32::Foundation::WAIT_OBJECT_0);
        }
        if !active {
            break;
        }
        ensure!(
            Instant::now() < deadline,
            "Some bridges did not exit; update cancelled"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    crate::tray::shutdown()?;
    Ok(())
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

#[cfg(test)]
mod control_tests {
    use super::*;
    use std::sync::Arc;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    async fn roundtrip(command: &str, token: &str) -> (String, bool) {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let mut client = tokio::net::TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (server, _) = listener.accept().await.unwrap();
        let (stop, mut stopped) = tokio::sync::mpsc::channel(1);
        let usage = Arc::new(crate::usage::Usage::default());
        usage.transferred(true, 321);
        let task = tokio::spawn(control_connection(
            server,
            "correct-token".into(),
            stop,
            usage,
        ));
        client
            .write_all(
                format!("{}\n", serde_json::json!({"command":command,"token":token})).as_bytes(),
            )
            .await
            .unwrap();
        let mut response = String::new();
        client.read_to_string(&mut response).await.unwrap();
        task.await.unwrap();
        (response, stopped.try_recv().is_ok())
    }
    #[tokio::test]
    async fn authentication_and_legacy_commands() {
        assert_eq!(
            roundtrip("status", "correct-token").await,
            ("OK\n".into(), false)
        );
        assert_eq!(
            roundtrip("stop", "correct-token").await,
            ("OK\n".into(), true)
        );
        for command in ["stop", "inspect", "status", "unknown"] {
            assert_eq!(
                roundtrip(command, "wrong-token").await,
                (String::new(), false)
            );
        }
        let (value, stopped) = roundtrip("inspect", "correct-token").await;
        let snapshot: crate::usage::Snapshot = serde_json::from_str(&value).unwrap();
        assert_eq!(snapshot.uploaded, 321);
        assert!(!stopped);
        assert!(!value.contains("correct-token"));
    }
    #[test]
    fn older_registrations_remain_readable() {
        let value = r#"{"pid":1,"control":1234,"token":"secret","identity":"old","username":null}"#;
        let state: State = serde_json::from_str(value).unwrap();
        assert!(state.instance.is_empty());
        assert!(state.log.is_none());
    }
}
