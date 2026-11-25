//! Managed processes use an authenticated loopback control socket, never PID-based killing.
use crate::options::BridgeOptions;
use anyhow::{Context, Result, ensure};
use directories::ProjectDirs;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    net::{IpAddr, SocketAddr, TcpStream},
    path::PathBuf,
    time::{Duration, Instant},
};

#[derive(Clone, Serialize, Deserialize)]
struct State {
    pid: u32,
    control: u16,
    token: String,
    identity: String,
    username: String,
    #[serde(default)]
    gateway: crate::options::Gateway,
    #[serde(default)]
    server_user: Option<String>,
    #[serde(default)]
    server_host: Option<String>,
    #[serde(default)]
    server_port: u16,
    jump_url: String,
    mode: String,
    port: u16,
    #[serde(default = "crate::options::default_host")]
    host: IpAddr,
    target: String,
    #[serde(default)]
    reverse: bool,
    #[serde(default)]
    remote_scope: String,
    #[serde(default)]
    remote_port: u16,
    version: String,
    executable: PathBuf,
    instance: String,
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
fn registration_key(mode: &str, host: IpAddr, port: u16) -> String {
    if host == crate::options::default_host() {
        format!("{mode}-{port}")
    } else {
        format!("{mode}-{port}-{}", host.to_string().replace(':', "_"))
    }
}
fn remote_scope(args: &BridgeOptions) -> String {
    format!(
        "{:x}",
        Sha256::digest(
            serde_json::json!([
                args.gateway,
                args.url,
                args.connection_id,
                args.data_source,
                args.server_host,
                args.server_port,
                args.remote_host
            ])
            .to_string()
            .as_bytes()
        )
    )
}
fn key(args: &BridgeOptions, port: u16) -> String {
    if args.reverse {
        format!(
            "reverse-{}-{}-{}",
            args.mode,
            args.remote_port(),
            remote_scope(args)
        )
    } else {
        registration_key(&args.mode, args.host, port)
    }
}
fn path(args: &BridgeOptions) -> Result<PathBuf> {
    Ok(directory()?.join(format!("{}.json", key(args, args.port()))))
}
fn identity(args: &BridgeOptions) -> String {
    let mut data = serde_json::json!([
        args.url,
        args.connection_id,
        args.data_source,
        args.mode.as_str(),
        args.port(),
        args.remote_host,
        args.remote_port(),
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
    if args.gateway == crate::options::Gateway::Fortigate {
        data.as_array_mut().unwrap().extend([
            serde_json::json!(args.gateway),
            serde_json::json!(args.server_host),
            serde_json::json!(args.server_port),
            serde_json::json!("output-reliable-v1"),
        ]);
    }
    if args.host != crate::options::default_host() {
        data.as_array_mut()
            .unwrap()
            .push(serde_json::json!(["host", args.host]));
    }
    if args.reverse {
        data.as_array_mut()
            .unwrap()
            .push(serde_json::json!(["reverse", true]));
    }
    format!("{:x}", Sha256::digest(data.to_string().as_bytes()))
}
fn state(args: &BridgeOptions) -> Result<State> {
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
mod startup;
pub(crate) use startup::handle;
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
    args: &BridgeOptions,
    username: &str,
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
        username: username.into(),
        gateway: args.gateway,
        server_user: args.server_user.clone(),
        server_host: args.server_host.clone(),
        server_port: args.server_port,
        jump_url: display_url(args.url.as_deref().unwrap_or_default()),
        mode: args.mode.as_str().into(),
        port,
        host: args.host,
        target: crate::options::endpoint(&args.remote_host, args.remote_port()),
        reverse: args.reverse,
        remote_scope: if args.reverse {
            remote_scope(args)
        } else {
            String::new()
        },
        remote_port: args.remote_port(),
        version: env!("CARGO_PKG_VERSION").into(),
        executable: std::env::current_exe()?,
        instance: uuid::Uuid::new_v4().to_string(),
        log: if args.managed_child {
            Some(directory()?.join(format!("{}.log", key(args, port))))
        } else {
            args.debug_log.as_ref().map(PathBuf::from)
        },
    };
    let path = directory()?.join(format!("{}.json", key(args, port)));
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

/// A registration is only considered live after its authenticated socket responds.
/// Never remove an unresponsive registration or terminate a PID from this data.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Clone)]
pub(crate) struct Bridge {
    state: State,
    pub key: String,
    pub instance: String,
    pub label: String,
    pub usage: Option<crate::usage::Snapshot>,
    pub online: bool,
}
impl Bridge {
    pub fn destination(&self) -> String {
        display_url(&self.state.jump_url)
    }
    pub fn local_url(&self) -> String {
        format!(
            "{}://{}",
            self.scheme(),
            SocketAddr::new(self.state.host, self.state.port)
        )
    }
    pub fn server_url(&self) -> String {
        format!("{}://{} (on server)", self.scheme(), self.state.target)
    }
    fn scheme(&self) -> &str {
        match self.state.mode.as_str() {
            "ssh" => "ssh",
            "rdp" => "rdp",
            _ => "tcp",
        }
    }
    pub fn route(&self) -> String {
        if self.state.reverse {
            return format!(
                "{}{} → {} → {} (on client)",
                self.server_url(),
                self.state
                    .server_host
                    .as_ref()
                    .map(|host| format!(
                        " [SSH {}]",
                        crate::options::endpoint(host, self.state.server_port)
                    ))
                    .unwrap_or_default(),
                self.destination(),
                self.local_url()
            );
        }
        format!(
            "{} → {}{} → {}",
            self.local_url(),
            self.destination(),
            self.state
                .server_host
                .as_ref()
                .map(|host| format!(
                    " → ssh://{}@{}:{}",
                    self.state.server_user.as_deref().unwrap_or(""),
                    host,
                    self.state.server_port
                ))
                .unwrap_or_default(),
            self.server_url()
        )
    }
    pub fn bridge_type(&self) -> &str {
        if self.state.reverse {
            "Reverse TCP"
        } else {
            service_label(&self.state.mode)
        }
    }
    pub fn gateway(&self) -> &str {
        self.state.gateway.name()
    }
    pub fn user(&self) -> &str {
        &self.state.username
    }
    pub fn stop(&self) -> Result<()> {
        request(&self.state, "stop")
    }
    #[cfg(windows)]
    pub fn log_path(&self) -> Option<&PathBuf> {
        self.state.log.as_ref()
    }
}

// Display only navigation information, never URL credentials or authentication queries.
fn display_url(value: &str) -> String {
    let Ok(mut url) = url::Url::parse(value) else {
        return String::new();
    };
    if !matches!(url.scheme(), "http" | "https") {
        return String::new();
    }
    let _ = url.set_username("");
    let _ = url.set_password(None);
    url.set_query(None);
    let fragment = url.fragment().and_then(|f| {
        let path = f.split('?').next().unwrap_or_default();
        path.strip_prefix("/client/")
            .filter(|id| {
                !id.is_empty()
                    && id
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || "=_-%".contains(c))
            })
            .map(|_| path.to_owned())
    });
    url.set_fragment(fragment.as_deref());
    url.to_string()
}

fn service_label(mode: &str) -> &str {
    match mode {
        "ssh" => "SSH",
        "llm" => "LLM",
        "rdp" => "RDP",
        _ => "TCP",
    }
}

pub(crate) fn list() -> Result<()> {
    let bridges = bridges()?;
    if bridges.is_empty() {
        println!("No registered bridges.");
        return Ok(());
    }
    let rows: Vec<[String; 5]> = bridges
        .into_iter()
        .map(|bridge| {
            [
                bridge.bridge_type().into(),
                bridge.gateway().into(),
                if bridge.online {
                    "Running"
                } else {
                    "Unresponsive"
                }
                .into(),
                single_line(bridge.user()),
                single_line(&bridge.route()),
            ]
        })
        .collect();
    print!(
        "{}",
        connection_table(["TYPE", "GATEWAY", "STATUS", "USER", "ROUTE"], &rows)
    );
    Ok(())
}
fn connection_table(headers: [&str; 5], rows: &[[String; 5]]) -> String {
    use unicode_width::UnicodeWidthStr;
    let widths: [usize; 5] = std::array::from_fn(|column| {
        rows.iter()
            .map(|row| row[column].width())
            .chain(std::iter::once(headers[column].width()))
            .max()
            .unwrap()
    });
    let border = |left: char, middle: char, right: char| {
        format!(
            "{left}{}{right}\n",
            widths
                .iter()
                .map(|width| "─".repeat(width + 2))
                .collect::<Vec<_>>()
                .join(&middle.to_string())
        )
    };
    let row = |cells: &[String; 5]| {
        format!(
            "│ {} │\n",
            cells
                .iter()
                .enumerate()
                .map(|(column, text)| format!(
                    "{text}{}",
                    " ".repeat(widths[column] - text.width())
                ))
                .collect::<Vec<_>>()
                .join(" │ ")
        )
    };
    let mut output = border('┌', '┬', '┐');
    output.push_str(&row(&headers.map(str::to_owned)));
    output.push_str(&border('├', '┼', '┤'));
    for cells in rows {
        output.push_str(&row(cells));
    }
    output.push_str(&border('└', '┴', '┘'));
    output
}

fn single_line(value: &str) -> String {
    value
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect()
}
pub(crate) fn bridges() -> Result<Vec<Bridge>> {
    let mut result = Vec::new();
    for entry in fs::read_dir(directory()?)? {
        let path = entry?.path();
        let Some(key) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.extension().is_none_or(|s| s != "json")
            || !(key.starts_with("ssh-")
                || key.starts_with("rdp-")
                || key.starts_with("tcp-")
                || key.starts_with("llm-")
                || key.starts_with("http-")
                || key.starts_with("reverse-tcp-"))
        {
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<State>(&bytes) else {
            continue;
        };
        // Discover old HTTP registrations so update preparation and manager stop
        // controls can still close them after the public HTTP command is removed.
        if !["tcp", "ssh", "llm", "rdp", "http"].contains(&state.mode.as_str())
            || state.port == 0
            || state.instance.is_empty()
            || state.target.is_empty()
            || display_url(&state.jump_url).is_empty()
            || key
                != if state.reverse {
                    format!(
                        "reverse-{}-{}-{}",
                        state.mode, state.remote_port, state.remote_scope
                    )
                } else {
                    registration_key(&state.mode, state.host, state.port)
                }
        {
            continue;
        }
        let usage = exchange(&state, "inspect")
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok());
        let online = usage.is_some();
        result.push(Bridge {
            key: key.into(),
            instance: state.instance.clone(),
            label: format!(
                "[{}]  {} → {}",
                if state.reverse {
                    "Reverse TCP"
                } else {
                    service_label(&state.mode)
                },
                if state.reverse {
                    state.target.clone()
                } else {
                    SocketAddr::new(state.host, state.port).to_string()
                },
                if state.reverse {
                    SocketAddr::new(state.host, state.port).to_string()
                } else {
                    state.target.clone()
                }
            ),
            state,
            usage,
            online,
        });
    }
    result.sort_by(|a, b| a.key.cmp(&b.key));
    Ok(result)
}

fn stop_registered(bridges: Vec<Bridge>) -> Result<()> {
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
        ensure!(Instant::now() < deadline, "Some bridges did not exit");
        std::thread::sleep(Duration::from_millis(50));
    }
    Ok(())
}

/// Root-level bridge control does not need a preset or gateway credentials.
pub(crate) fn stop(
    port: Option<u16>,
    host: Option<IpAddr>,
    remote_port: Option<u16>,
    remote_host: Option<String>,
) -> Result<()> {
    let selected: Vec<_> = bridges()?
        .into_iter()
        .filter(|bridge| {
            if let Some(remote_port) = remote_port {
                bridge.state.reverse
                    && bridge.state.remote_port == remote_port
                    && remote_host.as_ref().is_none_or(|host| {
                        bridge.state.target == crate::options::endpoint(host, remote_port)
                    })
            } else if let Some(port) = port {
                !bridge.state.reverse
                    && bridge.state.port == port
                    && host.is_none_or(|host| bridge.state.host == host)
            } else {
                true
            }
        })
        .collect();
    let running = selected.iter().filter(|bridge| bridge.online).count();
    stop_registered(selected)?;
    let message = if let Some(remote_port) = remote_port {
        format!("Reverse bridges stopped on remote port {remote_port} ({running})")
    } else {
        match (port, host, running) {
            (Some(port), Some(host), 0) => format!(
                "No managed bridge running on {}",
                SocketAddr::new(host, port)
            ),
            (Some(port), Some(host), _) => {
                format!("Bridge stopped on {}", SocketAddr::new(host, port))
            }
            (Some(port), None, 0) => format!("No managed bridge running on port {port}"),
            (Some(port), None, _) => format!("Bridge stopped on port {port}"),
            (None, _, _) => format!("All bridges stopped ({running})"),
        }
    };
    crate::console::line(&message);
    Ok(())
}

pub(crate) fn stop_all() -> Result<()> {
    stop_registered(bridges()?)?;
    crate::tray::shutdown()
}

#[cfg(test)]
mod control_tests {
    use super::*;
    use std::sync::Arc;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn reverse_keys_distinguish_remote_servers_and_listeners_without_changing_forward_keys() {
        #[derive(clap::Parser)]
        struct Options {
            #[command(flatten)]
            bridge: BridgeOptions,
        }
        let mut args = <Options as clap::Parser>::parse_from([
            "test",
            "https://gateway.example/",
            "--port",
            "8000",
            "--remote-port",
            "18000",
        ])
        .bridge;
        args.mode = "tcp".into();
        let forward = key(&args, 8000);
        assert_eq!(forward, "tcp-8000");
        args.reverse = true;
        let original = key(&args, 8000);
        assert_ne!(original, forward);
        args.port = Some(9000);
        assert_eq!(
            key(&args, 9000),
            original,
            "listener ownership must detect changed local destinations"
        );
        args.server_host = Some("other-server".into());
        assert_ne!(key(&args, 9000), original);
        args.server_host = None;
        args.remote_host = "127.0.0.2".into();
        assert_ne!(key(&args, 9000), original);
    }
    #[test]
    fn legacy_records_default_to_loopback_and_custom_addresses_have_distinct_keys() {
        let state: State = serde_json::from_value(serde_json::json!({
            "pid":1,"control":1,"token":"token","identity":"id","username":"u",
            "jump_url":"https://gateway.example", "mode":"ssh", "port":2222,
            "target":"127.0.0.1:22", "version":"5.0.5", "executable":"exe", "instance":"i", "log":null
        })).unwrap();
        assert_eq!(state.host, crate::options::default_host());
        assert_eq!(registration_key("ssh", state.host, 2222), "ssh-2222");
        let v6: IpAddr = "::1".parse().unwrap();
        assert_eq!(registration_key("ssh", v6, 2222), "ssh-2222-__1");
        assert_ne!(
            registration_key("ssh", v6, 2222),
            registration_key("ssh", state.host, 2222)
        );
        let bridge = Bridge {
            state: State { host: v6, ..state },
            key: String::new(),
            instance: String::new(),
            label: String::new(),
            usage: None,
            online: false,
        };
        assert_eq!(bridge.local_url(), "ssh://[::1]:2222");
        assert_eq!(crate::options::endpoint("::1", 22), "[::1]:22");
    }
    #[test]
    fn display_urls_keep_connection_navigation_and_remove_secrets() {
        assert_eq!(
            display_url(
                "https://alice:password@guac.example/guacamole/?token=secret#/client/MQBjAG15c3Fs?token=hidden"
            ),
            "https://guac.example/guacamole/#/client/MQBjAG15c3Fs"
        );
        assert_eq!(
            display_url("https://guac.example/#token=secret"),
            "https://guac.example/"
        );
        assert!(display_url("invalid").is_empty());
        assert!(display_url("file:///private").is_empty());
    }

