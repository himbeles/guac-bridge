//! Payload counters shared by forwarding and the read-only control endpoint.
use serde::{Deserialize, Serialize};
use std::{
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Instant,
};

pub(crate) struct Usage {
    started: Instant,
    uploaded: AtomicU64,
    downloaded: AtomicU64,
    connections: AtomicU64,
    total_connections: AtomicU64,
    last_activity_ms: AtomicU64,
}
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub(crate) struct Snapshot {
    pub uptime_ms: u64,
    pub uploaded: u64,
    pub downloaded: u64,
    pub connections: u64,
    pub total_connections: u64,
    pub idle_ms: Option<u64>,
}
impl Default for Usage {
    fn default() -> Self {
        Self {
            started: Instant::now(),
            uploaded: AtomicU64::new(0),
            downloaded: AtomicU64::new(0),
            connections: AtomicU64::new(0),
            total_connections: AtomicU64::new(0),
            last_activity_ms: AtomicU64::new(0),
        }
    }
}
impl Usage {
    pub fn connection(self: &Arc<Self>) -> Connection {
        self.connections.fetch_add(1, Ordering::Relaxed);
        self.total_connections.fetch_add(1, Ordering::Relaxed);
        Connection(self.clone())
    }
    pub fn transferred(&self, upload: bool, bytes: usize) {
        if bytes == 0 {
            return;
        }
        let counter = if upload {
            &self.uploaded
        } else {
            &self.downloaded
        };
        counter.fetch_add(bytes as u64, Ordering::Relaxed);
        self.last_activity_ms.fetch_max(
            self.started.elapsed().as_millis() as u64 + 1,
            Ordering::Relaxed,
        );
    }
    pub fn snapshot(&self) -> Snapshot {
        let uptime_ms = self.started.elapsed().as_millis() as u64;
        let last = self.last_activity_ms.load(Ordering::Relaxed);
        Snapshot {
            uptime_ms,
            uploaded: self.uploaded.load(Ordering::Relaxed),
            downloaded: self.downloaded.load(Ordering::Relaxed),
            connections: self.connections.load(Ordering::Relaxed),
            total_connections: self.total_connections.load(Ordering::Relaxed),
            idle_ms: (last != 0).then(|| uptime_ms.saturating_sub(last - 1)),
        }
    }
}
pub(crate) struct Connection(Arc<Usage>);
impl Drop for Connection {
    fn drop(&mut self) {
        self.0.connections.fetch_sub(1, Ordering::Relaxed);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn counters_survive_connections_and_guards_close_on_abort() {
        let usage = Arc::new(Usage::default());
        assert_eq!(usage.snapshot().idle_ms, None);
        let a = usage.connection();
        let b = usage.connection();
        usage.transferred(true, 123);
        usage.transferred(false, 456);
        drop(a);
        drop(b);
        let s = usage.snapshot();
        assert_eq!(
            (s.uploaded, s.downloaded, s.connections, s.total_connections),
            (123, 456, 0, 2)
        );
        assert!(s.idle_ms.is_some());
    }
}
