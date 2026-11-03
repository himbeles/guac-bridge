//! Independent native client: Guacamole login, WebSocket, ESC-safe mux, loopback TCP.
use crate::options::BridgeOptions;
use crate::{Decoder, Frame, Kind, Parser, instruction};
use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD as B64};
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
use tokio_tungstenite::tungstenite::Message;
mod guacamole;

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
struct ReliableInput {
    sender: Mutex<crate::reliable::Sender>,
    changed: Notify,
}
struct ReceivedFrames<'a> {
    decoder: Decoder,
    args: &'a BridgeOptions,
    channels: &'a Channels,
    ready: &'a Notify,
    started: &'a AtomicBool,
    reliable: &'a ReliableInput,
}
impl ReceivedFrames<'_> {
    fn feed(&mut self, data: &[u8]) -> Result<bool> {
        let frames = self
            .decoder
            .feed(data)
            .with_context(|| format!("Helper output decode failed: {}", self.decoder.status()))?;
        let any = !frames.is_empty();
        for frame in frames {
            crate::diagnostics::frame("rx", &frame);
            match frame.kind {
                Kind::Ready => {
                    ensure!(frame.id == 0, "Invalid helper ready frame");
                    let meta: serde_json::Value = serde_json::from_slice(&frame.data)?;
                    ensure!(
                        meta["package_version"] == env!("CARGO_PKG_VERSION"),
                        "Bundled helper version does not match client"
                    );
                    ensure!(
                        meta["window"] == self.args.window
                            && meta["frame_size"] == self.args.frame_size,
                        "Helper transport settings do not match"
                    );
                    ensure!(
                        meta["input_protocol"] == "reliable-v1",
                        "Helper must support reliable-v1 input; rebuild client and helper from the same release"
                    );
                    self.started.store(true, Ordering::Relaxed);
                    self.ready.notify_one();
                }
                Kind::Heartbeat => (),
                Kind::InputAck => {
                    self.reliable
                        .sender
                        .lock()
                        .unwrap()
                        .ack(&frame.data, Instant::now())?;
                    self.reliable.changed.notify_one();
                }
                Kind::Credit => {
                    if let Some(channel) = self.channels.lock().unwrap().get(&frame.id) {
                        ensure!(
                            channel.credits.available_permits() < self.args.window,
                            "Excess channel credits"
                        );
                        channel.credits.add_permits(1);
                    }
                }
                _ => {
                    ensure!(
                        frame.kind != Kind::Data
                            || frame.data.len() <= self.args.frame_size as usize,
                        "TCP frame exceeds limit"
                    );
                    if let Some(channel) = self.channels.lock().unwrap().get(&frame.id) {
                        let id = frame.id;
                        channel.queue.try_send(frame).map_err(|error| anyhow!("Local download queue rejected frame: channel={id} capacity={} error={error}", channel.queue.capacity()))?;
                    }
                }
            }
        }
        Ok(any)
    }
}
fn bootstrap_bytes(
    buffer: &mut Vec<u8>,
    data: &[u8],
    nonce: &str,
    args: &BridgeOptions,
    phase: &Mutex<&'static str>,
    tx: &mpsc::Sender<String>,
) -> Result<()> {
    buffer.extend(data);
    ensure!(buffer.len() < 4096, "Bootstrap output exceeds limit");
    while let Some(n) = buffer.iter().position(|b| *b == b'\n') {
        let line = String::from_utf8(buffer.drain(..=n).collect())?;
        let line = line.trim();
        if !line.starts_with(&format!("{nonce}:")) {
            continue;
        }
        if let Some(message) = line.strip_prefix(&format!("{nonce}:ERROR:")) {
            let lower = message.to_ascii_lowercase();
            if lower.contains("checksum") || lower.contains("decode") || lower.contains("upload") {
                bail!(
                    "Server helper bootstrap failed: {message}. Possible upload corruption or stalled input; reduce --input-window to 4 or 2; on Linux also try --upload-batch-size 12288 or 3072"
                );
            }
            bail!("Server helper bootstrap failed: {message}");
        }
        if let Some(path) = line.strip_prefix(&format!("{nonce}:LOCATION:")) {
            if !args.benchmark_control {
                eprintln!("Server helper cache: {path}");
            }
        } else if line == format!("{nonce}:VERIFY") {
            crate::console::complete_upload();
            *phase.lock().unwrap() = "verifying helper checksum";
            if !args.benchmark_control {
                eprintln!("Verifying helper checksum…");
            }
        } else if line == format!("{nonce}:STARTING") {
            crate::console::finish_progress();
            *phase.lock().unwrap() = "starting server helper";
            if !args.benchmark_control {
                eprintln!("Starting server helper…");
            }
        } else {
            tx.try_send(line.to_owned())?;
        }
    }
    Ok(())
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
fn reliable_wire(bytes: &[u8], windows: bool) -> Vec<u8> {
    if windows {
        let mut wire = B64.encode(bytes).into_bytes();
        wire.push(b'\r');
        wire
    } else {
        bytes.to_vec()
    }
}
async fn reliable_retries(
    state: &ReliableInput,
    acks: &Acks,
    out: &mpsc::Sender<Message>,
    args: &BridgeOptions,
    windows: bool,
) -> Result<()> {
    let (packets, warning) = {
        let mut tx = state.sender.lock().unwrap();
        let packets = tx.due(Instant::now())?;
        (packets, tx.warning(Instant::now()))
    };
    if let Some(warning) = warning {
        crate::diagnostics::event(&warning);
        eprintln!("{warning}");
    }
    for packet in packets {
        raw_input(
            &reliable_wire(&packet, windows),
            args.input_chunk,
            acks,
            out,
            Duration::from_secs(args.connect_timeout),
        )
        .await?;
    }
    Ok(())
}
async fn reliable_wait(
    state: &ReliableInput,
    acks: &Acks,
    out: &mpsc::Sender<Message>,
    args: &BridgeOptions,
    windows: bool,
) -> Result<()> {
    tokio::select! {_=state.changed.notified()=>(),_=tokio::time::sleep(Duration::from_millis(25))=>()}
    reliable_retries(state, acks, out, args, windows).await
}
async fn reliable_send(
    bytes: &[u8],
    state: &ReliableInput,
    acks: &Acks,
    out: &mpsc::Sender<Message>,
    args: &BridgeOptions,
    windows: bool,
) -> Result<()> {
    for part in bytes.chunks(crate::reliable::CHUNK) {
        loop {
            let packet = state.sender.lock().unwrap().queue(part, Instant::now())?;
            if let Some(packet) = packet {
                raw_input(
                    &reliable_wire(&packet, windows),
                    args.input_chunk,
                    acks,
                    out,
                    Duration::from_secs(args.connect_timeout),
                )
                .await?;
                break;
            }
            reliable_wait(state, acks, out, args, windows).await?;
        }
    }
    Ok(())
}
async fn input_sender(
    mut input: mpsc::Receiver<Input>,
    acks: Arc<Acks>,
    out: mpsc::Sender<Message>,
    args: Arc<BridgeOptions>,
    windows: Arc<AtomicBool>,
    reliable: Arc<ReliableInput>,
) -> Result<()> {
    let timeout = Duration::from_secs(args.connect_timeout);
    let mut tick = tokio::time::interval(Duration::from_millis(25));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut pending = None;
    loop {
        let message = if let Some(message) = pending.take() {
            message
        } else {
            tokio::select! {
                message=input.recv()=>{let Some(message)=message else {break;};message},
                _=tick.tick(),if !reliable.sender.lock().unwrap().idle()=>{
                    reliable_retries(&reliable,&acks,&out,&args,windows.load(Ordering::Relaxed)).await?;continue;
                }
            }
        };
        let printable = windows.load(Ordering::Relaxed);
        let (mut bytes, done, chunk, shutdown, event) = match message {
            Input::Raw(bytes) => (bytes, None, 3072, false, false),
            Input::Upload(bytes, done) => (
                bytes,
                Some(done),
                if args.server_os == "linux" {
                    crate::deploy::UPLOAD_INPUT_CHUNK
                } else {
                    args.input_chunk
                },
                false,
                false,
            ),
            Input::Event(frame) => {
                crate::diagnostics::frame("tx", &frame);
                let bytes = crate::encode_plain(&frame)?;
                (bytes, None, args.input_chunk, false, true)
            }
            Input::Shutdown(done) => {
                let frame = Frame::control(Kind::Shutdown, 0);
                crate::diagnostics::frame("tx", &frame);
                let bytes = crate::encode_plain(&frame)?;
                (bytes, Some(done), args.input_chunk, true, true)
            }
        };
        if event {
            // Coalesce only events already queued. Never wait to fill a record:
            // interactive messages retain their latency, while bursts of small
            // Credit/control frames avoid a separate carrier ACK per event.
            while !shutdown && bytes.len() < crate::reliable::CHUNK {
                match input.try_recv() {
                    Ok(Input::Event(frame)) => {
                        let extra = crate::encode_plain(&frame)?;
                        if bytes.len() + extra.len() > crate::reliable::CHUNK {
                            pending = Some(Input::Event(frame));
                            break;
                        }
                        crate::diagnostics::frame("tx", &frame);
                        bytes.extend_from_slice(&extra);
                    }
                    Ok(other) => {
                        pending = Some(other);
                        break;
                    }
                    Err(_) => break,
                }
            }
            if shutdown {
                while !reliable.sender.lock().unwrap().idle() {
                    reliable_wait(&reliable, &acks, &out, &args, printable).await?;
                }
            }
            reliable_send(&bytes, &reliable, &acks, &out, &args, printable).await?;
        } else {
            raw_input(&bytes,chunk,&acks,&out,timeout).await.context("Guacamole terminal input stopped progressing; reduce --input-window or increase --connect-timeout for a slow link")?;
        }
        if let Some(done) = done {
            if shutdown {
                let warning = reliable
                    .sender
                    .lock()
                    .unwrap()
                    .final_warning(Instant::now());
                if let Some(warning) = warning {
                    crate::diagnostics::event(&warning);
                    eprintln!("{warning}");
                }
            }
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
// Each argument is a separate channel resource; keep forwarding ownership explicit.
#[allow(clippy::too_many_arguments)]
async fn channel(
    id: u32,
    stream: TcpStream,
    mut queue: mpsc::Receiver<Frame>,
    credits: Arc<Semaphore>,
    input: mpsc::Sender<Input>,
    channels: Channels,
    args: Arc<BridgeOptions>,
    usage: Arc<crate::usage::Usage>,
) -> Result<()> {
    let _connection = usage.connection();
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
            "Remote target refused the TCP connection: {}",
            String::from_utf8_lossy(&opened.data[..opened.data.len().min(1024)]).escape_debug()
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
                usage.transferred(true, n);
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
                        usage.transferred(false, frame.data.len());
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
                        bail!(
                            "Remote target connection failed: {}",
                            String::from_utf8_lossy(&frame.data[..frame.data.len().min(1024)])
                                .escape_debug()
                        );
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

pub(crate) async fn serve(args: Arc<BridgeOptions>) -> Result<()> {
    let startup_guard = crate::maintenance::startup_guard()?;
    let destination = guacamole::Destination::parse(&args)?;
    #[cfg(unix)]
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    #[cfg(windows)]
    let mut interrupt = tokio::signal::windows::ctrl_c()?;
    let listener=TcpListener::bind(("127.0.0.1",args.port())).await.with_context(|| format!("Cannot listen on 127.0.0.1:{}: the port is already in use or reserved. Choose another --local-port, or stop the process using it",args.port()))?;

    #[cfg(windows)]
    if !args.benchmark_control
        && let Err(error) = crate::tray::launch()
    {
        crate::diagnostics::event(&format!("Cannot start tray: {error:#}"));
    }
    let guacamole::Session {
        socket,
        http,
        token,
        username,
    } = destination.connect(&args).await?;
    let (mut sink, mut stream) = socket.split();
    let (out, mut outbound) = mpsc::channel::<Message>(128);
    let (input, inputs) = mpsc::channel(64);
    let acks = Arc::new(Acks {
        slots: Arc::new(Semaphore::new(args.input_window)),
        pending: Mutex::new(VecDeque::new()),
        window: args.input_window,
    });
    let reliable = Arc::new(ReliableInput {
        sender: Mutex::new(crate::reliable::Sender::new(
            crate::reliable::MAX_WINDOW,
            Duration::from_secs(args.connect_timeout),
        )),
        changed: Notify::new(),
    });
    let read_reliable = reliable.clone();
    let channels: Channels = Arc::new(Mutex::new(HashMap::new()));
    let (failure, mut failed) = watch::channel(None);
    let closing = Arc::new(AtomicBool::new(false));
    let synced = Arc::new(Notify::new());
    let ready = Arc::new(Notify::new());
    let windows = Arc::new(AtomicBool::new(false));
    let helper_started = Arc::new(AtomicBool::new(false));
    let read_started = helper_started.clone();
    let read_windows = windows.clone();
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
        let mut parser=Parser::default();let mut frames=ReceivedFrames{decoder:Decoder::default(),args:&read_args,channels:&read_channels,ready:&read_ready,started:&read_started,reliable:&read_reliable};let mut output_stream=None;let mut pipe_ended:Option<Instant>=None;let mut remote_error=Vec::new();let mut boot_stream=None;let mut boot_buffer=Vec::new();
        let mut heartbeat=Instant::now();let mut tick=tokio::time::interval(Duration::from_secs(1));
        loop {
            tokio::select!{
                _=tick.tick()=>{
                    ensure!(!pipe_ended.is_some_and(|time|time.elapsed()>Duration::from_secs(2)),"Remote helper output pipe closed; helper exited without a fatal report. Check --server-debug-log");
                    ensure!(!read_started.load(Ordering::Relaxed)||heartbeat.elapsed()<Duration::from_secs(45),"Remote helper heartbeat lost");
                    let timed_out=read_acks.pending.lock().unwrap().front().is_some_and(|time|time.elapsed()>Duration::from_secs(read_args.connect_timeout));
                    ensure!(!timed_out,"Guacamole input acknowledgement timed out while {}",if read_started.load(Ordering::Relaxed){"forwarding data"}else{*read_phase.lock().unwrap()});
                },
                message=stream.next()=>{
                    let Some(message)=message else {bail!("Guacamole tunnel closed");};
                    let message=message.map_err(|_|anyhow!("WebSocket read failed"))?;
                    match message {
                        Message::Ping(bytes)=>{read_out.send(Message::Pong(bytes)).await?;continue;},
                        Message::Close(reason)=>bail!("Guacamole WebSocket closed: {reason:?}"),
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
                                    if values[3]=="GBERR:END" {
                                        let report=String::from_utf8_lossy(&remote_error);
                                        crate::diagnostics::event(&format!("remote fatal: {report}"));
                                        bail!("Remote helper failed: {}",report.escape_debug());
                                    }else if values[3].starts_with(crate::terminal_wire::ERROR_PREFIX){
                                        pipe_ended=Some(Instant::now());
                                        remote_error.extend(B64.decode(&values[3][crate::terminal_wire::ERROR_PREFIX.len()..])?);
                                        ensure!(remote_error.len()<=8192,"Remote helper fatal report exceeds limit");
                                        ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                    }else if read_windows.load(Ordering::Relaxed)&&values[3].starts_with(crate::terminal_wire::BOOT_PREFIX){
                                        bootstrap_bytes(&mut boot_buffer,&B64.decode(&values[3][crate::terminal_wire::BOOT_PREFIX.len()..])?,&read_nonce,&read_args,&read_phase,&boot_tx)?;
                                        ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                    }else if read_windows.load(Ordering::Relaxed)&&values[3].starts_with(crate::terminal_wire::OSC_PREFIX){
                                        let data=B64.decode(&values[3][crate::terminal_wire::OSC_PREFIX.len()..])?;
                                        if frames.feed(&data)?{heartbeat=Instant::now();}
                                        ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                    }else if values[3]=="BOOT"&&boot_stream.is_none(){boot_stream=Some(values[1].clone());ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;}
                                    else if values[3]=="LLM"&&output_stream.is_none(){output_stream=Some(values[1].clone());ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;}
                                    else{ws_send(&read_out,&["ack",&values[1],"Unsupported","256"]).await?;}
                                },
                                "blob" if values.get(1)==boot_stream.as_ref()=>{
                                    ensure!(values.len()>=3,"Invalid bootstrap blob");
                                    bootstrap_bytes(&mut boot_buffer,&B64.decode(&values[2])?,&read_nonce,&read_args,&read_phase,&boot_tx)?;
                                    ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                },
                                "end" if values.get(1)==boot_stream.as_ref()=>{boot_stream=None;},
                                "blob" if values.get(1)==output_stream.as_ref()=>{
                                    ensure!(values.len()>=3,"Invalid blob");
                                    let data=B64.decode(&values[2])?;
                                    if frames.feed(&data)? {heartbeat=Instant::now();}
                                    ws_send(&read_out,&["ack",&values[1],"OK","0"]).await?;
                                },
                                "end" if values.get(1)==output_stream.as_ref()=>{
                                    if !read_windows.load(Ordering::Relaxed) && !read_closing.load(Ordering::Relaxed){pipe_ended=Some(Instant::now());}output_stream=None;
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
        input_sender(
            inputs,
            acks.clone(),
            out.clone(),
            args.clone(),
            windows.clone(),
            reliable.clone(),
        ),
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
            let is_windows=args.server_os=="windows";
            windows.store(is_windows,Ordering::Relaxed);
            let mut deploy_args=(*args).clone();
            deploy_args.server_os=if is_windows {"windows"}else{"linux"}.into();
            if !args.benchmark_control {eprintln!("Remote helper platform: {} x86-64",if is_windows {"Windows"}else{"Linux"});}
            {
                if is_windows {*phase.lock().unwrap()="configuring Windows SSH console; check PowerShell availability and application policy";}
                input.send(Input::Raw(crate::deploy::prepare(&deploy_args,&nonce)?)).await?;
                let prepared=tokio::time::timeout(Duration::from_secs(args.connect_timeout),boot_rx.recv()).await?.context("Terminal preparation closed")?;
                ensure!(prepared==format!("{nonce}:PREPARED"),"Unexpected terminal preparation response");
            }
            *phase.lock().unwrap()="checking server helper cache";
            if !args.benchmark_control {eprintln!("Checking server helper…");}
            input.send(Input::Raw(crate::deploy::command(&deploy_args,&nonce)?)).await?;
            if args.server_command.is_none() {
                let response=tokio::time::timeout(Duration::from_secs(args.connect_timeout),boot_rx.recv()).await?.context("Helper bootstrap closed")?;
                if response==format!("{nonce}:UPLOAD") {
                    *phase.lock().unwrap()="uploading bundled helper";
                    let encoded=crate::deploy::encoded_agent(&deploy_args)?;
                    if !args.benchmark_control {eprintln!("Uploading bundled helper ({} KiB)…",crate::deploy::agent(&deploy_args)?.len().div_ceil(1024));}
                    let mut progress=crate::console::UploadProgress::new(!args.benchmark_control);
                    if is_windows {
                        // Windows PowerShell bootstrap uses a different (console-safe) upload protocol.
                        let block=args.input_chunk*args.input_window;
                        for (index,part) in encoded.chunks(block).enumerate() {
                            let (sent,receipt)=oneshot::channel();
                            input.send(Input::Upload(part.to_vec(),sent)).await?;
                            receipt.await.context("Helper upload sender stopped")?;
                            let percent=((index+1)*block).min(encoded.len())*100/encoded.len();
                            progress.update(percent,false);
                        }
                    } else {
                        // A Guacamole input ACK only says that the gateway accepted an input blob.
                        // Wait for the remote shell to confirm it has decoded each block.
                        let block=args.upload_batch_size as usize;
                        for (index,part) in encoded.chunks(block).enumerate() {
                            let (sent,receipt)=oneshot::channel();
                            input.send(Input::Upload(part.to_vec(),sent)).await?;
                            receipt.await.context("Helper upload sender stopped")?;
                            *phase.lock().unwrap()="waiting for remote upload block acknowledgement";
                            let response=tokio::time::timeout(
                                Duration::from_secs(args.connect_timeout),
                                boot_rx.recv()
                            ).await.with_context(||format!(
                                "Remote shell did not acknowledge helper upload block {} ({} / {} encoded bytes sent); try --upload-batch-size 12288 (or 3072) and --input-window 4 (or 2)",
                                index+1, ((index+1)*block).min(encoded.len()), encoded.len()
                            ))?.context("Helper bootstrap closed during upload")?;
                            ensure!(
                                response==format!("{nonce}:CHUNK:{}",index+1),
                                "Unexpected remote upload acknowledgement for block {}: {response}",
                                index+1
                            );
                            // The shell can report VERIFY/STARTING immediately after the final
                            // chunk; do not overwrite those newer stages.
                            let mut stage=phase.lock().unwrap();
                            if *stage=="waiting for remote upload block acknowledgement" {
                                *stage="uploading bundled helper";
                            }
                            drop(stage);
                            let percent=((index+1)*block).min(encoded.len())*100/encoded.len();
                            progress.update(percent,true);
                        }
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
        let usage=Arc::new(crate::usage::Usage::default());
        let _managed=crate::background::control(&args,&username,port,stop_tx,usage.clone()).await?;
        drop(startup_guard);
        if !args.benchmark_control{eprintln!("Bridge ready on 127.0.0.1:{port}; Ctrl+C to stop");}
        let control=async {if args.benchmark_control{benchmark_control(port).await}else{interrupt.recv().await;Ok(())}};
        tokio::pin!(control);
        let next_id=AtomicU32::new(1);
        let mut connections=tokio::task::JoinSet::new();
        let refresh=async {
            loop {
                tokio::time::sleep(Duration::from_secs(60)).await;
                guacamole::refresh_once(&http, destination.refresh_url()?, &token).await?;
            }
            #[allow(unreachable_code)] Ok::<(),anyhow::Error>(())
        };
        tokio::pin!(refresh);
        loop {
            tokio::select!{
                _=stop_rx.recv(),if !args.benchmark_control=>break,
                result=&mut control=>{result?;break;},
                _=failed.changed()=>bail!("{}",failed.borrow().as_deref().unwrap_or("Bridge failed")),
                result=&mut refresh,if destination.is_connection()=>{result?;break;},
                accepted=listener.accept()=>{
                    let (stream,_)=accepted?;
                    stream.set_nodelay(true)?;
                    if channels.lock().unwrap().len()>=args.max_connections{drop(stream);continue;}
                    let id=next_id.fetch_add(1,Ordering::Relaxed);ensure!(id>0,"Channel ID space exhausted; restart bridge");
                    let (tx,rx)=mpsc::channel(args.window+4);let credits=Arc::new(Semaphore::new(args.window));
                    channels.lock().unwrap().insert(id,Channel{queue:tx,credits:credits.clone()});
                    let (input,channels,args)=(input.clone(),channels.clone(),args.clone());
                    let usage=usage.clone();
                    connections.spawn(async move {if let Err(error)=channel(id,stream,rx,credits,input,channels,args,usage).await{crate::diagnostics::event(&format!("TCP channel {id}: {error:#}"));eprintln!("TCP channel {id}: {error:#}");}});
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
    if helper_started.load(Ordering::Relaxed) {
        let _ = tokio::time::timeout(Duration::from_secs(3), shutdown).await;
    }
    let _ = tokio::time::timeout(Duration::from_secs(1), out.send(Message::Close(None))).await;
    for task in &tasks {
        task.abort();
    }
    for task in tasks {
        let _ = task.await;
    }
    result
}
