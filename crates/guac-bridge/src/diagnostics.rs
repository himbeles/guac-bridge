//! Bounded metadata-only diagnostics; disk I/O runs on a separate thread.
use crate::{Frame, Kind};
use anyhow::Result;
use std::{
    collections::VecDeque,
    fs::{File, OpenOptions},
    io::{Seek, SeekFrom, Write},
    path::PathBuf,
    sync::{
        Mutex, OnceLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{SyncSender, sync_channel},
    },
    time::Instant,
};
const LIMIT: u64 = 2 * 1024 * 1024;
struct Trace {
    start: Instant,
    recent: VecDeque<String>,
    output: Option<SyncSender<String>>,
    worker: Option<std::thread::JoinHandle<()>>,
}
static TRACE: OnceLock<Mutex<Trace>> = OnceLock::new();
static ENABLED: AtomicBool = AtomicBool::new(false);
static TX_FRAMES: AtomicU64 = AtomicU64::new(0);
static RX_FRAMES: AtomicU64 = AtomicU64::new(0);
static TX_BYTES: AtomicU64 = AtomicU64::new(0);
static RX_BYTES: AtomicU64 = AtomicU64::new(0);
static DROPPED: AtomicU64 = AtomicU64::new(0);
fn state() -> &'static Mutex<Trace> {
    TRACE.get_or_init(|| {
        Mutex::new(Trace {
            start: Instant::now(),
            recent: VecDeque::new(),
            output: None,
            worker: None,
        })
    })
}
fn open(path: &PathBuf) -> Result<File> {
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        let file = options.open(path)?;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
        Ok(file)
    }
    #[cfg(not(unix))]
    {
        Ok(options.open(path)?)
    }
}
pub fn init(path: Option<&str>) -> Result<()> {
    if let Some(path) = path {
        let path = PathBuf::from(path);
        let mut file = open(&path)?;
        let (output, receiver) = sync_channel::<String>(256);
        let worker = std::thread::Builder::new()
            .name("guac-diagnostics".into())
            .spawn(move || {
                let mut size = 0;
                let result = (|| -> Result<()> {
                    for line in receiver {
                        if size + line.len() as u64 > LIMIT {
                            let mut backup = open(&PathBuf::from(format!("{}.1", path.display())))?;
                            file.seek(SeekFrom::Start(0))?;
                            std::io::copy(&mut file, &mut backup)?;
                            file.set_len(0)?;
                            file.seek(SeekFrom::Start(0))?;
                            size = 0;
                        }
                        file.write_all(line.as_bytes())?;
                        size += line.len() as u64;
                    }
                    Ok(())
                })();
                // Disk errors disable logging, never the tunnel.
                let _ = result;
            })?;
        let mut trace = state().lock().unwrap();
        trace.output = Some(output);
        trace.worker = Some(worker);
        ENABLED.store(true, Ordering::Relaxed);
    }
    event(&format!(
        "start version={} pid={}",
        env!("CARGO_PKG_VERSION"),
        std::process::id()
    ));
    Ok(())
}
pub fn event(message: &str) {
    let mut trace = state().lock().unwrap();
    let safe: String = message
        .chars()
        .take(2048)
        .flat_map(char::escape_debug)
        .collect();
    let line = format!("{:.3} {safe}\n", trace.start.elapsed().as_secs_f64());
    if trace.recent.len() == 32 {
        trace.recent.pop_front();
    }
    trace.recent.push_back(line.clone());
    if let Some(output) = &trace.output
        && output.try_send(line).is_err()
    {
        DROPPED.fetch_add(1, Ordering::Relaxed);
    }
}
#[inline]
pub fn frame(direction: &str, frame: &Frame) {
    // Hot path: no allocation, mutex, file I/O, payload scanning, or extra wire bytes.
    if matches!(frame.kind, Kind::Data | Kind::Credit | Kind::InputAck) {
        if ENABLED.load(Ordering::Relaxed) {
            let (frames, bytes) = if direction == "tx" {
                (&TX_FRAMES, &TX_BYTES)
            } else {
                (&RX_FRAMES, &RX_BYTES)
            };
            frames.fetch_add(1, Ordering::Relaxed);
            bytes.fetch_add(frame.data.len() as u64, Ordering::Relaxed);
        }
        return;
    }
    event(&format!(
        "{direction} kind={:?} channel={} bytes={}",
        frame.kind,
        frame.id,
        frame.data.len()
    ));
    if ENABLED.load(Ordering::Relaxed)
        && matches!(frame.kind, Kind::Heartbeat | Kind::Closed | Kind::Shutdown)
    {
        event(&format!(
            "totals tx_frames={} tx_bytes={} rx_frames={} rx_bytes={} dropped_log_events={}",
            TX_FRAMES.load(Ordering::Relaxed),
            TX_BYTES.load(Ordering::Relaxed),
            RX_FRAMES.load(Ordering::Relaxed),
            RX_BYTES.load(Ordering::Relaxed),
            DROPPED.load(Ordering::Relaxed)
        ));
    }
}
pub fn recent() -> String {
    state().lock().unwrap().recent.iter().cloned().collect()
}
/// Only called after forwarding has stopped; drain pending log records at exit.
pub fn shutdown() {
    ENABLED.store(false, Ordering::Relaxed);
    let worker = {
        let mut trace = state().lock().unwrap();
        trace.output.take();
        trace.worker.take()
    };
    if let Some(worker) = worker {
        let _ = worker.join();
    }
}
