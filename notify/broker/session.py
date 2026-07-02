"""
Phase 2 — Per-session state records and the verb→State mapping used by all
harness adapters (Phase 3 / 4).

A "verb" is the short action tag that led-report or the harness adapter sends
to the broker (e.g. "start", "running", "done").  The broker maps it to a State
here so that harness adapters don't need to import state.py directly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from notify.state import State

# Default TTL: free the segment if no event arrives in 15 minutes.
SESSION_TTL_S = 900.0


@dataclass
class SessionRecord:
    session_id: str
    harness:    str           # "claude" | "codex" | "vibe"
    cwd:        str
    state:      State = State.Idle
    segment:    int   = -1    # assigned by SegmentAllocator; -1 = unassigned
    last_event: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_event = time.monotonic()

    def is_stale(self, ttl: float = SESSION_TTL_S) -> bool:
        return (time.monotonic() - self.last_event) > ttl


# ---------------------------------------------------------------------------
# Verb → State mapping
# ---------------------------------------------------------------------------

# Each harness adapter sends a verb to the broker.  The mapping here is
# intentionally flat — adapters normalise harness-specific events to these
# canonical verbs before calling the broker.
_VERB_TO_STATE: dict[str, State] = {
    # Lifecycle
    "start":              State.Idle,
    "end":                State.Offline,
    # Progress
    "running":            State.Running,
    "before_tool":        State.Running,    # Vibe: tool about to execute
    "after_tool:success": State.Running,    # Vibe: tool done, more may follow
    "after_tool:failure": State.Error,
    "post_agent_turn":    State.Done,       # Vibe: turn complete
    # Completion
    "done":               State.Done,
    "error":              State.Error,
    # HITL
    "approval":           State.AwaitingApproval,  # Codex PermissionRequest
    "notify:permission_prompt": State.AwaitingApproval,   # Claude Notification
    "notify:elicitation_dialog":State.WaitingInput,
    "notify:idle_prompt":       State.WaitingInput,       # Claude idle 60 s
    "hitl_inferred":            State.AwaitingApproval,   # Vibe heuristic
}


def verb_to_state(verb: str) -> State:
    """Map a canonical verb to a State.  Unknown verbs → Running (safe default)."""
    return _VERB_TO_STATE.get(verb, State.Running)
