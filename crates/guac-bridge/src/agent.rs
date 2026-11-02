//! Independent loopback TCP helper using ESC-safe binary frames.
use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use guac_bridge::diagnostics;
use guac_bridge::{FRAME, Frame, Kind, WINDOW};
use std::{collections::HashMap, io::Write, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    sync::{Semaphore, mpsc},
    task::JoinHandle,
};

#[derive(Parser)]
#[command(
    version,
    about = "Guac Bridge session helper for SSH and HTTP endpoints"
)]
struct Args {
    #[arg(long)]
    stdio: bool,
    #[arg(long)]
    debug_log: Option<String>,
    /// Use the printable Windows console carrier.
    #[arg(long)]
    windows_console: bool,
    #[arg(long, default_value = "127.0.0.1")]
    target_host: String,
    #[arg(long, default_value_t = 22)]
    target_port: u16,
    #[arg(long, default_value_t = 30)]
    connect_timeout: u64,
    #[arg(long, default_value_t = 8)]
    max_connections: usize,
    #[arg(long, default_value_t=WINDOW)]
    window: usize,
    #[arg(long, default_value_t=FRAME)]
    frame_size: usize,
}
struct Channel {
    input: mpsc::Sender<Option<Vec<u8>>>,
    credits: Arc<Semaphore>,
    task: JoinHandle<()>,
    eof: bool,
}
async fn connection(
    id: u32,
    args: Arc<Args>,
    mut input: mpsc::Receiver<Option<Vec<u8>>>,
    credits: Arc<Semaphore>,
    output: mpsc::Sender<Frame>,
) -> Result<()> {
    let stream = tokio::time::timeout(
        Duration::from_secs(args.connect_timeout),
        TcpStream::connect((args.target_host.as_str(), args.target_port)),
    )
    .await
    .context("Target TCP connect timed out")?
    .context("Target TCP connect failed")?;
    stream.set_nodelay(true)?;
    let (mut reader, mut writer) = stream.into_split();
    output.send(Frame::control(Kind::Opened, id)).await?;
    let download = async {
        let mut buffer = vec![0; args.frame_size];
        loop {
            credits.clone().acquire_owned().await?.forget();
            let n = reader
                .read(&mut buffer)
                .await
                .context("Target TCP read failed")?;
            if n == 0 {
                credits.add_permits(1);
                output.send(Frame::control(Kind::Eof, id)).await?;
                return Ok::<(), anyhow::Error>(());
            }
            output
                .send(Frame::payload(Kind::Data, id, buffer[..n].to_vec()))
                .await?;
        }
    };
    let upload = async {
        while let Some(data) = input.recv().await {
            match data {
                Some(data) => {
                    writer
                        .write_all(&data)
                        .await
                        .context("Target TCP write failed")?;
                    output.send(Frame::control(Kind::Credit, id)).await?;
                }
                None => {
                    writer.shutdown().await?;
                    return Ok::<(), anyhow::Error>(());
                }
            }
        }
        bail!("Input channel closed before EOF")
    };
    tokio::try_join!(upload, download)?;
    Ok(())
}
async fn read_input(sender: mpsc::Sender<Frame>, windows: bool) -> Result<()> {
    #[cfg(windows)]
    if windows {
        // Console handles are character devices, not ordinary files. Read input
        // records directly so ConPTY keyboard events cannot remain in a cooked
        // or redirected stream reader. Keep blocking console I/O off Tokio.
        return tokio::task::spawn_blocking(move || read_console_input(sender)).await?;
    }
    let mut stdin = tokio::io::stdin();
    let mut decoder = guac_bridge::terminal_wire::InputDecoder::default();
    let mut chunk = [0; 16384];
    loop {
        let n = stdin.read(&mut chunk).await?;
        if n == 0 {
            ensure!(
                decoder.complete(),
                "Truncated binary frame: {}",
                decoder.status()
            );
            sender.send(Frame::control(Kind::Shutdown, 0)).await?;
            return Ok(());
        }
        for frame in decoder
            .feed(&chunk[..n], windows)
            .with_context(|| format!("Remote console decode failed: {}", decoder.status()))?
        {
            diagnostics::frame("rx", &frame);
            let stop = frame.kind == Kind::Shutdown;
            sender.send(frame).await?;
            if stop {
                return Ok(());
            }
        }
    }
}
#[cfg(windows)]
fn read_console_input(sender: mpsc::Sender<Frame>) -> Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::Console::{INPUT_RECORD, KEY_EVENT, ReadConsoleInputW};
    let input = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("CONIN$")?;
    let mut records = [INPUT_RECORD::default(); 256];
    let mut decoder = guac_bridge::terminal_wire::InputDecoder::default();
    loop {
        let mut count = 0;
        let success = unsafe {
            ReadConsoleInputW(
                input.as_raw_handle(),
                records.as_mut_ptr(),
                records.len() as u32,
                &mut count,
            )
        };
        if success == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        for record in &records[..count as usize] {
            if record.EventType != KEY_EVENT as u16 {
                continue;
            }
            let key = unsafe { record.Event.KeyEvent };
            if key.bKeyDown == 0 {
                continue;
            }
            let character = unsafe { key.uChar.UnicodeChar };
            if character == 0 {
                continue;
            }
            ensure!(character <= 127, "Non-ASCII Windows transport input");
            for _ in 0..key.wRepeatCount {
                for frame in decoder.feed(&[character as u8], true).with_context(|| {
                    format!("Remote console decode failed: {}", decoder.status())
                })? {
                    diagnostics::frame("rx", &frame);
                    let stop = frame.kind == Kind::Shutdown;
                    sender.blocking_send(frame)?;
                    if stop {
                        return Ok(());
                    }
                }
            }
        }
    }
}

