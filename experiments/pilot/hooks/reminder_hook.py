"""Stop hook for the reminder arm: block once with a generic consistency reminder.

It runs no analysis. It is the control for the ledger arm's completion check, so that a
difference between the two arms is not just the effect of being asked to look again.
"""
import json
import sys

REMINDER = (
    "Before finishing, review your change once more. Check that every caller, wrapper, "
    "subclass override, and sibling implementation affected by what you changed is "
    "consistent with it. If everything is consistent, say so and finish."
)

try:
    event = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    event = {}
if isinstance(event, dict) and not event.get("stop_hook_active"):
    print(json.dumps({"decision": "block", "reason": REMINDER}))
