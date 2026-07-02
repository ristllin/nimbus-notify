"""Tests for segment allocator: assignment, priority, eviction."""
import time

from notify.broker.segments import SegmentAllocator
from notify.broker.session import SessionRecord
from notify.state import State


def _rec(sid, state=State.Running, cwd="/x"):
    return SessionRecord(session_id=sid, harness="claude", cwd=cwd, state=state)


def test_register_assigns_lowest_free_index():
    alloc = SegmentAllocator(max_segs=4)
    r0 = _rec("s0")
    r1 = _rec("s1")
    alloc.register(r0)
    alloc.register(r1)
    assert r0.segment == 0
    assert r1.segment == 1


def test_free_returns_index_to_pool():
    alloc = SegmentAllocator(max_segs=2)
    alloc.register(_rec("s0"))
    alloc.register(_rec("s1"))
    alloc.free("s0")
    r2 = _rec("s2")
    alloc.register(r2)
    assert r2.segment == 0  # recycled


def test_register_same_id_is_idempotent():
    alloc = SegmentAllocator(max_segs=4)
    r = _rec("s0")
    idx1 = alloc.register(r)
    idx2 = alloc.register(r)
    assert idx1 == idx2
    assert len(alloc) == 1


def test_full_allocator_returns_minus_one():
    alloc = SegmentAllocator(max_segs=2)
    alloc.register(_rec("s0"))
    alloc.register(_rec("s1"))
    r = _rec("s2")
    idx = alloc.register(r)
    assert idx == -1


def test_active_segments_ordered_by_index():
    alloc = SegmentAllocator(max_segs=4)
    for i in range(3):
        alloc.register(_rec(f"s{i}"))
    alloc.free("s1")
    alloc.register(_rec("s3"))
    segments = alloc.active_segments()
    indices = [r.segment for r in segments]
    assert indices == sorted(indices)


def test_highest_priority_state():
    alloc = SegmentAllocator(max_segs=4)
    alloc.register(_rec("s0", State.Running))
    alloc.register(_rec("s1", State.AwaitingApproval))
    alloc.register(_rec("s2", State.Done))
    assert alloc.highest_priority_state() == State.AwaitingApproval


def test_evict_stale_removes_session(monkeypatch):
    alloc = SegmentAllocator(max_segs=4)
    r = _rec("s0")
    alloc.register(r)

    import notify.broker.session as mod
    monkeypatch.setattr(mod.time, "monotonic",
                        lambda: r.last_event + mod.SESSION_TTL_S + 1)

    evicted = alloc.evict_stale()
    assert "s0" in evicted
    assert len(alloc) == 0