async fn termination() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut term = signal(SignalKind::terminate()).unwrap();
        let mut hup = signal(SignalKind::hangup()).unwrap();
        tokio::select! {_=term.recv()=>(),_=hup.recv()=>(),_=tokio::signal::ctrl_c()=>()}
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
async fn serve(args: Arc<Args>) -> Result<()> {
    #[cfg(unix)]
    let _terminal = guac_bridge::RawTerminal::enter()?;
    #[cfg(windows)]
    let _windows_terminal = if args.windows_console {
        Some(WindowsTerminal::enter()?)
    } else {
        None
    };
    let mut stdout = std::io::stdout().lock();
    if !args.windows_console {
        stdout.write_all(b"\x1b]482202;2;LLM\x07")?;
    }
    let ready = serde_json::to_vec(
        &serde_json::json!({"package_version":env!("CARGO_PKG_VERSION"),"version":5,"protocol":"tcp","window":args.window,"frame_size":args.frame_size}),
    )?;
    stdout.write_all(&guac_bridge::terminal_wire::output(
        &Frame::payload(Kind::Ready, 0, ready),
        args.windows_console,
    )?)?;
    stdout.flush()?;
    let (input_tx, mut input_rx) = mpsc::channel(64);
    let (output_tx, mut output_rx) = mpsc::channel::<Frame>(64);
    // Reader sends a fatal error via a separate channel rather than losing queued frames.
    let (error_tx, mut error_rx) = mpsc::channel(1);
    let windows = args.windows_console;
    let stdin_task = tokio::spawn(async move {
        if let Err(error) = read_input(input_tx, windows).await {
            let _ = error_tx.send(error).await;
        }
    });
    let mut channels: HashMap<u32, Channel> = HashMap::new();
    let mut heartbeat = tokio::time::interval(Duration::from_secs(10));
    heartbeat.tick().await;
    let stop = termination();
    tokio::pin!(stop);
    let result:Result<()>=async {
        loop {
            tokio::select!{
                _=&mut stop=>break,
                Some(error)=error_rx.recv()=>return Err(error),
                _=heartbeat.tick()=>{stdout.write_all(&guac_bridge::terminal_wire::output(&Frame::control(Kind::Heartbeat,0),args.windows_console)?)?;stdout.flush()?;},
                Some(frame)=output_rx.recv()=>{
                    if matches!(frame.kind,Kind::Closed|Kind::Error){channels.remove(&frame.id);}
                    stdout.write_all(&guac_bridge::terminal_wire::output(&frame,args.windows_console)?)?;stdout.flush()?;
                },
                frame=input_rx.recv()=>{
                    let Some(frame)=frame else {
                        // The reader may drop input_tx before publishing its fatal error.
                        // Drain its result instead of racing EOF against error_rx.
                        if let Some(error)=error_rx.recv().await {return Err(error);}
                        break;
                    };
                    let id=frame.id;
                    match frame.kind {
                        Kind::Shutdown=>break,
                        Kind::Open=>{
                            ensure!(id>0&&!channels.contains_key(&id),"Invalid or duplicate channel ID");
                            if channels.len()>=args.max_connections {
                                stdout.write_all(&guac_bridge::terminal_wire::output(&Frame::payload(Kind::Error,id,b"Connection limit reached".to_vec()),args.windows_console)?)?;
                                stdout.flush()?;continue;
                            }
                            let (tx,rx)=mpsc::channel(args.window+1);
                            let credits=Arc::new(Semaphore::new(args.window));
                            let (owned_args,owned_credits,owned_output)=(args.clone(),credits.clone(),output_tx.clone());
                            let task=tokio::spawn(async move {
                                let result=connection(id,owned_args.clone(),rx,owned_credits,owned_output.clone()).await;
                                if let Err(error)=&result {diagnostics::event(&format!("target channel={id}: {error:#}"));}
                                let event=match result {Ok(())=>Frame::control(Kind::Closed,id),Err(error)=>Frame::payload(Kind::Error,id,format!("Target {}:{} failed: {error:#}",owned_args.target_host,owned_args.target_port).into_bytes())};
                                let _=owned_output.send(event).await;
                            });
                            channels.insert(id,Channel{input:tx,credits,task,eof:false});
                        },
                        Kind::Close=>{
                            if let Some(channel)=channels.remove(&id){channel.task.abort();let _=channel.task.await;}
                            stdout.write_all(&guac_bridge::terminal_wire::output(&Frame::control(Kind::Closed,id),args.windows_console)?)?;stdout.flush()?;
                        },
                        _=>if let Some(channel)=channels.get_mut(&id){match frame.kind {
                            Kind::Data=>{
                                ensure!(!channel.eof&&frame.data.len()<=args.frame_size,"Invalid TCP frame: channel={id} eof={} bytes={} limit={}",channel.eof,frame.data.len(),args.frame_size);
                                channel.input.try_send(Some(frame.data)).map_err(|error| anyhow::anyhow!("Remote upload queue rejected data: channel={id} capacity={} error={error}", channel.input.capacity()))?;
                            },
                            Kind::Credit=>{
                                ensure!(channel.credits.available_permits()<args.window,"Excess channel credits: channel={id} available={} window={}",channel.credits.available_permits(),args.window);
                                channel.credits.add_permits(1);
                            },
                            Kind::Eof=>{
                                ensure!(!channel.eof,"Duplicate EOF: channel={id}");channel.eof=true;channel.input.try_send(None).map_err(|error| anyhow::anyhow!("Remote upload queue rejected EOF: channel={id} error={error}"))?;
                            },
                            _=>bail!("Unexpected channel event"),
                        }}
                    }
                }
            }
        }
        Ok(())
    }.await;
    stdin_task.abort();
    for channel in channels.values() {
        channel.task.abort();
    }
    for (_, channel) in channels {
        let _ = channel.task.await;
    }
    if !args.windows_console {
        stdout.write_all(b"\x1b]482203;\x07")?;
    }
    diagnostics::event("helper stopped");
    stdout.flush()?;
    result
}
fn report_fatal(error: &anyhow::Error) -> Result<()> {
    diagnostics::event(&format!("fatal: {error:#}"));
    let report = format!(
        "{error:#}\nRecent remote events:\n{}",
        diagnostics::recent()
    );
    let mut end = report.len().min(6000);
    while !report.is_char_boundary(end) {
        end -= 1;
    }
    let mut stdout = std::io::stdout().lock();
    // Also handles failures before the ordinary output pipe was opened.
    stdout.write_all(b"\x1b]482203;\x07")?;
    stdout.write_all(&guac_bridge::terminal_wire::osc_bytes(
        guac_bridge::terminal_wire::ERROR_PREFIX,
        &report.as_bytes()[..end],
    ))?;
    stdout.write_all(b"\x1b]482202;2;GBERR:END\x07\x1b]482203;\x07")?;
    stdout.flush()?;
    Ok(())
}

