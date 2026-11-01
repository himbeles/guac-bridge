//! Independent native client: Guacamole login, WebSocket, ESC-safe mux, loopback TCP.
use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD as B64, STANDARD_NO_PAD},
};
use clap::Parser as ClapParser;
use futures_util::{SinkExt, StreamExt};
use guac_ssh_rust_prototype::{
    Decoder, FRAME, Frame, Kind, MAX_FRAME, Parser, WINDOW, encode, instruction, shell_quote,
};
use serde_json::json;
use std::{
    collections::{HashMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU32, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{Notify, Semaphore, mpsc, oneshot, watch},
    task::JoinHandle,
};
use tokio_tungstenite::{
    Connector,
    tungstenite::{Message, client::IntoClientRequest},
};
use url::Url;

#[derive(ClapParser)]
#[command(version, about = "Experimental native Guacamole SSH bridge")]
struct Args {
    #[arg(long)]
    url: String,
    #[arg(long, alias = "user")]
    username: Option<String>,
    #[arg(long)]
    connection_id: Option<String>,
    #[arg(long)]
    data_source: Option<String>,
    #[arg(long, default_value_t = 2222)]
    port: u16,
    #[arg(long, default_value = "~/.local/bin/guac-ssh-server-rs")]
    server_command: String,
    #[arg(long, default_value = "127.0.0.1")]
    target_host: String,
    #[arg(long, default_value_t = 22)]
    target_port: u16,
    #[arg(long, default_value_t = 30)]
    connect_timeout: u64,
    #[arg(long, default_value_t = 1.0)]
    startup_delay: f64,
    #[arg(long, default_value_t = 8)]
    max_connections: usize,
    /// Data frames in flight per channel; more credits keep delayed links busy.
    #[arg(long,default_value_t=WINDOW)]
    window: usize,
    /// Maximum TCP bytes per frame; larger frames reduce overhead but buffer more.
    #[arg(long,default_value_t=FRAME)]
    frame_size: usize,
    /// Guacamole input blobs in flight before waiting for acknowledgements.
    #[arg(long, default_value_t = 64)]
    input_window: usize,
    /// Bytes per input blob, limited to 6048 by Guacamole's instruction size.
    #[arg(long, default_value_t = 6048)]
    input_chunk: usize,
    #[arg(long)]
    ca_file: Option<String>,
    /// Test harness IPC on stdin/stdout; no credentials are written to output.
    #[arg(long, hide = true)]
    benchmark_control: bool,
}
struct Channel {
    queue: mpsc::Sender<Frame>,
    credits: Arc<Semaphore>,
}
type Channels = Arc<Mutex<HashMap<u32, Channel>>>;
type Failure = watch::Sender<Option<String>>;
struct Acks {
    slots: Arc<Semaphore>,
    pending: Mutex<VecDeque<Instant>>,
    window: usize,
}
enum Input {
    Raw(Vec<u8>),
    Event(Frame),
    Shutdown(oneshot::Sender<()>),
}
fn fail(failure: &Failure, closing: &AtomicBool, message: &str) {
    if !closing.load(Ordering::Relaxed) && failure.borrow().is_none() {
        failure.send_replace(Some(message.to_owned()));
    }
}
fn spawn_guard<F>(future: F, failure: Failure, closing: Arc<AtomicBool>) -> JoinHandle<()>
where
    F: std::future::Future<Output = Result<()>> + Send + 'static,
{
    tokio::spawn(async move {
        if let Err(error) = future.await {
            fail(&failure, &closing, &error.to_string());
        }
    })
}
async fn ws_send(tx: &mpsc::Sender<Message>, args: &[&str]) -> Result<()> {
    tx.send(Message::Text(instruction(args).into()))
        .await
        .map_err(|_| anyhow!("WebSocket writer stopped"))
}
async fn raw_input(
    bytes: &[u8],
    chunk: usize,
    acks: &Acks,
    tx: &mpsc::Sender<Message>,
    timeout: Duration,
) -> Result<()> {
    for part in bytes.chunks(chunk) {
        tokio::time::timeout(timeout, acks.slots.clone().acquire_owned())
            .await??
            .forget();
        acks.pending.lock().unwrap().push_back(Instant::now());
        ws_send(tx, &["blob", "0", &B64.encode(part)]).await?;
    }
    Ok(())
}
async fn drain(acks: &Acks, timeout: Duration) -> Result<()> {
    let permit = tokio::time::timeout(
        timeout,
        acks.slots.clone().acquire_many_owned(acks.window as u32),
    )
    .await??;
    drop(permit);
    Ok(())
}
async fn input_sender(
    mut input: mpsc::Receiver<Input>,
    acks: Arc<Acks>,
    out: mpsc::Sender<Message>,
    args: Arc<Args>,
) -> Result<()> {
    let timeout = Duration::from_secs(args.connect_timeout);
    while let Some(message) = input.recv().await {
        let (bytes, done, chunk) = match message {
            Input::Raw(data) => (data, None, 3072),
            Input::Event(frame) => (encode(&frame)?, None, args.input_chunk),
            Input::Shutdown(done) => (
                encode(&Frame::control(Kind::Shutdown, 0))?,
                Some(done),
                args.input_chunk,
            ),
        };
        raw_input(&bytes, chunk, &acks, &out, timeout).await?;
        if let Some(done) = done {
            drain(&acks, timeout).await?;
            let _ = done.send(());
            return Ok(());
        }
    }
    Ok(())
}
async fn channel(
    id: u32,
    stream: TcpStream,
    mut queue: mpsc::Receiver<Frame>,
    credits: Arc<Semaphore>,
    input: mpsc::Sender<Input>,
    channels: Channels,
    args: Arc<Args>,
) -> Result<()> {
    let (mut reader, mut writer) = stream.into_split();
    let remote_closed = AtomicBool::new(false);
    let result: Result<()> = async {
        input
            .send(Input::Event(Frame::control(Kind::Open, id)))
            .await?;
        let opened = tokio::time::timeout(Duration::from_secs(args.connect_timeout), queue.recv())
            .await?
            .ok_or_else(|| anyhow!("Channel ended before open"))?;
        ensure!(
            opened.kind == Kind::Opened,
            "Remote target refused the TCP connection"
        );
        let upload = async {
            let mut buffer = vec![0; args.frame_size];
            loop {
                let permit = credits.clone().acquire_owned().await?;
                let n = reader.read(&mut buffer).await?;
                if n == 0 {
                    drop(permit);
                    input
                        .send(Input::Event(Frame::control(Kind::Eof, id)))
                        .await?;
                    return Ok::<(), anyhow::Error>(());
                }
                permit.forget();
                input
                    .send(Input::Event(Frame::payload(
                        Kind::Data,
                        id,
                        buffer[..n].to_vec(),
                    )))
                    .await?;
            }
        };
        let download = async {
            while let Some(frame) = queue.recv().await {
                match frame.kind {
                    Kind::Data => {
                        writer.write_all(&frame.data).await?;
                        input
                            .send(Input::Event(Frame::control(Kind::Credit, id)))
                            .await?;
                    }
                    Kind::Eof | Kind::Closed => {
                        remote_closed.store(frame.kind == Kind::Closed, Ordering::Relaxed);
                        writer.shutdown().await?;
                        return Ok::<(), anyhow::Error>(());
                    }
                    Kind::Error => {
                        remote_closed.store(true, Ordering::Relaxed);
                        bail!("Remote target connection failed");
                    }
                    _ => bail!("Unexpected TCP event"),
                }
            }
            bail!("Bridge disconnected during TCP transfer")
        };
        tokio::try_join!(upload, download)?;
        Ok(())
    }
    .await;
    if result.is_err() {
        let _ = socket2::SockRef::from(writer.as_ref()).set_linger(Some(Duration::ZERO));
    }
    if !remote_closed.load(Ordering::Relaxed) {
        let cleanup = async {
            input
                .send(Input::Event(Frame::control(Kind::Close, id)))
                .await?;
            while let Some(frame) = queue.recv().await {
                if matches!(frame.kind, Kind::Closed | Kind::Error) {
                    return Ok::<(), anyhow::Error>(());
                }
            }
            bail!("No channel close confirmation")
        };
        if !matches!(
            tokio::time::timeout(Duration::from_secs(5), cleanup).await,
            Ok(Ok(()))
        ) {
            channels.lock().unwrap().remove(&id);
            bail!("Channel close timed out");
        }
    }
    channels.lock().unwrap().remove(&id);
    result
}

fn parse_url(args: &Args) -> Result<(Url, String, String, String)> {
    let mut base = Url::parse(&args.url)?;
    ensure!(
        ["http", "https"].contains(&base.scheme())
            && base.host_str().is_some()
            && base.username().is_empty()
            && base.password().is_none(),
        "Supply an HTTP(S) Guacamole URL without embedded credentials"
    );
    let mut connection = args.connection_id.clone();
    let mut source = args.data_source.clone();
    let mut kind = "c".to_owned();
    if let Some((_, encoded)) = base.fragment().and_then(|text| text.split_once("/client/")) {
        let text = percent_encoding::percent_decode_str(encoded.split('?').next().unwrap())
            .decode_utf8()?;
        let decoded = B64
            .decode(text.as_bytes())
            .or_else(|_| STANDARD_NO_PAD.decode(text.as_bytes()))?;
        let decoded = String::from_utf8(decoded)?;
        let values: Vec<_> = decoded.split('\0').collect();
        ensure!(values.len() == 3, "Invalid Guacamole client URL");
        connection = connection.or_else(|| Some(values[0].into()));
        kind = values[1].into();
        source = source.or_else(|| Some(values[2].into()));
    }
    ensure!(kind == "c" || kind == "g", "Invalid connection type");
    base.set_fragment(None);
    base.set_query(None);
    base.set_path(&(base.path().trim_end_matches('/').to_owned() + "/"));
    Ok((
        base,
        connection.context("URL needs #/client/... or --connection-id")?,
        kind,
        source.context("URL needs a data source")?,
    ))
}
fn helper_command(args: &Args) -> Result<Vec<u8>> {
    let executable = if let Some(path) = args.server_command.strip_prefix("~/") {
        format!("\"$HOME\"/{}", shell_quote(path))
    } else {
        shell_quote(&args.server_command)
    };
    let command = format!(
        "exec {executable} --stdio --target-host {} --target-port {} --connect-timeout {} --max-connections {} --window {} --frame-size {}\n",
        shell_quote(&args.target_host),
        args.target_port,
        args.connect_timeout,
        args.max_connections,
        args.window,
        args.frame_size
    );
    ensure!(
        command.len() < 3900,
        "Helper command exceeds terminal line limit"
    );
    Ok(command.into_bytes())
}
#[cfg(unix)]
fn usage() -> Result<(f64, i64)> {
    unsafe {
        let mut value: libc::rusage = std::mem::zeroed();
        ensure!(
            libc::getrusage(libc::RUSAGE_SELF, &mut value) == 0,
            "CPU counter unavailable"
        );
        let seconds = value.ru_utime.tv_sec as f64
            + value.ru_stime.tv_sec as f64
            + (value.ru_utime.tv_usec + value.ru_stime.tv_usec) as f64 / 1e6;
        Ok((seconds, value.ru_maxrss))
    }
}
#[cfg(not(unix))]
fn usage() -> Result<(f64, i64)> {
    bail!("Benchmark counters require Unix; ordinary bridging is available")
}
async fn benchmark_control(port: u16) -> Result<()> {
    println!("{}", json!({"endpoint":port}));
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let (mut cpu, mut wall) = (0.0, Instant::now());
    while let Some(command) = lines.next_line().await? {
        match command.trim() {
            "start" => {
                cpu = usage()?.0;
                wall = Instant::now();
                println!("{{}}");
            }
            "stop" => {
                let (now, rss) = usage()?;
                println!(
                    "{}",
                    json!({"client_cpu_s":now-cpu,"wall_s":wall.elapsed().as_secs_f64(),"client_peak_rss_kib":rss})
                );
            }
            "quit" => return Ok(()),
            _ => bail!("Invalid benchmark command"),
        }
    }
    Ok(())
}

async fn serve(args: Arc<Args>) -> Result<()> {
    let (base, connection, kind, source) = parse_url(&args)?;
    let username = match &args.username {
        Some(name) => name.clone(),
        None => {
            use std::io::Write;
            eprint!("Guacamole username: ");
            std::io::stderr().flush()?;
            let mut name = String::new();
            std::io::stdin().read_line(&mut name)?;
            name.trim().to_owned()
        }
    };
    let password = match std::env::var("GUAC_BRIDGE_PASSWORD") {
        Ok(password) => password,
        Err(_) => rpassword::prompt_password("Guacamole password: ")?,
    };
    let mut http = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(args.connect_timeout));
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_native_certs::load_native_certs().certs {
        let _ = roots.add(cert);
    }
    if let Some(path) = &args.ca_file {
        let pem = std::fs::read(path)?;
        for cert in rustls_pemfile::certs(&mut &pem[..]) {
            roots.add(cert?)?;
        }
        http = http.add_root_certificate(reqwest::Certificate::from_pem(&pem)?);
    }
    let http = http.build()?;
    let response = http
        .post(base.join("api/tokens")?)
        .form(&[("username", username), ("password", password)])
        .send()
        .await
        .map_err(|_| anyhow!("Guacamole login request failed"))?;
    ensure!(
        response.status().is_success(),
        "Guacamole login rejected (HTTP {})",
        response.status()
    );
    let login: serde_json::Value = response
        .json()
        .await
        .context("Invalid Guacamole login response")?;
    let token = login["authToken"]
        .as_str()
        .context("No authentication token; external SSO/MFA is not supported by this prototype")?
        .to_owned();
    let mut ws_url = base.join("websocket-tunnel")?;
    ws_url
        .set_scheme(if base.scheme() == "https" {
            "wss"
        } else {
            "ws"
        })
        .unwrap();
    ws_url.query_pairs_mut().extend_pairs([
        ("token", token.as_str()),
        ("GUAC_DATA_SOURCE", source.as_str()),
        ("GUAC_ID", connection.as_str()),
        ("GUAC_TYPE", kind.as_str()),
        ("GUAC_WIDTH", "1024"),
        ("GUAC_HEIGHT", "768"),
        ("GUAC_DPI", "96"),
        ("GUAC_TIMEZONE", "Europe/Berlin"),
        ("GUAC_IMAGE", "image/png"),
    ]);
    let mut request = ws_url.as_str().into_client_request()?;
    request
        .headers_mut()
        .insert("Sec-WebSocket-Protocol", "guacamole".parse()?);
    request
        .headers_mut()
        .insert("Origin", base.origin().ascii_serialization().parse()?);
    let tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let (socket, _) = tokio::time::timeout(
        Duration::from_secs(args.connect_timeout),
        tokio_tungstenite::connect_async_tls_with_config(
            request,
            None,
            true,
            Some(Connector::Rustls(Arc::new(tls))),
        ),
    )
    .await?
    .map_err(|_| anyhow!("Guacamole WebSocket connection failed"))?;
    let (mut sink, mut stream) = socket.split();
    let (out, mut outbound) = mpsc::channel::<Message>(128);
    let (input, inputs) = mpsc::channel(64);
    let acks = Arc::new(Acks {
        slots: Arc::new(Semaphore::new(args.input_window)),
        pending: Mutex::new(VecDeque::new()),
        window: args.input_window,
    });
    let channels: Channels = Arc::new(Mutex::new(HashMap::new()));
    let (failure, mut failed) = watch::channel(None);
    let closing = Arc::new(AtomicBool::new(false));
    let synced = Arc::new(Notify::new());
    let ready = Arc::new(Notify::new());
    let mut tasks = Vec::new();
    tasks.push(spawn_guard(
        async move {
            while let Some(message) = outbound.recv().await {
                sink.send(message)
                    .await
                    .map_err(|_| anyhow!("WebSocket write failed"))?;
            }
            Ok(())
        },
        failure.clone(),
        closing.clone(),
    ));
    let (read_out, read_acks, read_channels, read_args, read_ready, read_synced, read_closing) = (
        out.clone(),
        acks.clone(),
        channels.clone(),
        args.clone(),
        ready.clone(),
        synced.clone(),
        closing.clone(),
    );
    tasks.push(spawn_guard(async move {
        let mut parser=Parser::default();let mut decoder=Decoder::default();let mut output_stream=None;
        let mut heartbeat=Instant::now();let mut tick=tokio::time::interval(Duration::from_secs(1));
        loop {
            tokio::select!{
                _=tick.tick()=>{
                    ensure!(heartbeat.elapsed()<Duration::from_secs(45),"Remote helper heartbeat lost");
                    let timed_out=read_acks.pending.lock().unwrap().front().is_some_and(|time|time.elapsed()>Duration::from_secs(read_args.connect_timeout));
                    ensure!(!timed_out,"Guacamole input acknowledgement timed out");
                },
                message=stream.next()=>{
                    let Some(message)=message else {bail!("Guacamole tunnel closed");};
                    let message=message.map_err(|_|anyhow!("WebSocket read failed"))?;
                    match message {
                        Message::Ping(bytes)=>{read_out.send(Message::Pong(bytes)).await?;continue;},
                        Message::Close(_)=>bail!("Guacamole tunnel closed"),
                        Message::Text(text)=>for values in parser.feed(&text)?{
                            let op=values.first().map(String::as_str).unwrap_or("");
                            match op {
                                "sync"=>{ensure!(values.len()>=2,"Invalid sync");ws_send(&read_out,&["sync",&values[1]]).await?;read_synced.notify_one();},
                                "ack" if values.get(1).map(String::as_str)==Some("0")=>{
                                    ensure!(values.get(3).map(String::as_str)==Some("0"),"Guacamole rejected terminal input");
                                    ensure!(read_acks.pending.lock().unwrap().pop_front().is_some(),"Unexpected input ACK");
                                    read_acks.slots.add_permits(1);
                                },
                                "pipe"=>{
                                    ensure!(values.len()>=4,"Invalid pipe");
                                    if values[3]=="LLM"&&output_stream.is_none(){output_stream=Some(values[1].clone());ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;}
                                    else{ws_send(&read_out,&["ack",&values[1],"Unsupported","256"]).await?;}
                                },
                                "blob" if values.get(1)==output_stream.as_ref()=>{
                                    ensure!(values.len()>=3,"Invalid blob");
                                    let data=B64.decode(&values[2])?;
                                    for frame in decoder.feed(&data)?{
                                        heartbeat=Instant::now();
                                        match frame.kind {
                                            Kind::Ready=>{
                                                ensure!(frame.id==0&&frame.data.len()==8,"Invalid helper ready frame");
                                                let window=u32::from_be_bytes(frame.data[..4].try_into()?) as usize;
                                                let size=u32::from_be_bytes(frame.data[4..].try_into()?) as usize;
                                                ensure!(window==read_args.window&&size==read_args.frame_size,"Helper transport settings do not match");
                                                read_ready.notify_one();
                                            },
                                            Kind::Heartbeat=>(),
                                            Kind::Credit=>{if let Some(channel)=read_channels.lock().unwrap().get(&frame.id){
                                                ensure!(channel.credits.available_permits()<read_args.window,"Excess channel credits");channel.credits.add_permits(1);
                                            }},
                                            _=>{
                                                ensure!(frame.kind!=Kind::Data||frame.data.len()<=read_args.frame_size,"TCP frame exceeds limit");
                                                if let Some(channel)=read_channels.lock().unwrap().get(&frame.id){channel.queue.try_send(frame)?;}
                                            },
                                        }
                                    }
                                    ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                },
                                "end" if values.get(1)==output_stream.as_ref()=>{
                                    ensure!(read_closing.load(Ordering::Relaxed),"Remote helper output pipe closed");output_stream=None;
                                },
                                "error"|"disconnect"=>bail!("Guacamole disconnected or reported a terminal error"),
                                "img"|"file"|"audio"|"video"|"body" if values.len()>1=>ws_send(&read_out,&["ack",&values[1],"Not needed","256"]).await?,
                                _=>(),
                            }
                        },
                        _=>(),
                    }
                }
            }
        }
    },failure.clone(),closing.clone()));
    tasks.push(spawn_guard(
        input_sender(inputs, acks.clone(), out.clone(), args.clone()),
        failure.clone(),
        closing.clone(),
    ));
    let result:Result<()>=async {
        let startup=async {
            synced.notified().await;
            tokio::time::sleep(Duration::from_secs_f64(args.startup_delay)).await;
            acks.slots.clone().acquire_owned().await?.forget();
            acks.pending.lock().unwrap().push_back(Instant::now());
            ws_send(&out,&["pipe","0","text/plain","STDIN"]).await?;
            drain(&acks,Duration::from_secs(args.connect_timeout)).await?;
            input.send(Input::Raw(helper_command(&args)?)).await?;
            ready.notified().await;
            Ok::<(),anyhow::Error>(())
        };
        tokio::select!{
            result=tokio::time::timeout(Duration::from_secs(args.connect_timeout),startup)=>result.context("Helper did not become ready; check native helper installation and --server-command")??,
            _=failed.changed()=>bail!("{}",failed.borrow().as_deref().unwrap_or("Bridge failed")),
        }
        let listener=TcpListener::bind(("127.0.0.1",args.port)).await.context("Cannot bind local port; it may be in use or reserved")?;
        let port=listener.local_addr()?.port();
        if !args.benchmark_control{eprintln!("Native SSH bridge ready on 127.0.0.1:{port}; Ctrl+C to stop");}
        let control=async {if args.benchmark_control{benchmark_control(port).await}else{tokio::signal::ctrl_c().await?;Ok(())}};
        tokio::pin!(control);
        let next_id=AtomicU32::new(1);
        let mut connections=tokio::task::JoinSet::new();
        let mut refresh=tokio::time::interval(Duration::from_secs(60));refresh.tick().await;
        loop {
            tokio::select!{
                result=&mut control=>{result?;break;},
                _=failed.changed()=>bail!("{}",failed.borrow().as_deref().unwrap_or("Bridge failed")),
                _=refresh.tick(),if kind=="c"=>{
                    let mut url=base.clone();url.path_segments_mut().map_err(|_|anyhow!("Invalid refresh URL"))?
                        .pop_if_empty().extend(["api","session","data",&source,"connections",&connection]);
                    let response=http.get(url).query(&[("token",&token)]).timeout(Duration::from_secs(15)).send().await.map_err(|_|anyhow!("Session refresh failed"))?;
                    ensure!(response.status().is_success(),"Guacamole session refresh rejected");
                },
                accepted=listener.accept()=>{
                    let (stream,_)=accepted?;
                    stream.set_nodelay(true)?;
                    if channels.lock().unwrap().len()>=args.max_connections{drop(stream);continue;}
                    let id=next_id.fetch_add(1,Ordering::Relaxed);ensure!(id>0,"Channel ID space exhausted; restart bridge");
                    let (tx,rx)=mpsc::channel(args.window+4);let credits=Arc::new(Semaphore::new(args.window));
                    channels.lock().unwrap().insert(id,Channel{queue:tx,credits:credits.clone()});
                    let (input,channels,args)=(input.clone(),channels.clone(),args.clone());
                    connections.spawn(async move {if let Err(error)=channel(id,stream,rx,credits,input,channels,args).await{eprintln!("TCP channel {id}: {error}");}});
                },
                _=connections.join_next(),if !connections.is_empty()=>(),
            }
        }
        connections.abort_all();while connections.join_next().await.is_some(){}
        Ok(())
    }.await;
    closing.store(true, Ordering::Relaxed);
    let (done, wait) = oneshot::channel();
    let shutdown = async {
        input.send(Input::Shutdown(done)).await?;
        wait.await?;
        Ok::<(), anyhow::Error>(())
    };
    let _ = tokio::time::timeout(Duration::from_secs(3), shutdown).await;
    let _ = tokio::time::timeout(Duration::from_secs(1), out.send(Message::Close(None))).await;
    for task in &tasks {
        task.abort();
    }
    for task in tasks {
        let _ = task.await;
    }
    result
}
fn main() -> Result<()> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    let args = Args::parse();
    ensure!(
        (1..=128).contains(&args.window)
            && (1..=128).contains(&args.input_window)
            && (512..=6048).contains(&args.input_chunk),
        "Invalid window/input chunk (maximum 6048 bytes for Guacamole)"
    );
    ensure!(
        (4096..=MAX_FRAME).contains(&args.frame_size)
            && (1..=16).contains(&args.max_connections)
            && args.connect_timeout > 0,
        "Invalid transport settings"
    );
    ensure!(
        args.startup_delay.is_finite() && args.startup_delay >= 0.0 && args.target_port > 0,
        "Invalid delay or target port"
    );
    ensure!(
        ["127.0.0.1", "localhost", "::1"].contains(&args.target_host.as_str()),
        "Target must be loopback"
    );
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(serve(Arc::new(args)));
    runtime.shutdown_timeout(Duration::from_millis(100));
    result
}
