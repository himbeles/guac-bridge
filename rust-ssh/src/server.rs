//! Independent loopback TCP helper using ESC-safe binary frames.
use anyhow::{Result, bail, ensure};
use clap::Parser;
use guac_ssh_rust_prototype::{Decoder, FRAME, Frame, Kind, MAX_FRAME, WINDOW, encode};
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
    about = "Native experimental Guacamole SSH helper (binary protocol)"
)]
struct Args {
    #[arg(long)]
    stdio: bool,
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
    .await??;
    stream.set_nodelay(true)?;
    let (mut reader, mut writer) = stream.into_split();
    output.send(Frame::control(Kind::Opened, id)).await?;
    let download = async {
        let mut buffer = vec![0; args.frame_size];
        loop {
            credits.clone().acquire_owned().await?.forget();
            let n = reader.read(&mut buffer).await?;
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
                    writer.write_all(&data).await?;
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
async fn read_input(sender: mpsc::Sender<Frame>) -> Result<()> {
    let mut stdin = tokio::io::stdin();
    let mut decoder = Decoder::default();
    let mut chunk = [0; 16384];
    loop {
        let n = stdin.read(&mut chunk).await?;
        if n == 0 {
            ensure!(decoder.complete(), "Truncated binary frame");
            sender.send(Frame::control(Kind::Shutdown, 0)).await?;
            return Ok(());
        }
        for frame in decoder.feed(&chunk[..n])? {
            let stop = frame.kind == Kind::Shutdown;
            sender.send(frame).await?;
            if stop {
                return Ok(());
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
    let _terminal = guac_ssh_rust_prototype::RawTerminal::enter()?;
    let mut stdout = std::io::stdout().lock();
    stdout.write_all(b"\x1b]482202;2;LLM\x07")?;
    let mut ready = Vec::new();
    ready.extend_from_slice(&(args.window as u32).to_be_bytes());
    ready.extend_from_slice(&(args.frame_size as u32).to_be_bytes());
    stdout.write_all(&encode(&Frame::payload(Kind::Ready, 0, ready))?)?;
    stdout.flush()?;
    let (input_tx, mut input_rx) = mpsc::channel(64);
    let (output_tx, mut output_rx) = mpsc::channel::<Frame>(64);
    // Reader sends a fatal error via a separate channel rather than losing queued frames.
    let (error_tx, mut error_rx) = mpsc::channel(1);
    let stdin_task = tokio::spawn(async move {
        if let Err(error) = read_input(input_tx).await {
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
                _=heartbeat.tick()=>{stdout.write_all(&encode(&Frame::control(Kind::Heartbeat,0))?)?;stdout.flush()?;},
                Some(frame)=output_rx.recv()=>{
                    if matches!(frame.kind,Kind::Closed|Kind::Error){channels.remove(&frame.id);}
                    stdout.write_all(&encode(&frame)?)?;stdout.flush()?;
                },
                frame=input_rx.recv()=>{
                    let Some(frame)=frame else {break;};
                    let id=frame.id;
                    match frame.kind {
                        Kind::Shutdown=>break,
                        Kind::Open=>{
                            ensure!(id>0&&!channels.contains_key(&id),"Invalid or duplicate channel ID");
                            if channels.len()>=args.max_connections {
                                stdout.write_all(&encode(&Frame::payload(Kind::Error,id,b"SSH connection limit reached".to_vec()))?)?;
                                stdout.flush()?;continue;
                            }
                            let (tx,rx)=mpsc::channel(args.window+1);
                            let credits=Arc::new(Semaphore::new(args.window));
                            let (owned_args,owned_credits,owned_output)=(args.clone(),credits.clone(),output_tx.clone());
                            let task=tokio::spawn(async move {
                                let result=connection(id,owned_args,rx,owned_credits,owned_output.clone()).await;
                                let event=if result.is_ok(){Frame::control(Kind::Closed,id)}
                                    else {Frame::payload(Kind::Error,id,b"SSH target connection failed".to_vec())};
                                let _=owned_output.send(event).await;
                            });
                            channels.insert(id,Channel{input:tx,credits,task,eof:false});
                        },
                        Kind::Close=>{
                            if let Some(channel)=channels.remove(&id){channel.task.abort();let _=channel.task.await;}
                            stdout.write_all(&encode(&Frame::control(Kind::Closed,id))?)?;stdout.flush()?;
                        },
                        _=>if let Some(channel)=channels.get_mut(&id){match frame.kind {
                            Kind::Data=>{
                                ensure!(!channel.eof&&frame.data.len()<=args.frame_size,"Invalid TCP frame");
                                channel.input.try_send(Some(frame.data))?;
                            },
                            Kind::Credit=>{
                                ensure!(channel.credits.available_permits()<args.window,"Excess channel credits");
                                channel.credits.add_permits(1);
                            },
                            Kind::Eof=>{
                                ensure!(!channel.eof,"Duplicate EOF");channel.eof=true;channel.input.try_send(None)?;
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
    stdout.write_all(b"\x1b]482203;\x07")?;
    stdout.flush()?;
    result
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
        (1..=128).contains(&args.window) && (4096..=MAX_FRAME).contains(&args.frame_size),
        "Invalid transport sizes"
    );
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(serve(Arc::new(args)));
    runtime.shutdown_timeout(Duration::from_millis(100));
    result
}
