//! Terminal carriers share the same binary protocol and flow control.
//! Windows ConPTY needs printable input and short OSC-only output envelopes.
use crate::{Decoder, Frame, MAX_FRAME, encode};
use anyhow::{Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
pub const OSC_PREFIX: &str = "GBW1:";
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
    Ok(if windows {
        osc_bytes(OSC_PREFIX, &bytes)
    } else {
        bytes
    })
}
pub fn input(frame: &Frame, windows: bool) -> Result<Vec<u8>> {
    let bytes = encode(frame)?;
    Ok(if windows {
        let mut out = STANDARD.encode(bytes).into_bytes();
        out.push(b'\n');
        out
    } else {
        bytes
    })
}
#[derive(Default)]
pub struct InputDecoder {
    binary: Decoder,
    line: Vec<u8>,
}
impl InputDecoder {
    pub fn feed(&mut self, bytes: &[u8], windows: bool) -> Result<Vec<Frame>> {
        if !windows {
            return self.binary.feed(bytes);
        }
        let mut frames = Vec::new();
        for &b in bytes {
            if b == b'\r' || b == b'\n' {
                if !self.line.is_empty() {
                    let decoded = STANDARD.decode(&self.line)?;
                    self.line.clear();
                    frames.extend(self.binary.feed(&decoded)?);
                    ensure!(self.binary.complete(), "Incomplete armored frame");
                }
            } else {
                ensure!(
                    b.is_ascii_alphanumeric() || b == b'+' || b == b'/' || b == b'=',
                    "Invalid Windows input encoding"
                );
                self.line.push(b);
                ensure!(
                    self.line.len() <= (MAX_FRAME + 12) * 8 / 3 + 8,
                    "Windows input frame exceeds limit"
                );
            }
        }
        Ok(frames)
    }
    pub fn complete(&self) -> bool {
        self.line.is_empty() && self.binary.complete()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::Kind;
    #[test]
    fn armored_input_survives_crlf_and_all_bytes() {
        let frame = Frame::payload(Kind::Data, 1, (0..65536).map(|n| (n % 256) as u8).collect());
        let wire = input(&frame, true).unwrap();
        let mut decoder = InputDecoder::default();
        let mut result = Vec::new();
        for part in wire.chunks(17) {
            result.extend(decoder.feed(part, true).unwrap());
        }
        assert_eq!(result, vec![frame]);
        assert!(decoder.complete());
        assert_eq!(decoder.feed(b"\r\n", true).unwrap().len(), 0);
        assert!(decoder.feed(b"!", true).is_err());
    }
    #[test]
    fn short_osc_envelopes_fit_legacy_conpty() {
        let wire = osc_bytes(OSC_PREFIX, &vec![255; 65536]);
        for part in wire.split(|b| *b == 7) {
            assert!(part.len() < 220);
        }
    }
}