fn main() -> Result<()> {
    let args = Args::parse();
    ensure!(
        args.stdio,
        "Started automatically by native client; --stdio is required"
    );
    ensure!(
        ["127.0.0.1", "localhost", "::1"].contains(&args.target_host.as_str()),
        "Target must be loopback"
    );
    ensure!(
        args.target_port > 0
            && (1..=16).contains(&args.max_connections)
            && args.connect_timeout > 0,
        "Invalid port, limit or timeout"
    );
    ensure!(
        (1..=64).contains(&args.window)
            && [4096, 8192, 16384, 32768, 65536].contains(&args.frame_size),
        "Invalid transport sizes"
    );
    let result = (|| -> Result<()> {
        diagnostics::init(args.debug_log.as_deref())
            .context("Cannot initialize remote debug log")?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let result = runtime.block_on(serve(Arc::new(args)));
        runtime.shutdown_timeout(Duration::from_millis(100));
        result
    })();
    if let Err(error) = &result {
        let _ = report_fatal(error);
    }
    diagnostics::shutdown();
    result
}

#[cfg(windows)]
struct WindowsTerminal {
    input: std::fs::File,
    output: std::fs::File,
    input_mode: u32,
    output_mode: u32,
}
#[cfg(windows)]
impl WindowsTerminal {
    fn enter() -> Result<Self> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Console::*;
        let input = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONIN$")?;
        let output = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("CONOUT$")?;
        let (mut input_mode, mut output_mode) = (0, 0);
        unsafe {
            ensure!(
                GetConsoleMode(input.as_raw_handle(), &mut input_mode) != 0
                    && GetConsoleMode(output.as_raw_handle(), &mut output_mode) != 0,
                "Cannot read Windows console modes"
            );
            ensure!(
                SetConsoleMode(input.as_raw_handle(), ENABLE_EXTENDED_FLAGS) != 0
                    && SetConsoleMode(
                        output.as_raw_handle(),
                        output_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                    ) != 0,
                "Cannot configure Windows console"
            );
            SetStdHandle(STD_INPUT_HANDLE, input.as_raw_handle());
            SetStdHandle(STD_OUTPUT_HANDLE, output.as_raw_handle());
        }
        Ok(Self {
            input,
            output,
            input_mode,
            output_mode,
        })
    }
}
#[cfg(windows)]
impl Drop for WindowsTerminal {
    fn drop(&mut self) {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::Console::SetConsoleMode;
        unsafe {
            SetConsoleMode(self.input.as_raw_handle(), self.input_mode);
            SetConsoleMode(self.output.as_raw_handle(), self.output_mode);
        }
    }
}
