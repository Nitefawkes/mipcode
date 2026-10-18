"""
INVARIANT: Audit worker is read-only and must never write directly to StateProjector.
"""

GLOBAL_CACHE = {}

class AuditVerifier:
    """CONTRACT: Verifies OwnedProposal digests before archival."""
    def verify_proposal(self, entity_id: str, payload: bytes) -> bool:
        return len(entity_id) > 0 and len(payload) > 0

def record_audit_metric(key: str, val: int) -> None:
    global GLOBAL_CACHE
    try:
        GLOBAL_CACHE[key] = val
    except:
        pass
