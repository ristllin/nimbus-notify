"""VibeWatcher session detection: Tier 1 dir scan + Tier 2 lease (no hardware).

Vibe exposes no session start/stop hook, so the broker runs a VibeWatcher.  These
tests drive its scan/HITL logic synchronously (never start the daemon thread) with
an injected `root` (fully hermetic; never touches the real ~/.vibe), and use the
REAL meta.json shape (cwd nested at environment.working_directory, end_time stamped
on every save).  End is NEVER derived from end_time.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from notify.harness.vibe import VibeWatcher


def _mk_session(root, name, session_id, cwd, end_time="2026-09-16T17:43:02Z"):
    """A real Vibe session dir: name `session_*`, meta with NESTED cwd + end_time
    (Vibe stamps end_time on every interaction save, so it never means 'ended')."""
    d = root / name
    d.mkdir(parents=True)
    meta = {"session_id": session_id, "start_time": "2026-09-16T17:42:58Z",
            "end_time": end_time, "environment": {"working_directory": cwd}}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def _mk_lease(root, session_id, pid):
    active = root / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / ".registry").write_bytes(b"")   # Vibe's directory-lock file (a phantom today)
    (active / f"{session_id}.lock").write_text(json.dumps(
        {"lease_version": 1, "session_id": session_id, "process_id": pid,
         "acquired_at": "2026-09-16T17:42:58.000Z"}))
    return active / f"{session_id}.lock"


# ------------------------------------------------------------------
# Tier 1: dir scan (prime -> new dir -> start; never end_time-based end)
# ------------------------------------------------------------------

def test_first_scan_primes_no_start(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_session(tmp_path, "session_A", "vibe-123", "/tmp/proj")
    w._scan_sessions()                       # first scan = baseline, no flood
    assert events == []


def test_new_dir_after_prime_fires_start_with_nested_cwd(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w._scan_sessions()                       # prime (empty)
    _mk_session(tmp_path, "session_A", "vibe-123", "/tmp/proj")
    w._scan_sessions()                       # new dir -> start
    assert len(events) == 1
    assert events[0] == {"harness": "vibe", "session_id": "vibe-123",
                         "cwd": "/tmp/proj", "verb": "start"}


def test_end_time_never_fires_end(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w._scan_sessions()                       # prime
    d = _mk_session(tmp_path, "session_x", "uuid-x", "/p", end_time=None)
    w._scan_sessions()                       # start(uuid-x)
    # meta gains an end_time (a normal interaction save) -> must NOT fire end
    (d / "meta.json").write_text(json.dumps(
        {"session_id": "uuid-x", "end_time": "2026-09-16T18:00:00Z",
         "environment": {"working_directory": "/p"}}))
    w._scan_sessions()
    assert [e["verb"] for e in events] == ["start"]


def test_removed_dir_fires_end(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w._scan_sessions()                       # prime
    d = _mk_session(tmp_path, "session_B", "vibe-9", "/tmp/p")
    w._scan_sessions()                       # start
    (d / "meta.json").unlink(); d.rmdir()
    w._scan_sessions()                       # dir gone -> end
    assert [e["verb"] for e in events] == ["start", "end"]


def test_scan_idempotent_between_changes(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w._scan_sessions()                       # prime
    _mk_session(tmp_path, "session_C", "vibe-c", "/w")
    w._scan_sessions()                       # start
    w._scan_sessions()                       # no change -> no new events
    assert len(events) == 1


def test_cwd_reread_on_change_refires_start(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w._scan_sessions()                       # prime
    d = _mk_session(tmp_path, "session_D", "vibe-d", "")   # cwd empty at first
    w._scan_sessions()                       # start (empty cwd)
    (d / "meta.json").write_text(json.dumps(
        {"session_id": "vibe-d", "environment": {"working_directory": "/relocated"}}))
    w._scan_sessions()                       # cwd settled -> re-fire start with cwd
    starts = [e for e in events if e["verb"] == "start"]
    assert starts[-1]["cwd"] == "/relocated"


# ------------------------------------------------------------------
# Phantoms: a non-session dir must NEVER register
# ------------------------------------------------------------------

def test_non_session_dirs_never_register(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    # the 2.25 lease dir, a .last_session pointer dir, a dir with no meta, and a
    # session_ dir whose meta has no session_id; none of these are real sessions
    (tmp_path / "active").mkdir()
    (tmp_path / "active" / ".registry").write_bytes(b"")
    (tmp_path / ".last_session").mkdir()
    (tmp_path / "session_nometa").mkdir()
    d = tmp_path / "session_noid"; d.mkdir()
    (d / "meta.json").write_text(json.dumps({"environment": {"working_directory": "/x"}}))
    w._scan_sessions()   # prime
    w._scan_sessions()   # would fire start for any phantom
    assert events == []


# ------------------------------------------------------------------
# Tier 2: session-lease start/end (Vibe >= 2.25)
# ------------------------------------------------------------------

def test_lease_present_fires_start(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_session(tmp_path, "session_L", "lease-1", "/proj")   # meta for cwd
    _mk_lease(tmp_path, "lease-1", os.getpid())              # live lease (our pid)
    w._scan_leases()
    assert len(events) == 1
    assert events[0]["verb"] == "start"
    assert events[0]["session_id"] == "lease-1"
    assert events[0]["cwd"] == "/proj"
    assert events[0]["pid"] == os.getpid()


def test_lease_removed_fires_end(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    lock = _mk_lease(tmp_path, "lease-2", os.getpid())
    w._scan_leases()                         # start
    lock.unlink()                            # session closed -> lease released
    w._scan_leases()                         # end
    assert [e["verb"] for e in events] == ["start", "end"]
    assert events[-1]["session_id"] == "lease-2"


def test_stale_lease_dead_pid_never_registers(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_lease(tmp_path, "crashed", 999999)   # a pid that is not alive
    w._scan_leases()
    assert events == []                      # a stale lock after a crash is not live


def test_stale_lease_after_live_fires_end(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    lock = _mk_lease(tmp_path, "s3", os.getpid())
    w._scan_leases()                         # live -> start
    # simulate a crash: the lock stays but its pid dies (rewrite with a dead pid)
    lock.write_text(json.dumps({"lease_version": 1, "session_id": "s3",
                                "process_id": 999999, "acquired_at": "x"}))
    w._scan_leases()                         # dead pid -> end
    assert [e["verb"] for e in events] == ["start", "end"]


def test_registry_file_is_not_a_lease(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    active = tmp_path / "active"; active.mkdir()
    (active / ".registry").write_bytes(b"")   # only the dir-lock file, no .lock
    w._scan_leases()
    assert events == []


def test_no_lease_dir_is_tier1_only(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w._scan_leases()                         # no active/ dir -> no-op, no crash
    assert events == []


# ------------------------------------------------------------------
# HITL inference: pre_tool with no post_tool past the timeout
# ------------------------------------------------------------------

def test_hitl_timeout_fires_inferred(tmp_path, monkeypatch):
    import notify.harness.vibe as vibe
    monkeypatch.setattr(vibe, "HITL_TIMEOUT_S", -1.0)
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w.record_before_tool("vibe-x")
    w._check_hitl_timeouts()
    assert len(events) == 1
    assert events[0]["verb"] == "hitl_inferred"
    assert events[0]["session_id"] == "vibe-x"


def test_after_tool_cancels_hitl(tmp_path, monkeypatch):
    import notify.harness.vibe as vibe
    monkeypatch.setattr(vibe, "HITL_TIMEOUT_S", -1.0)
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    w.record_before_tool("vibe-y")
    w.record_after_tool("vibe-y")            # tool completed -> no HITL
    w._check_hitl_timeouts()
    assert events == []
