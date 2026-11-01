//! Independent native client: Guacamole login, WebSocket, ESC-safe mux, loopback TCP.
use crate::cli::Args;
use crate::{Decoder, Frame, Kind, Parser, encode, instruction};
use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD as B64, STANDARD_NO_PAD},
};
use futures_util::{SinkExt, StreamExt};
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
    Upload(Vec<u8>, oneshot::Sender<()>),
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
            fail(&failure, &closing, &format!("{error:#}"));
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
        let (bytes, done, chunk, shutdown) = match message {
            Input::Raw(data) => (data, None, 3072, false),
            Input::Upload(data, done) => (data, Some(done), args.input_chunk, false),
            Input::Event(frame) => (encode(&frame)?, None, args.input_chunk, false),
            Input::Shutdown(done) => (
                encode(&Frame::control(Kind::Shutdown, 0))?,
                Some(done),
                args.input_chunk,
                true,
            ),
        };
        raw_input(&bytes, chunk, &acks, &out, timeout)
            .await
            .context("Guacamole terminal input stopped progressing")?;
        if let Some(done) = done {
            drain(&acks, timeout)
                .await
                .context("Guacamole did not acknowledge terminal input")?;
            let _ = done.send(());
            if shutdown {
                return Ok(());
            }
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
            let mut buffer = vec![0; args.frame_size as usize];
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
            // On a clean half-close, wait for the helper to flush queued data
            // and confirm Closed. A premature Close would abort its writer.
            if result.is_err() {
                input
                    .send(Input::Event(Frame::control(Kind::Close, id)))
                    .await?;
            }
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
    let mut base = Url::parse(args.url.as_deref().context("--url is required")?)?;
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

pub(crate) async fn serve(args: Arc<Args>) -> Result<()> {
    let (base, connection, kind, source) = parse_url(&args)?;
    #[cfg(unix)]
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    #[cfg(windows)]
    let mut interrupt = tokio::signal::windows::ctrl_c()?;
    let listener=TcpListener::bind(("127.0.0.1",args.port())).await.with_context(|| format!("Cannot listen on 127.0.0.1:{}: the port is already in use or reserved. Choose another --port, or stop the process using it",args.port()))?;

    let username = crate::credentials::username(&args)?;
    let password = crate::credentials::password(&args, &username)?;
    let mut http = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(args.connect_timeout))
        .timeout(Duration::from_secs(args.connect_timeout));
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_native_certs::load_native_certs().certs {
        let _ = roots.add(cert);
    }
    if let Some(path) = &args.ca_file {
        let pem = std::fs::read(path)?;
        for cert in rustls_pemfile::certs(&mut &pem[..]) {
            roots.add(cert?)?;
        }
        for cert in reqwest::Certificate::from_pem_bundle(&pem)? {
            http = http.add_root_certificate(cert);
        }
    }
    if args.insecure {
        eprintln!("\x1b[33mTLS verification disabled by --insecure\x1b[0m");
        http = http.danger_accept_invalid_certs(true);
    }
    let http = http.build()?;
    let response = http
        .post(base.join("api/tokens")?)
        .form(&[
            ("username", username.as_str()),
            ("password", password.as_str()),
        ])
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
        .context("No authentication token; this login requires an external SSO/MFA flow")?
        .to_owned();
    if args.save_password {
        crate::credentials::save(&args, &username, &password)?;
    }
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
    let mut tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    if args.insecure {
        tls.dangerous()
            .set_certificate_verifier(Arc::new(InsecureVerifier));
    }
    let (socket, _) = tokio::time::timeout(Duration::from_secs(args.connect_timeout), async {
        let stream = crate::proxy::connect(&ws_url).await?;
        tokio_tungstenite::client_async_tls_with_config(
            request,
            stream,
            None,
            Some(Connector::Rustls(Arc::new(tls))),
        )
        .await
        .map_err(|_| {
            anyhow!("Guacamole WebSocket connection failed; check proxy and certificate trust")
        })
    })
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
    let helper_started = Arc::new(AtomicBool::new(false));
    let read_started = helper_started.clone();
    let (boot_tx, mut boot_rx) = mpsc::channel::<String>(8);
    let nonce = uuid::Uuid::new_v4().simple().to_string();
    let read_nonce = nonce.clone();
    let phase = Arc::new(Mutex::new("preparing terminal"));
    let read_phase = phase.clone();
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
        let mut parser=Parser::default();let mut decoder=Decoder::default();let mut output_stream=None;let mut boot_stream=None;let mut boot_buffer=Vec::new();
        let mut heartbeat=Instant::now();let mut tick=tokio::time::interval(Duration::from_secs(1));
        loop {
            tokio::select!{
                _=tick.tick()=>{
                    ensure!(!read_started.load(Ordering::Relaxed)||heartbeat.elapsed()<Duration::from_secs(45),"Remote helper heartbeat lost");
                    let timed_out=read_acks.pending.lock().unwrap().front().is_some_and(|time|time.elapsed()>Duration::from_secs(read_args.connect_timeout));
                    ensure!(!timed_out,"Guacamole input acknowledgement timed out while {}",if read_started.load(Ordering::Relaxed){"forwarding data"}else{*read_phase.lock().unwrap()});
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
                                    if values[3]=="BOOT"&&boot_stream.is_none(){boot_stream=Some(values[1].clone());ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;}
                                    else if values[3]=="LLM"&&output_stream.is_none(){output_stream=Some(values[1].clone());ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;}
                                    else{ws_send(&read_out,&["ack",&values[1],"Unsupported","256"]).await?;}
                                },
                                "blob" if values.get(1)==boot_stream.as_ref()=>{
                                    ensure!(values.len()>=3,"Invalid bootstrap blob");
                                    boot_buffer.extend(B64.decode(&values[2])?);
                                    ensure!(boot_buffer.len()<4096,"Bootstrap output exceeds limit");
                                    while let Some(n)=boot_buffer.iter().position(|b|*b==b'\n') {
                                        let line=String::from_utf8(boot_buffer.drain(..=n).collect())?;
                                        let line=line.trim();
                                        if let Some(message)=line.strip_prefix(&format!("{read_nonce}:ERROR:")) {
                                            bail!("Server helper bootstrap failed: {message}");
                                        }
                                        if line==format!("{read_nonce}:VERIFY") {
                                            *read_phase.lock().unwrap()="verifying helper checksum";
                                            if !read_args.benchmark_control {eprintln!("Verifying helper checksum…");}
                                        }else if line==format!("{read_nonce}:STARTING") {
                                            *read_phase.lock().unwrap()="starting server helper";
                                            if !read_args.benchmark_control {eprintln!("Starting server helper…");}
                                        }else {
                                            boot_tx.try_send(line.to_owned())?;
                                        }
                                    }
                                    ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                },
                                "end" if values.get(1)==boot_stream.as_ref()=>{boot_stream=None;},
                                "blob" if values.get(1)==output_stream.as_ref()=>{
                                    ensure!(values.len()>=3,"Invalid blob");
                                    let data=B64.decode(&values[2])?;
                                    for frame in decoder.feed(&data)?{
                                        heartbeat=Instant::now();
                                        match frame.kind {
                                            Kind::Ready=>{
                                                ensure!(frame.id==0,"Invalid helper ready frame");
                                                let meta:serde_json::Value=serde_json::from_slice(&frame.data)?;
                                                ensure!(meta["package_version"]==env!("CARGO_PKG_VERSION"),"Bundled helper version does not match client");
                                                ensure!(meta["window"]==read_args.window && meta["frame_size"]==read_args.frame_size,"Helper transport settings do not match");
                                                read_started.store(true,Ordering::Relaxed);
                                                read_ready.notify_one();
                                            },
                                            Kind::Heartbeat=>(),
                                            Kind::Credit=>{if let Some(channel)=read_channels.lock().unwrap().get(&frame.id){
                                                ensure!(channel.credits.available_permits()<read_args.window,"Excess channel credits");channel.credits.add_permits(1);
                                            }},
                                            _=>{
                                                ensure!(frame.kind!=Kind::Data||frame.data.len()<=read_args.frame_size as usize,"TCP frame exceeds limit");
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
            tokio::time::timeout(Duration::from_secs(args.connect_timeout),synced.notified()).await?;
            tokio::time::sleep(Duration::from_secs_f64(args.startup_delay)).await;
            acks.slots.clone().acquire_owned().await?.forget();
            acks.pending.lock().unwrap().push_back(Instant::now());
            ws_send(&out,&["pipe","0","text/plain","STDIN"]).await?;
            drain(&acks,Duration::from_secs(args.connect_timeout)).await?;
            input.send(Input::Raw(crate::deploy::prepare(&nonce))).await?;
            let prepared=tokio::time::timeout(Duration::from_secs(args.connect_timeout),boot_rx.recv()).await?.context("Terminal preparation closed")?;
            ensure!(prepared==format!("{nonce}:PREPARED"),"Unexpected terminal preparation response");
            *phase.lock().unwrap()="checking server helper cache";
            if !args.benchmark_control {eprintln!("Checking server helper…");}
            input.send(Input::Raw(crate::deploy::command(&args,&nonce)?)).await?;
            if args.server_command.is_none() {
                let response=tokio::time::timeout(Duration::from_secs(args.connect_timeout),boot_rx.recv()).await?.context("Helper bootstrap closed")?;
                if response==format!("{nonce}:UPLOAD") {
                    *phase.lock().unwrap()="uploading bundled helper";
                    let encoded=crate::deploy::encoded_agent();
                    let block=args.input_chunk*args.input_window;
                    if !args.benchmark_control {eprintln!("Uploading bundled helper ({} KiB)…",crate::deploy::AGENT.len().div_ceil(1024));}
                    for (index,part) in encoded.chunks(block).enumerate() {
                        let (sent,receipt)=oneshot::channel();
                        input.send(Input::Upload(part.to_vec(),sent)).await?;
                        receipt.await.context("Helper upload sender stopped")?;
                        let percent=((index+1)*block).min(encoded.len())*100/encoded.len();
                        if !args.benchmark_control {eprintln!("Helper upload: {percent}%");}
                    }
                    let mut stage=phase.lock().unwrap();
                    if *stage=="uploading bundled helper" {*stage="waiting for server to finish helper upload";}
                }else {
                    ensure!(response==format!("{nonce}:CACHED"),"Unexpected helper cache response");
                    if !args.benchmark_control {eprintln!("Using cached helper");}
                }
            }
            tokio::time::timeout(Duration::from_secs(args.connect_timeout),ready.notified()).await?;
            Ok::<(),anyhow::Error>(())
        };
        tokio::select!{
            biased;
            _=interrupt.recv()=>return Ok(()),
            _=failed.changed()=>bail!("{}",failed.borrow().as_deref().unwrap_or("Bridge failed")),
            result=startup=>{
                if let Some(message)=failed.borrow().as_ref(){bail!("{message}");}
                result.with_context(||format!("Helper startup failed while {}; check the remote terminal and cache permissions",*phase.lock().unwrap()))?;
            },
        }

        let port=listener.local_addr()?.port();
        let (stop_tx,mut stop_rx)=mpsc::channel(1);
        let _managed=crate::background::control(&args,stop_tx).await?;
        if !args.benchmark_control{eprintln!("Bridge ready on 127.0.0.1:{port}; Ctrl+C to stop");}
        let control=async {if args.benchmark_control{benchmark_control(port).await}else{interrupt.recv().await;Ok(())}};
        tokio::pin!(control);
        let next_id=AtomicU32::new(1);
        let mut connections=tokio::task::JoinSet::new();
        let mut refresh=tokio::time::interval(Duration::from_secs(60));refresh.tick().await;
        loop {
            tokio::select!{
                _=stop_rx.recv(),if args.managed_child=>break,
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

#[derive(Debug)]
struct InsecureVerifier;
impl rustls::client::danger::ServerCertVerifier for InsecureVerifier {
    fn verify_server_cert(
        &self,
        _: &rustls::pki_types::CertificateDer<'_>,
        _: &[rustls::pki_types::CertificateDer<'_>],
        _: &rustls::pki_types::ServerName<'_>,
        _: &[u8],
        _: rustls::pki_types::UnixTime,
    ) -> std::result::Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        m: &[u8],
        c: &rustls::pki_types::CertificateDer<'_>,
        s: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            m,
            c,
            s,
            &rustls::crypto::ring::default_provider().signature_verification_algorithms,
        )
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &rustls::pki_types::CertificateDer<'_>,
        s: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            m,
            c,
            s,
            &rustls::crypto::ring::default_provider().signature_verification_algorithms,
        )
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
