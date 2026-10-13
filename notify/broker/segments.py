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

from notify.broker.session import (
    CTA_STATES,
    CTA_TTL_S,
    RUNNING_LIVE_TTL_S,
    SESSION_TTL_S,
    SessionRecord,
)
from notify.state import State, STATE_PRIORITY

MAX_SEGS = 16


class SegmentAllocator:
    def __init__(self, max_segs: int = MAX_SEGS) -> None:
        self._max = max_segs
        self._sessions: dict[str, SessionRecord] = {}  # session_id → record
        self._index: dict[str, int] = {}               # session_id → capacity slot (0..max-1)
        self._used: set[int] = set()
        # Ring ORDER is arrival order, NOT the capacity slot: a freed low slot gets
        # recycled, so ordering by slot made a NEW session insert BEFORE existing
        # arcs (and survivors shift when a low-slot session ends) — "my codex arc
        # jumps around" every birth/death. A monotonic birth stamp keeps arrival
        # order stable; the slot stays pure capacity bookkeeping.
        self._order: dict[str, int] = {}               # session_id → birth sequence
        self._seq = 0

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
        self._seq += 1
        self._order[record.session_id]    = self._seq
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
        self._order.pop(session_id, None)

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
        A Running session whose pid was seen alive gets the longer
        :data:`~notify.broker.session.RUNNING_LIVE_TTL_S` window (it is mid tool
        call; the broker's dead-pid sweep, which runs first, catches a kill).
        """
        def window(rec: SessionRecord) -> float:
            if rec.state in CTA_STATES:
                return cta_ttl
            if rec.state == State.Running and rec.pid_alive_seen:
                return max(ttl, RUNNING_LIVE_TTL_S)
            return ttl

        stale = [sid for sid, rec in self._sessions.items() if rec.is_stale(window(rec))]
        for sid in stale:
            self.free(sid)
        return stale

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def active_segments(self) -> list[SessionRecord]:
        """Sessions in ARRIVAL order (stable ring position — new sessions append,
        existing arcs never reshuffle when another is born)."""
        return sorted(self._sessions.values(),
                      key=lambda r: self._order.get(r.session_id, 0))

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
