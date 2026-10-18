//! INVARIANT: RouteHandler::handle executes synchronously inside the hot ring-buffer loop.
use crate::ingress::{RingBuffer, RawFrame};
use crate::projection::OwnedProposal;

/// CONTRACT: Must not hold RawFrame<'buf> across async suspension points.
pub trait RouteHandler<'buf>: Send + Sync {
    fn handle(&self, frame: &RawFrame<'buf>) -> Result<OwnedProposal, String>;
}

pub struct Router<H> {
    handler: H,
}

impl<'buf, H: RouteHandler<'buf>> Router<H> {
    pub async fn dispatch_batch(&self, buf: &'buf mut RingBuffer) -> Vec<OwnedProposal> {
        let mut out = Vec::new();
        while let Some(frame) = unsafe { buf.next_frame(64) } {
            if let Ok(prop) = self.handler.handle(&frame) {
                out.push(prop);
            }
        }
        out
    }
}
