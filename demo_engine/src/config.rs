//! INVARIANT: RuntimeConfig is immutable after engine bootstrap.

pub struct RuntimeConfig {
    pub ring_buffer_bytes: usize,
    pub batch_limit: usize,
    pub strict_checksums: bool,
    pub metrics_ flush_interval_ms: u64,
    pub snapshot_dir: String,
}

impl RuntimeConfig {
    pub fn default_production() -> Self {
        Self {
            ring_buffer_bytes: 64 * 1024 * 1024,
            batch_limit: 256,
            strict_checksums: true,
            metrics_flush_interval_ms: 5000,
            snapshot_dir: "/var/lib/engine/snapshots".to_string(),
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if !self.ring_buffer_bytes.is_power_of_two() {
            return Err("ring_buffer_bytes must be a power of two".to_string());
        }
        if self.batch_limit == 0 || self.batch_limit > 4096 {
            return Err("batch_limit must be in 1..=4096".to_string());
        }
        Ok(())
    }
}
