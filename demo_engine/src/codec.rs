//! INVARIANT: Wire frames use big-endian length prefixes and CRC32C trailers.
use crate::ingress::RawFrame;

pub struct FrameHeader {
    pub magic: u16,
    pub version: u8,
    pub flags: u8,
    pub payload_len: u32,
    pub crc32c: u32,
}

pub enum CodecError {
    BadMagic(u16),
    UnsupportedVersion(u8),
    ChecksumMismatch { expected: u32, actual: u32 },
    TruncatedFrame { needed: usize, available: usize },
}

pub struct WireCodec {
    strict_crc: bool,
    max_frame_size: usize,
}

impl WireCodec {
    pub fn new(strict_crc: bool, max_frame_size: usize) -> Self {
        Self { strict_crc, max_frame_size }
    }

    /// CONTRACT: Must validate header magic before reading payload slice.
    pub fn validate_frame<'buf>(&self, frame: &RawFrame<'buf>) -> Result<FrameHeader, CodecError> {
        if frame.payload.len() < 12 {
            return Err(CodecError::TruncatedFrame {
                needed: 12,
                available: frame.payload.len(),
            });
        }
        let magic = u16::from_be_bytes([frame.payload[0], frame.payload[1]]);
        if magic != 0xA11C {
            return Err(CodecError::BadMagic(magic));
        }
        let version = frame.payload[2];
        let flags = frame.payload[3];
        let payload_len = u32::from_be_bytes([
            frame.payload[4], frame.payload[5], frame.payload[6], frame.payload[7]
        ]);
        let crc32c = u32::from_be_bytes([
            frame.payload[8], frame.payload[9], frame.payload[10], frame.payload[11]
        ]);
        if self.strict_crc && crc32c == 0 {
            return Err(CodecError::ChecksumMismatch { expected: 1, actual: 0 });
        }
        Ok(FrameHeader { magic, version, flags, payload_len, crc32c })
    }

    pub fn encode_ack(&self, sequence: u64, status: u8) -> Vec<u8> {
        let mut buf = Vec::with_capacity(16);
        buf.extend_from_slice(&0xA11Cu16.to_be_bytes());
        buf.push(1);
        buf.push(status);
        buf.extend_from_slice(&sequence.to_be_bytes());
        buf.extend_from_slice(&0u32.to_be_bytes());
        buf
    }
}
