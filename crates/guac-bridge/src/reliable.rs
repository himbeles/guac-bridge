//! Optional checksummed input records; TCP frame fields remain unchanged.
use anyhow::{Result, bail, ensure};
use std::{
    collections::BTreeMap,
    sync::Arc,
    time::{Duration, Instant},
};
pub const CHUNK: usize = crate::MAX_FRAME + 12;
pub const MAX_WINDOW: usize = 64;
const HEADER: usize = 15;
const MAX_BUFFER: usize = (CHUNK + HEADER + 4) * 2;

fn cobs_encode(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(bytes.len() + bytes.len() / 254 + 2);
    let mut position = 0;
    loop {
        let limit = (bytes.len() - position).min(254);
        let count = memchr::memchr(0, &bytes[position..position + limit]).unwrap_or(limit);
        out.push((count + 1) as u8);
        out.extend_from_slice(&bytes[position..position + count]);
        position += count;
        if position == bytes.len() {
            break;
        }
        if count < 254 {
            position += 1;
            if position == bytes.len() {
                out.push(1);
                break;
            }
        }
    }
    out
}
fn cobs_decode(bytes: &mut Vec<u8>) -> bool {
    let (mut read, mut write) = (0, 0);
    while read < bytes.len() {
        let code = bytes[read] as usize;
        read += 1;
        if code == 0 || read + code - 1 > bytes.len() {
            return false;
        }
        bytes.copy_within(read..read + code - 1, write);
        read += code - 1;
        write += code - 1;
        if code < 255 && read < bytes.len() {
            bytes[write] = 0;
            write += 1;
        }
    }
    bytes.truncate(write);
    true
}
pub fn record(sequence: u64, data: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        sequence > 0 && !data.is_empty() && data.len() <= CHUNK,
        "Invalid reliable record"
    );
    let mut raw = Vec::with_capacity(HEADER + data.len() + 4);
    raw.extend_from_slice(b"RI\x01");
    raw.extend_from_slice(&sequence.to_be_bytes());
    raw.extend_from_slice(&(data.len() as u32).to_be_bytes());
    raw.extend_from_slice(data);
    raw.extend_from_slice(&crc32fast::hash(&raw).to_be_bytes());
    let raw = cobs_encode(&raw);
    let mut wire = Vec::with_capacity(raw.len() + raw.len() / 64 + 2);
    wire.push(0);
    let mut offset = 0;
    while let Some(relative) = memchr::memchr2(0x1b, 0x1d, &raw[offset..]) {
        let at = offset + relative;
        wire.extend_from_slice(&raw[offset..at]);
        wire.extend_from_slice(&[0x1d, raw[at] ^ 0x20]);
        offset = at + 1;
    }
    wire.extend_from_slice(&raw[offset..]);
    wire.push(0);
    Ok(wire)
}
#[derive(Debug, Clone, Copy)]
pub struct Ack {
    pub next: u64,
    pub mask: u64,
    pub rejected: u64,
}
impl Ack {
    pub fn encode(self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(28);
        for value in [self.next, self.mask, self.rejected] {
            bytes.extend_from_slice(&value.to_be_bytes());
        }
        bytes.extend_from_slice(&crc32fast::hash(&bytes).to_be_bytes());
        bytes
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        ensure!(bytes.len() == 28, "Invalid reliable ACK length");
        ensure!(
            crc32fast::hash(&bytes[..24]) == u32::from_be_bytes(bytes[24..].try_into()?),
            "Invalid reliable ACK checksum"
        );
        Ok(Self {
            next: u64::from_be_bytes(bytes[..8].try_into()?),
            mask: u64::from_be_bytes(bytes[8..16].try_into()?),
            rejected: u64::from_be_bytes(bytes[16..24].try_into()?),
        })
    }
}
pub struct Receiver {
    record: Vec<u8>,
    escaped: bool,
    discard: bool,
    next: u64,
    pending: BTreeMap<u64, Vec<u8>>,
    rejected: u64,
    dirty: bool,
}
impl Default for Receiver {
    fn default() -> Self {
        Self {
            record: Vec::new(),
            escaped: false,
            discard: false,
            next: 1,
            pending: BTreeMap::new(),
            rejected: 0,
            dirty: false,
        }
    }
}
impl Receiver {
    pub fn reject_armor(&mut self) {
        self.rejected += 1;
        self.dirty = true;
    }
    fn finish(&mut self, decoder: &mut crate::Decoder) -> Result<Vec<crate::Frame>> {
        let mut frames = Vec::new();
        if self.discard || self.escaped {
            self.reject_armor();
        } else if !self.record.is_empty() {
            self.dirty = true;
            let mut raw = std::mem::take(&mut self.record);
            let valid = cobs_decode(&mut raw) && raw.len() >= HEADER + 4 && raw[..3] == *b"RI\x01";
            let length = if valid {
                u32::from_be_bytes(raw[11..15].try_into()?) as usize
            } else {
                0
            };
            if valid
                && length > 0
                && length <= CHUNK
                && raw.len() == HEADER + length + 4
                && crc32fast::hash(&raw[..HEADER + length])
                    == u32::from_be_bytes(raw[HEADER + length..].try_into()?)
            {
                let sequence = u64::from_be_bytes(raw[3..11].try_into()?);
                if sequence >= self.next && sequence < self.next.saturating_add(MAX_WINDOW as u64) {
                    self.pending.entry(sequence).or_insert(raw);
                    while let Some(data) = self.pending.remove(&self.next) {
                        frames.extend(decoder.feed_plain(&data[HEADER..data.len() - 4])?);
                        if self.record.capacity() < data.capacity() {
                            self.record = data;
                            self.record.clear();
                        }
                        self.next = self
                            .next
                            .checked_add(1)
                            .ok_or_else(|| anyhow::anyhow!("Reliable input sequence exhausted"))?;
                    }
                } else if sequence == 0 || sequence >= self.next {
                    self.rejected += 1;
                }
                // Retransmissions older than next are ACKed but never delivered twice.
            } else {
                self.rejected += 1;
            }
        }
        self.record.clear();
        self.escaped = false;
        self.discard = false;
        Ok(frames)
    }
    pub fn feed(
        &mut self,
        bytes: &[u8],
        decoder: &mut crate::Decoder,
    ) -> Result<Vec<crate::Frame>> {
        let mut frames = Vec::new();
        let mut position = 0;
        while position < bytes.len() {
            if self.discard {
                let Some(next) = memchr::memchr(0, &bytes[position..]) else {
                    break;
                };
                position += next;
            } else if !self.escaped {
                let count = memchr::memchr3(0, 0x1b, 0x1d, &bytes[position..])
                    .unwrap_or(bytes.len() - position);
                if self.record.len() + count > MAX_BUFFER {
                    self.discard = true;
                    position += count;
                    continue;
                }
                self.record
                    .extend_from_slice(&bytes[position..position + count]);
                position += count;
                if position == bytes.len() {
                    break;
                }
            }
            let byte = bytes[position];
            position += 1;
            if byte == 0 {
                frames.extend(self.finish(decoder)?);
            } else if !self.discard {
                if self.escaped {
                    if matches!(byte, 0x3b | 0x3d) {
                        self.record.push(byte ^ 0x20);
                        self.escaped = false;
                    } else {
                        self.discard = true;
                    }
                } else if byte == 0x1d {
                    self.escaped = true;
                } else {
                    self.discard = true;
                }
                if self.record.len() > MAX_BUFFER {
                    self.discard = true;
                }
            }
        }
        Ok(frames)
    }
    pub fn take_ack(&mut self) -> Option<Ack> {
        if !std::mem::take(&mut self.dirty) {
            return None;
        }
        let mut mask = 0;
        for sequence in self.pending.keys() {
            mask |= 1 << (sequence - self.next);
        }
        Some(Ack {
            next: self.next,
            mask,
            rejected: self.rejected,
        })
    }
    pub fn complete(&self) -> bool {
        self.record.is_empty() && !self.escaped && !self.discard && self.pending.is_empty()
    }
    pub fn status(&self) -> String {
        format!(
            "reliable_next={} buffered={} reordered={} rejected={}",
            self.next,
            self.record.len(),
            self.pending.len(),
            self.rejected
        )
    }
}
struct Packet {
    wire: Arc<[u8]>,
    first: Instant,
    sent: Instant,
    retries: u32,
    fast: bool,
}
pub struct Sender {
    base: u64,
    next: u64,
    window: usize,
    pending: BTreeMap<u64, Packet>,
    rto: Duration,
    timeout: Duration,
    retries: u64,
    rejected: u64,
    bad_acks: u64,
    gaps: u64,
    last_gap: Option<u64>,
    last_report: Option<(Instant, (u64, u64, u64, u64))>,
}
impl Sender {
    pub fn new(window: usize, timeout: Duration) -> Self {
        Self {
            base: 1,
            next: 1,
            window,
            pending: BTreeMap::new(),
            rto: Duration::from_millis(500),
            timeout,
            retries: 0,
            rejected: 0,
            bad_acks: 0,
            gaps: 0,
            last_gap: None,
            last_report: None,
        }
    }
    pub fn queue(&mut self, data: &[u8], now: Instant) -> Result<Option<Arc<[u8]>>> {
        ensure!(
            (1..=MAX_WINDOW).contains(&self.window),
            "Invalid reliable send window"
        );
        if self.next - self.base >= self.window as u64 {
            return Ok(None);
        }
        let wire: Arc<[u8]> = record(self.next, data)?.into();
        self.pending.insert(
            self.next,
            Packet {
                wire: wire.clone(),
                first: now,
                sent: now,
                retries: 0,
                fast: false,
            },
        );
        self.next = self
            .next
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("Reliable input sequence exhausted"))?;
        Ok(Some(wire))
    }
    pub fn ack(&mut self, bytes: &[u8], now: Instant) -> Result<()> {
        let ack = match Ack::decode(bytes) {
            Ok(ack) => ack,
            Err(_) => {
                self.bad_acks += 1;
                return Ok(());
            }
        };
        ensure!(
            ack.next >= 1 && ack.next <= self.next,
            "Reliable ACK refers to unsent input"
        );
        let new_rejections = ack.rejected > self.rejected;
        self.rejected = self.rejected.max(ack.rejected);
        let accepted = |seq: u64| {
            seq < ack.next
                || (seq >= ack.next
                    && seq - ack.next < 64
                    && ack.mask & (1 << (seq - ack.next)) != 0)
        };
        if let Some(rtt) = self.pending.iter().find_map(|(&seq, p)| {
            (accepted(seq) && p.retries == 0).then(|| now.duration_since(p.sent))
        }) {
            self.rto = (rtt * 3).clamp(Duration::from_millis(250), Duration::from_secs(30));
        }
        self.pending.retain(|&seq, _| !accepted(seq));
        self.base = self.base.max(ack.next);
        if ack.mask != 0 {
            if self.last_gap != Some(ack.next) {
                self.gaps += 1;
                self.last_gap = Some(ack.next);
            }
            let highest = ack.next + (63 - ack.mask.leading_zeros() as u64);
            for (_, packet) in self.pending.range_mut(ack.next..=highest) {
                if packet.retries == 0 {
                    packet.fast = true;
                }
            }
        }
        if new_rejections
            && let Some(packet) = self.pending.get_mut(&self.base)
            && packet.retries == 0
        {
            packet.fast = true;
        }
        Ok(())
    }
    pub fn due(&mut self, now: Instant) -> Result<Vec<Arc<[u8]>>> {
        let mut due = Vec::new();
        for (&seq, packet) in &mut self.pending {
            if now.duration_since(packet.first) > self.timeout {
                bail!(
                    "Reliable input delivery stalled at record {seq}; try --input-window 4 (then 2), --window 2 or --reliable-window 32; collect client/server debug logs"
                );
            }
            if packet.fast
                || now.duration_since(packet.sent) >= self.rto * (1u32 << packet.retries.min(4))
            {
                packet.fast = false;
                packet.sent = now;
                packet.retries += 1;
                self.retries += 1;
                due.push(packet.wire.clone());
            }
        }
        Ok(due)
    }
    pub fn idle(&self) -> bool {
        self.pending.is_empty()
    }
    pub fn warning(&mut self, now: Instant) -> Option<String> {
        let counts = (self.retries, self.rejected, self.bad_acks, self.gaps);
        if counts == (0, 0, 0, 0) {
            return None;
        }
        if let Some((last, previous)) = self.last_report
            && (previous == counts || now.duration_since(last) < Duration::from_secs(5))
        {
            return None;
        }
        self.last_report = Some((now, counts));
        Some(format!(
            "Warning: reliable input recovery: retransmitted={} rejected_records={} invalid_acks={} sequence_gaps={}. Retries can also mean delayed/lost ACKs. If repeated, reduce --input-window to 4 or 2; then try --window 2 or --reliable-window 32. For a merely slow link, increase --connect-timeout. Keep client/server debug logs for diagnosis.",
            counts.0, counts.1, counts.2, counts.3
        ))
    }
    pub fn final_warning(&mut self, now: Instant) -> Option<String> {
        if self
            .last_report
            .is_some_and(|(_, old)| old == (self.retries, self.rejected, self.bad_acks, self.gaps))
        {
            return None;
        }
        self.last_report = None;
        self.warning(now)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Decoder, Frame, Kind, encode_plain};
    #[test]
    fn cobs_all_boundaries_and_byte_patterns() {
        for size in [0, 1, 253, 254, 255, 508, 65548] {
            for value in [0, 1, 0x1b, 0x1d, 255] {
                let data = vec![value; size];
                let mut encoded = cobs_encode(&data);
                assert!(!encoded.contains(&0));
                assert!(cobs_decode(&mut encoded));
                assert_eq!(encoded, data);
            }
        }
    }
    #[test]
    fn loss_corruption_duplicates_and_reordering_preserve_data() {
        let frame = Frame::payload(Kind::Data, 1, (0..65536).map(|i| (i % 256) as u8).collect());
        let raw = encode_plain(&frame).unwrap();
        let packets: Vec<_> = (1..=3).map(|n| record(n, &raw).unwrap()).collect();
        let mut rx = Receiver::default();
        let mut decoder = Decoder::default();
        let mut frames = rx.feed(&packets[0], &mut decoder).unwrap();
        assert!(rx.feed(&packets[2], &mut decoder).unwrap().is_empty());
        let mut corrupt = packets[1].clone();
        corrupt[40] ^= 1;
        assert!(rx.feed(&corrupt, &mut decoder).unwrap().is_empty());
        let ack = rx.take_ack().unwrap();
        assert_eq!(ack.next, 2);
        assert_ne!(ack.mask, 0);
        assert_eq!(ack.rejected, 1);
        for part in packets[1].chunks(17) {
            frames.extend(rx.feed(part, &mut decoder).unwrap());
        }
        assert_eq!(frames, vec![frame; 3]);
        for packet in packets {
            assert!(rx.feed(&packet, &mut decoder).unwrap().is_empty());
        }
        assert!(rx.complete());
        assert!(decoder.complete());
    }
    #[test]
    fn missing_delimiter_resynchronizes() {
        let frame = Frame::control(Kind::Open, 1);
        let raw = encode_plain(&frame).unwrap();
        let a = record(1, &raw).unwrap();
        let b = record(2, &raw).unwrap();
        let mut rx = Receiver::default();
        let mut decoder = Decoder::default();
        assert!(
            rx.feed(&[&a[..a.len() - 1], &b[1..]].concat(), &mut decoder)
                .unwrap()
                .is_empty()
        );
        assert_eq!(rx.take_ack().unwrap().rejected, 1);
        assert_eq!(rx.feed(&a, &mut decoder).unwrap(), vec![frame.clone()]);
        assert_eq!(rx.feed(&b, &mut decoder).unwrap(), vec![frame]);
    }
    #[test]
    fn selective_ack_retries_only_missing_and_bounds_new_data() {
        let now = Instant::now();
        let mut tx = Sender::new(4, Duration::from_secs(5));
        for _ in 0..4 {
            assert!(tx.queue(b"sample", now).unwrap().is_some());
        }
        assert!(tx.queue(b"full", now).unwrap().is_none());
        tx.ack(
            &Ack {
                next: 2,
                mask: 6,
                rejected: 0,
            }
            .encode(),
            now + Duration::from_millis(20),
        )
        .unwrap();
        assert_eq!(tx.due(now + Duration::from_millis(21)).unwrap().len(), 1);
        assert!(
            tx.warning(now + Duration::from_millis(21))
                .unwrap()
                .contains("--input-window")
        );
        tx.ack(
            &Ack {
                next: 5,
                mask: 0,
                rejected: 0,
            }
            .encode(),
            now + Duration::from_millis(40),
        )
        .unwrap();
        assert!(tx.idle());
    }
    #[test]
    fn queued_small_frames_share_a_record_without_changing_order() {
        let frames = [
            Frame::control(Kind::Credit, 1),
            Frame::control(Kind::Open, 2),
        ];
        let bytes: Vec<u8> = frames
            .iter()
            .flat_map(|f| encode_plain(f).unwrap())
            .collect();
        let mut rx = Receiver::default();
        let mut decoder = Decoder::default();
        assert_eq!(
            rx.feed(&record(1, &bytes).unwrap(), &mut decoder).unwrap(),
            frames
        );
        assert!(decoder.complete());
    }
    #[test]
    fn invalid_ack_cannot_release_data_and_delivery_deadline_is_bounded() {
        let now = Instant::now();
        let mut tx = Sender::new(1, Duration::from_secs(2));
        tx.queue(b"sample", now).unwrap();
        let mut ack = Ack {
            next: 2,
            mask: 0,
            rejected: 0,
        }
        .encode();
        ack[1] ^= 1;
        tx.ack(&ack, now).unwrap();
        assert!(!tx.idle());
        assert!(tx.warning(now).unwrap().contains("invalid_acks=1"));
        assert!(
            tx.due(now + Duration::from_secs(3))
                .unwrap_err()
                .to_string()
                .contains("--input-window")
        );
    }
    #[test]
    fn lost_ack_does_not_duplicate_application_delivery() {
        let now = Instant::now();
        let mut tx = Sender::new(1, Duration::from_secs(5));
        let mut rx = Receiver::default();
        let mut decoder = Decoder::default();
        let frame = Frame::control(Kind::Open, 1);
        let packet = tx
            .queue(&encode_plain(&frame).unwrap(), now)
            .unwrap()
            .unwrap();
        assert_eq!(rx.feed(&packet, &mut decoder).unwrap(), vec![frame]);
        rx.take_ack();
        let retries = tx.due(now + Duration::from_millis(600)).unwrap();
        assert_eq!(retries.len(), 1);
        assert!(rx.feed(&retries[0], &mut decoder).unwrap().is_empty());
        tx.ack(
            &rx.take_ack().unwrap().encode(),
            now + Duration::from_millis(620),
        )
        .unwrap();
        assert!(tx.idle());
    }
}
