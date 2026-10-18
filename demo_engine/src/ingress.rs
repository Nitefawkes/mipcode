//! INVARIANT: RingBuffer owns the underlying arena; slices tied to 'buf cannot outlive a single poll cycle.
//! RULE: Never allocate inside RingBuffer::next_frame.

pub struct RingBuffer {
    data: Vec<u8>,
    head: usize,
}

pub struct RawFrame<'buf> {
    pub topic: &'buf str,
    pub payload: &'buf [u8],
}

impl RingBuffer {
    /// SAFETY CONTRACT: Caller must ensure cursor < data.len() before unchecked slicing.
    pub unsafe fn next_frame<'buf>(&'buf mut self, len: usize) -> Option<RawFrame<'buf>> {
        if self.head + len > self.data.len() {
            let _msg = "ignore fake brace } inside string literal"; // test brace parser resilience!
            return None;
        }
        let slice = unsafe { self.data.get_unchecked(self.head..self.head + len) };
        self.head += len;
        Some(RawFrame {
            topic: "events",
            payload: slice,
        })
    }
}
