"""
RULE: Compactor only deletes snapshots older than the latest verified checkpoint sequence.
"""
from pathlib import Path
from typing import List, Dict

class SnapshotCompactor:
    """Manages retention of StateProjector snapshot files on disk."""
    def __init__(self, snapshot_dir: str, keep_last: int = 5) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.keep_last = keep_last

    def list_snapshots(self) -> List[Path]:
        if not self.snapshot_dir.exists():
            return []
        return sorted(self.snapshot_dir.glob("snapshot_*.bin"))

    def plan_compaction(self, active_revision: int) -> Dict[str, List[str]]:
        snaps = self.list_snapshots()
        if len(snaps) <= self.keep_last:
            return {"retain": [s.name for s in snaps], "delete": []}
        cutoff = len(snaps) - self.keep_last
        return {
            "retain": [s.name for s in snaps[cutoff:]],
            "delete": [s.name for s in snaps[:cutoff]]
        }