    #[test]
    fn connection_table_aligns_unicode_display_columns() {
        use unicode_width::UnicodeWidthStr;
        let rows = [
            [
                "SSH",
                "Guacamole",
                "Running",
                "用户😀",
                "ssh://127.0.0.1:2222 → https://gateway.example",
            ]
            .map(str::to_owned),
            [
                "HTTP",
                "FortiGate",
                "Unresponsive",
                "Müller",
                "https://127.0.0.1:8080 → https://gateway.example",
            ]
            .map(str::to_owned),
        ];
        let table = connection_table(["TYPE", "GATEWAY", "STATUS", "USER", "ROUTE"], &rows);
        let widths: Vec<_> = table.lines().map(UnicodeWidthStr::width).collect();
        assert!(widths.iter().all(|width| *width == widths[0]));
        let columns: Vec<Vec<usize>> = table
            .lines()
            .filter(|line| line.starts_with('│'))
            .map(|line| {
                line.char_indices()
                    .filter(|(_, c)| *c == '│')
                    .map(|(at, _)| line[..at].width())
                    .collect()
            })
            .collect();
        assert!(columns.iter().all(|positions| positions == &columns[0]));
        assert!(table.contains("用户😀"));
    }
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
    async fn authenticated_inspection_and_control() {
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
}
