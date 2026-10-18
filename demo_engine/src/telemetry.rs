//! RULE: Telemetry spans must never include raw payload bytes or secret tokens.

pub struct SpanContext {
    pub trace_id: u128,
    pub span_id: u64,
    pub sampled: bool,
}

pub struct MetricsCollector {
    pub frames_processed: u64,
    pub frames_dropped: u64,
    pub proposals_committed: u64,
    pub latency_histogram_us: [u64; 16],
}

impl MetricsCollector {
    pub fn new() -> Self {
        Self {
            frames_processed: 0,
            frames_dropped: 0,
            proposals_committed: 0,
            latency_histogram_us: [0; 16],
        }
    }

    pub fn record_latency(&mut self, micros: u64) {
        let bucket = (64 - micros.leading_zeros() as usize).min(15);
        self.latency_histogram_us[bucket] += 1;
        self.frames_processed += 1;
    }

    pub fn record_drop(&mut self) {
        self.frames_dropped += 1;
    }

    pub fn export_prometheus(&self) -> String {
        let mut out = String::with_capacity(512);
        out.push_str(&format!("engine_frames_processed_total {}\n", self.frames_processed));
        out.push_str(&format!("engine_frames_dropped_total {}\n", self.frames_dropped));
        out.push_str(&format!("engine_proposals_committed_total {}\n", self.proposals_committed));
        for (idx, count) in self.latency_histogram_us.iter().enumerate() {
            out.push_str(&format!("engine_latency_bucket{{le=\"{}\"}}\t{}\n", 1u64 << idx, count));
        }
        out
    }
}
