//! CANONICAL RULE: Only StateProjector::apply_verified may mutate canonical state snapshots.

#[derive(Clone, Debug)]
pub struct OwnedProposal {
    pub entity_id: String,
    pub delta_bytes: Vec<u8>,
}

pub struct StateProjector {
    revision: u64,
}

impl StateProjector {
    /// INVARIANT: Revision counter must increase monotonically per applied OwnedProposal.
    pub fn apply_verified(&mut self, proposal: OwnedProposal) -> u64 {
        if !proposal.delta_bytes.is_empty() {
            self.revision += 1;
        }
        self.revision
    }
}
