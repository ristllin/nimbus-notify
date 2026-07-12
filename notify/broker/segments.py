"""
Phase 2 — Segment allocator.

Rules:
  - Each live session occupies exactly one segment index (0..MAX_SEGS-1).
  - Segments are assigned in insertion order (lowest free index wins).
  - A segment showing AWAITING_APPROVAL or WAITING_INPUT is never evicted to
    make room for a new session while high-priority slots are full — the new
    session is assigned the next available index instead.
  - On free(), the index is returned to the pool immediately.
  - active_segments() returns SessionRecords ordered by segment index, which
    determines ring position.
"""
from __future__ import annotations

from notify.broker.session import CTA_STATES, CTA_TTL_S, SESSION_TTL_S, SessionRecord
from notify.state import State, STATE_PRIORITY

MAX_SEGS = 16


class SegmentAllocator:
    def __init__(self, max_segs: int = MAX_SEGS) -> None:
        self._max = max_segs
        self._sessions: dict[str, SessionRecord] = {}  # session_id → record
        self._index: dict[str, int] = {}               # session_id → segment index
        self._used: set[int] = set()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, record: SessionRecord) -> int:
        """Assign a segment to a new session.  Returns the segment index, or -1 if full."""
        if record.session_id in self._index:
            return self._index[record.session_id]

        idx = self._next_free()
        if idx < 0:
            return -1

        self._sessions[record.session_id] = record
        self._index[record.session_id]    = idx
        self._used.add(idx)
        record.segment = idx
        return idx

    def update(self, record: SessionRecord) -> None:
        """Update state for an already-registered session."""
        sid = record.session_id
        if sid not in self._sessions:
            self.register(record)
            return
        self._sessions[sid].state      = record.state
        self._sessions[sid].last_event = record.last_event

    def free(self, session_id: str) -> None:
        """Release the segment held by session_id."""
        idx = self._index.pop(session_id, None)
        if idx is not None:
            self._used.discard(idx)
        self._sessions.pop(session_id, None)

    def evict_stale(self, ttl: float = SESSION_TTL_S,
                    cta_ttl: float = CTA_TTL_S) -> list[str]:
        """Remove idle sessions.  Returns evicted ids.

        Two windows, keyed on state: benign/ambient states (Idle/Running/Done)
        use ``ttl``; call-to-action states (:data:`~notify.broker.session.CTA_STATES`
        — a job blocked ON the human) use the longer ``cta_ttl`` so the ring's
        "needs you" signal can't be reaped while it's still pending. Both default
        to the module constants; the broker passes its own (possibly
        ``--ttl``-overridden) values. Catches sessions killed without a clean
        ``end`` — the common killed case (Idle/Running) clears at ``ttl``.
        """
        stale = [
            sid for sid, rec in self._sessions.items()
            if rec.is_stale(cta_ttl if rec.state in CTA_STATES else ttl)
        ]
        for sid in stale:
            self.free(sid)
        return stale

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def active_segments(self) -> list[SessionRecord]:
        """Sessions ordered by segment index (ring position)."""
        return sorted(self._sessions.values(), key=lambda r: self._index[r.session_id])

    def highest_priority_state(self) -> State:
        """State with the highest priority among all active sessions."""
        if not self._sessions:
            return State.Idle
        return max(
            (r.state for r in self._sessions.values()),
            key=lambda s: STATE_PRIORITY[s],
        )

    def __len__(self) -> int:
        return len(self._sessions)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_free(self) -> int:
        for i in range(self._max):
            if i not in self._used:
                return i
        return -1
