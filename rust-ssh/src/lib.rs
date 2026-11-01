//! Native binary multiplexing. ESC-safe byte stuffing for Guacamole terminal pipes.
use anyhow::{Result, bail, ensure};
use std::fmt::Write;

pub const VERSION: u8 = 1;
pub const FRAME: usize = 65536;
pub const WINDOW: usize = 32;
pub const MAX_FRAME: usize = 262144;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    Ready = 1,
    Open = 2,
    Opened = 3,
    Data = 4,
    Credit = 5,
    Eof = 6,
    Close = 7,
    Closed = 8,
    Error = 9,
    Heartbeat = 10,
    Shutdown = 11,
}
impl TryFrom<u8> for Kind {
    type Error = anyhow::Error;
    fn try_from(value: u8) -> Result<Self> {
        Ok(match value {
            1 => Self::Ready,
            2 => Self::Open,
            3 => Self::Opened,
            4 => Self::Data,
            5 => Self::Credit,
            6 => Self::Eof,
            7 => Self::Close,
            8 => Self::Closed,
            9 => Self::Error,
            10 => Self::Heartbeat,
            11 => Self::Shutdown,
            _ => bail!("Unknown frame kind"),
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub kind: Kind,
    pub id: u32,
    pub data: Vec<u8>,
}
impl Frame {
    pub fn control(kind: Kind, id: u32) -> Self {
        Self {
            kind,
            id,
            data: Vec::new(),
        }
    }
    pub fn payload(kind: Kind, id: u32, data: Vec<u8>) -> Self {
        Self { kind, id, data }
    }
}

pub fn encode(frame: &Frame) -> Result<Vec<u8>> {
    ensure!(frame.data.len() <= MAX_FRAME, "Oversized binary frame");
    let mut raw = Vec::with_capacity(12 + frame.data.len());
    raw.extend_from_slice(&[b'G', b'R', VERSION, frame.kind as u8]);
    raw.extend_from_slice(&frame.id.to_be_bytes());
    raw.extend_from_slice(&(frame.data.len() as u32).to_be_bytes());
    raw.extend_from_slice(&frame.data);
    let mut wire = Vec::with_capacity(raw.len() + raw.len() / 64);
    for b in raw {
        match b {
            0x1b => wire.extend_from_slice(&[0x1d, 0x3b]),
            0x1d => wire.extend_from_slice(&[0x1d, 0x3d]),
            _ => wire.push(b),
        }
    }
    Ok(wire)
}

#[derive(Default)]
pub struct Decoder {
    buffer: Vec<u8>,
    escaped: bool,
}
impl Decoder {
    pub fn feed(&mut self, bytes: &[u8]) -> Result<Vec<Frame>> {
        for &b in bytes {
            if self.escaped {
                ensure!(b == 0x3b || b == 0x3d, "Invalid byte escape");
                self.buffer.push(b ^ 0x20);
                self.escaped = false;
            } else {
                ensure!(b != 0x1b, "Unescaped terminal ESC");
                if b == 0x1d {
                    self.escaped = true;
                } else {
                    self.buffer.push(b);
                }
            }
        }
        ensure!(self.buffer.len() <= MAX_FRAME * 2, "Oversized binary input");
        let mut result = Vec::new();
        let mut pos = 0;
        while self.buffer.len() - pos >= 12 {
            let header = &self.buffer[pos..pos + 12];
            ensure!(
                header[..3] == [b'G', b'R', VERSION],
                "Wrong bridge protocol; use the native helper"
            );
            let kind = Kind::try_from(header[3])?;
            let id = u32::from_be_bytes(header[4..8].try_into()?);
            let n = u32::from_be_bytes(header[8..12].try_into()?) as usize;
            ensure!(n <= MAX_FRAME, "Oversized binary frame");
            if self.buffer.len() - pos < 12 + n {
                break;
            }
            let data = self.buffer[pos + 12..pos + 12 + n].to_vec();
            if !matches!(kind, Kind::Data | Kind::Error | Kind::Ready) {
                ensure!(n == 0, "Unexpected control payload");
            }
            result.push(Frame { kind, id, data });
            pos += 12 + n;
        }
        self.buffer.drain(..pos);
        Ok(result)
    }
    pub fn complete(&self) -> bool {
        self.buffer.is_empty() && !self.escaped
    }
}

pub fn instruction(args: &[&str]) -> String {
    let mut out = String::new();
    for (i, arg) in args.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        let len = if arg.is_ascii() {
            arg.len()
        } else {
            arg.chars().count()
        };
        write!(out, "{len}.{arg}").unwrap();
    }
    out.push(';');
    out
}

#[derive(Default)]
pub struct Parser {
    buffer: Vec<u8>,
}

impl Parser {
    pub fn feed(&mut self, text: &str) -> Result<Vec<Vec<String>>> {
        self.buffer.extend_from_slice(text.as_bytes());
        ensure!(
            self.buffer.len() <= 16 * 1024 * 1024,
            "Guacamole instruction exceeds limit"
        );
        let mut result = Vec::new();
        let mut start = 0;
        'outer: while start < self.buffer.len() {
            let mut pos = start;
            let mut args = Vec::new();
            loop {
                let Some(relative) = self.buffer[pos..].iter().position(|b| *b == b'.') else {
                    break 'outer;
                };
                let dot = pos + relative;
                ensure!(
                    dot > pos
                        && dot - pos <= 9
                        && self.buffer[pos..dot].iter().all(u8::is_ascii_digit),
                    "Invalid instruction length"
                );
                let length: usize = std::str::from_utf8(&self.buffer[pos..dot])?.parse()?;
                let value_start = dot + 1;
                let tail = &self.buffer[value_start..];
                let end = if tail.len() >= length && tail[..length].is_ascii() {
                    value_start + length
                } else {
                    let s = std::str::from_utf8(tail)?;
                    let Some((offset, _)) = s.char_indices().nth(length) else {
                        break 'outer;
                    };
                    value_start + offset
                };
                if end >= self.buffer.len() {
                    break 'outer;
                }
                args.push(std::str::from_utf8(&self.buffer[value_start..end])?.to_owned());
                pos = end + 1;
                match self.buffer[end] {
                    b';' => {
                        result.push(args);
                        start = pos;
                        break;
                    }
                    b',' => (),
                    _ => bail!("Invalid instruction separator"),
                }
            }
        }
        self.buffer.drain(..start);
        Ok(result)
    }
}

pub fn shell_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

#[cfg(unix)]
pub struct RawTerminal(Option<libc::termios>);
#[cfg(unix)]
impl RawTerminal {
    pub fn enter() -> Result<Self> {
        // All termios calls operate on stdin only; restore the captured value in Drop.
        unsafe {
            if libc::isatty(0) == 0 {
                return Ok(Self(None));
            }
            let mut old = std::mem::zeroed();
            ensure!(
                libc::tcgetattr(0, &mut old) == 0,
                "Cannot read terminal state"
            );
            let mut raw = old;
            libc::cfmakeraw(&mut raw);
            ensure!(
                libc::tcsetattr(0, libc::TCSANOW, &raw) == 0,
                "Cannot set terminal raw mode"
            );
            Ok(Self(Some(old)))
        }
    }
}
#[cfg(unix)]
impl Drop for RawTerminal {
    fn drop(&mut self) {
        if let Some(old) = self.0 {
            unsafe {
                libc::tcsetattr(0, libc::TCSANOW, &old);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_byte_survives_every_boundary() {
        let data: Vec<u8> = (0..65536).map(|n| (n % 256) as u8).collect();
        let frame = Frame::payload(Kind::Data, 0x1b1d1234, data);
        let wire = encode(&frame).unwrap();
        assert!(!wire.contains(&0x1b));
        for chunk in [1, 2, 7, 3072, 16384, 100000] {
            let mut decoder = Decoder::default();
            let mut result = Vec::new();
            for part in wire.chunks(chunk) {
                result.extend(decoder.feed(part).unwrap());
            }
            assert_eq!(result, vec![frame.clone()]);
            assert!(decoder.complete());
        }
    }
    #[test]
    fn coalesced_and_malformed_records() {
        let frame = Frame::control(Kind::Eof, 1);
        let mut wire = encode(&frame).unwrap();
        wire.extend(encode(&frame).unwrap());
        assert_eq!(Decoder::default().feed(&wire).unwrap().len(), 2);
        for wire in [
            vec![0x1b],
            vec![0x1d, 0],
            b"XX\x01\x04\0\0\0\x01\0\0\0\0".to_vec(),
        ] {
            assert!(Decoder::default().feed(&wire).is_err());
        }
        let mut bad = vec![b'G', b'R', 1, 4];
        bad.extend([0; 4]);
        bad.extend(u32::MAX.to_be_bytes());
        assert!(Decoder::default().feed(&bad).is_err());
    }
    #[test]
    fn instruction_unicode_and_fragmentation() {
        let raw = instruction(&["blob", "1", "Grüße 😀"]) + &instruction(&["sync", "1"]);
        let mut parser = Parser::default();
        let mut parsed = Vec::new();
        for ch in raw.chars() {
            parsed.extend(parser.feed(&ch.to_string()).unwrap());
        }
        assert_eq!(
            parsed,
            vec![vec!["blob", "1", "Grüße 😀"], vec!["sync", "1"]]
        );
    }
    #[test]
    fn bad_lengths_and_separators() {
        for raw in ["x.a;", "1.ax", "9999999999.a;"] {
            assert!(Parser::default().feed(raw).is_err());
        }
    }
}
