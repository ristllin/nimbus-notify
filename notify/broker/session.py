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

# Idle-eviction TTL: free the segment if no event arrives in this many seconds.
# This is the reaper for sessions that vanished WITHOUT a clean "end" event — a
# hard kill (closing the terminal, `kill`, force-quit) terminates the harness
# before its SessionEnd/`end` hook can run, so the broker never hears "Offline"
# and the session sticks at its last state. A graceful exit still frees the
# segment INSTANTLY via the hook; the TTL only catches the abrupt-kill case.
#
# TWO windows, keyed on the session's last state (a segment's last_event is only
# refreshed by inbound events, and a call-to-action fires exactly ONE event then
# goes quiet while it waits on the human — so a single TTL blind to state would
# reap a live "needs you" session):
#
#   SESSION_TTL_S (120 s, was 900 s) — benign/ambient states (Idle / Running /
#     Done). A killed session almost always lands here, so 2 min clears it
#     promptly. Trade-off: a genuinely idle-but-alive session is also dropped
#     after the TTL, but it reappears the instant it does anything. `--ttl` tunes
#     this window (see server.py).
#
#   CTA_TTL_S (900 s) — call-to-action states (WaitingInput / AwaitingApproval /
#     Error): a job blocked ON the human. Kept at the original 15 min so the
#     ring's highest-value signal can't vanish while it's still pending. A
#     killed CTA session therefore lingers longer — the safe direction (better
#     to over-show a pending job than to hide a live one). Never shorter than
#     the benign window (the broker clamps: max(benign_ttl, CTA_TTL_S)).
SESSION_TTL_S = 120.0
CTA_TTL_S     = 300.0   # was 900; owner red-ring round 2026-07-13 — matches the
                        # device's 5-min attention hold, and dead-PID eviction now
                        # clears killed sessions far sooner anyway

# States that mean "a human needs to act" (or a failure worth surfacing). Held
# on the ring for CTA_TTL_S, not the short SESSION_TTL_S. Mirrors the firmware's
# own CTA-hold design (input/approval/error never silently expire).
CTA_STATES: frozenset = frozenset({
    State.WaitingInput,
    State.AwaitingApproval,
    State.Error,
})


@dataclass
class SessionRecord:
    session_id: str
    harness:    str           # "claude" | "codex" | "vibe"
    cwd:        str
    state:      State = State.Idle
    segment:    int   = -1    # assigned by SegmentAllocator; -1 = unassigned
    pid:        int   = 0     # harness process id (led-report --pid); 0 = unknown.
    pid_alive_seen: bool = False  # pid was observed ALIVE at least once IN OUR PID
                              # namespace. Dead-pid eviction requires this: a hook
                              # inside a container reports a pid that never exists
                              # host-side — without the flag such LIVE sessions were
                              # evicted every sweep (review finding 2026-07-13).
                              # Lets the broker LIVENESS-CHECK a session: a dead pid
                              # is evicted on the next sweep instead of holding its
                              # (possibly red) segment for the full CTA TTL.
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
    "plan_pending":             State.WaitingInput,       # Codex plan-mode Stop = waiting on you
}


def verb_to_state(verb: str) -> State:
    """Map a canonical verb to a State.  Unknown verbs → Running (safe default)."""
    return _VERB_TO_STATE.get(verb, State.Running)
