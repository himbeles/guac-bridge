//! Terminal carriers share the same binary protocol and flow control.
//! Windows ConPTY needs printable input and short OSC-only output envelopes.
use crate::{Decoder, Frame, encode};
use anyhow::{Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
pub const OSC_PREFIX: &str = "GBW1:";
pub const ERROR_PREFIX: &str = "GBERR:";
pub const BOOT_PREFIX: &str = "GBWB:";
pub const OSC_CHUNK: usize = 120;
pub fn osc_bytes(prefix: &str, bytes: &[u8]) -> Vec<u8> {
    let mut output = Vec::new();
    for part in bytes.chunks(OSC_CHUNK) {
        output.extend_from_slice(
            format!(
                "\x1b]482202;2;{prefix}{}\x07\x1b]482203;\x07",
                STANDARD.encode(part)
            )
            .as_bytes(),
        );
    }
    output
}
pub fn output(frame: &Frame, windows: bool) -> Result<Vec<u8>> {
    let bytes = encode(frame)?;
    crate::diagnostics::frame("tx", frame);
    Ok(if windows {
        osc_bytes(OSC_PREFIX, &bytes)
    } else {
        bytes
    })
}
#[derive(Default)]
pub struct InputDecoder {
    binary: Decoder,
    line: Vec<u8>,
    reliable: crate::reliable::Receiver,
    armor_discard: bool,
}
impl InputDecoder {
    fn binary_feed(&mut self, bytes: &[u8]) -> Result<Vec<Frame>> {
        self.reliable.feed(bytes, &mut self.binary)
    }
    pub fn take_ack(&mut self) -> Option<Frame> {
        self.reliable
            .take_ack()
            .map(|ack| Frame::payload(crate::Kind::InputAck, 0, ack.encode()))
    }
    pub fn feed(&mut self, bytes: &[u8], windows: bool) -> Result<Vec<Frame>> {
        if !windows {
            return self.binary_feed(bytes);
        }
        let mut frames = Vec::new();
        let limit = (crate::reliable::CHUNK + crate::reliable::CHUNK / 254 + 64) * 8 / 3 + 32;
        for &byte in bytes {
            if matches!(byte, b'\r' | b'\n') {
                if self.armor_discard {
                    self.armor_discard = false;
                    self.reliable.reject_armor();
                    continue;
                }
                if self.line.is_empty() {
                    continue;
                }
                let decoded = match STANDARD.decode(&self.line) {
                    Ok(decoded) => decoded,
                    Err(_) => {
                        self.reliable.reject_armor();
                        self.line.clear();
                        continue;
                    }
                };
                self.line.clear();
                frames.extend(self.binary_feed(&decoded)?);
            } else {
                if self.armor_discard {
                    continue;
                }
                let valid = byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/' | b'=');
                if !valid || self.line.len() >= limit {
                    self.line.clear();
                    self.armor_discard = true;
                    continue;
                }
                ensure!(valid, "Invalid Windows input encoding");
                self.line.push(byte);
                ensure!(
                    self.line.len() <= limit,
                    "Windows input frame exceeds limit"
                );
            }
        }
        Ok(frames)
    }
    pub fn status(&self) -> String {
        format!(
            "{} armor_buffered={} {}",
            self.binary.status(),
            self.line.len(),
            self.reliable.status()
        )
    }
    pub fn complete(&self) -> bool {
        self.line.is_empty()
            && !self.armor_discard
            && self.binary.complete()
            && self.reliable.complete()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::Kind;
    #[test]
    fn armored_input_survives_crlf_and_all_bytes() {
        let frame = Frame::payload(Kind::Data, 1, (0..65536).map(|n| (n % 256) as u8).collect());
        let packet = crate::reliable::record(1, &crate::encode_plain(&frame).unwrap()).unwrap();
        let mut wire = STANDARD.encode(packet).into_bytes();
        wire.push(b'\r');
        let mut decoder = InputDecoder::default();
        let mut result = Vec::new();
        for part in wire.chunks(17) {
            result.extend(decoder.feed(part, true).unwrap());
        }
        assert_eq!(result, vec![frame]);
        assert!(decoder.complete());
        assert_eq!(decoder.feed(b"\r\n", true).unwrap().len(), 0);
        assert!(decoder.feed(b"!\r", true).unwrap().is_empty());
        assert!(decoder.complete());
    }
    #[test]
    fn console_output_reconstructs_binary_frames() {
        let expected = Frame::payload(Kind::Data, 7, (0..65536).map(|n| (n % 256) as u8).collect());
        let wire = output(&expected, true).unwrap();
        let mut decoder = Decoder::default();
        let mut frames = Vec::new();
        for envelope in wire.split(|b| *b == 7) {
            if let Some(encoded) = envelope.strip_prefix(b"\x1b]482202;2;GBW1:") {
                frames.extend(decoder.feed(&STANDARD.decode(encoded).unwrap()).unwrap());
            }
        }
        assert_eq!(frames, vec![expected]);
        assert!(decoder.complete());
    }
    #[test]
    fn reliable_windows_recovers_armor_and_bounds_maximum_frames() {
        for byte in [0, 0x1b, 0x1d, 255] {
            let expected = Frame::payload(Kind::Data, 1, vec![byte; 65536]);
            let packet =
                crate::reliable::record(1, &crate::encode_plain(&expected).unwrap()).unwrap();
            let mut line = STANDARD.encode(packet).into_bytes();
            line.push(b'\r');
            let mut decoder = InputDecoder::default();
            assert!(decoder.feed(b"@\r", true).unwrap().is_empty());
            let mut truncated = line.clone();
            truncated.remove(18);
            assert!(decoder.feed(&truncated, true).unwrap().is_empty());
            let mut frames = Vec::new();
            for part in line.chunks(257) {
                frames.extend(decoder.feed(part, true).unwrap());
            }
            assert_eq!(frames, vec![expected]);
            assert!(decoder.complete());
            assert!(
                crate::reliable::Ack::decode(&decoder.take_ack().unwrap().data)
                    .unwrap()
                    .rejected
                    >= 2
            );
        }
    }
    #[test]
    fn short_osc_envelopes_fit_legacy_conpty() {
        let wire = osc_bytes(OSC_PREFIX, &vec![255; 65536]);
        for part in wire.split(|b| *b == 7) {
            assert!(part.len() < 220);
        }
    }
}
