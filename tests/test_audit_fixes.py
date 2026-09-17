"""Regression tests for the 2026-07 UX-audit broker fixes."""
import json
import time

from notify.broker.segments import SegmentAllocator
from notify.broker.session import SessionRecord
from notify.harness.vibe import VibeWatcher
from notify.state import State


def _rec(sid, state=State.Running):
    return SessionRecord(session_id=sid, harness="claude", cwd="/x", state=state)


# --- order stability: new sessions APPEND; survivors never reshuffle -----------

def test_ring_order_is_arrival_not_slot():
    a = SegmentAllocator()
    a.register(_rec("A"))
    a.register(_rec("B"))
    a.register(_rec("C"))
    a.free("A")            # A's low slot (0) is now free
    a.register(_rec("D"))  # D recycles slot 0 — but must render LAST, not first
    order = [r.session_id for r in a.active_segments()]
    assert order == ["B", "C", "D"], f"new session jumped the queue: {order}"


def test_full_allocator_reports_minus_one():
    a = SegmentAllocator(max_segs=2)
    assert a.register(_rec("A")) >= 0
    assert a.register(_rec("B")) >= 0
    assert a.register(_rec("C")) == -1   # full -> caller logs, not a silent drop


# --- vibe watcher: no ghost flood on (re)start, end fires by session_id --------

def _make_vibe_tree(tmp_path, dirs):
    """dirs: {dir_name: meta_dict}. Returns the injected session root (hermetic)."""
    root = tmp_path / ".vibe" / "logs" / "session"
    root.mkdir(parents=True)
    for name, meta in dirs.items():
        d = root / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps(meta))
    return root


def test_first_scan_baselines_historical_dirs(tmp_path):
    # No-flood on (re)start: EVERY pre-existing dir is baselined silently, whatever
    # its end_time — end_time is no longer a liveness signal (Vibe stamps it on every
    # save). A session live at startup is caught by its ongoing hook events / lease.
    root = _make_vibe_tree(tmp_path, {
        "session_old1": {"session_id": "o1", "end_time": "2026-07-03T00:00:00Z",
                         "environment": {"working_directory": "/a"}},
        "session_old2": {"session_id": "o2", "end_time": "2026-07-04T00:00:00Z",
                         "environment": {"working_directory": "/b"}},
        "session_live": {"session_id": "liv", "end_time": None,
                         "environment": {"working_directory": "/proj"}},
    })
    events = []
    w = VibeWatcher(events.append, root=root)
    w._scan_sessions()   # first scan = baseline -> zero starts, no flood
    assert [e for e in events if e["verb"] == "start"] == []


def test_end_not_derived_from_end_time(tmp_path):
    # The bug this replaces: Vibe stamps end_time on every save, so treating a
    # non-null end_time as "ended" fired end on every live session. It must not.
    root = tmp_path / ".vibe" / "logs" / "session"
    root.mkdir(parents=True)
    events = []
    w = VibeWatcher(events.append, root=root)
    w._scan_sessions()   # prime (empty)
    d = root / "session_x"; d.mkdir()
    d.joinpath("meta.json").write_text(json.dumps(
        {"session_id": "uuid-x", "end_time": None,
         "environment": {"working_directory": "/p"}}))
    w._scan_sessions()   # new dir -> start(uuid-x)
    d.joinpath("meta.json").write_text(json.dumps(
        {"session_id": "uuid-x", "end_time": "2026-07-15T00:00:00Z",
         "environment": {"working_directory": "/p"}}))
    w._scan_sessions()   # end_time now set -> must NOT fire end
    assert [e["verb"] for e in events] == ["start"], f"end_time wrongly ended: {events}"


def test_hitl_pending_is_lock_guarded():
    # record_/pop across the lock must not raise (smoke: the API is present + safe).
    w = VibeWatcher(lambda e: None)
    w.record_before_tool("s1")
    w.record_after_tool("s1")
    w._check_hitl_timeouts()   # empty -> no fire, no raise
