"""VibeWatcher session detection: Tier 1 dir scan + Tier 2 lease (no hardware).

Vibe exposes no session start/stop hook, so the broker runs a VibeWatcher.  These
tests drive its scan/HITL logic synchronously (never start the daemon thread) with
an injected `root` (fully hermetic; never touches the real ~/.vibe), and use the
REAL meta.json shape (cwd nested at environment.working_directory, end_time stamped
on every save).  End is NEVER derived from end_time.
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import notify.harness.vibe as vibe
from notify.harness.vibe import VibeWatcher, _synth_session_key, build_event


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


def _mk_unified(root, session_id, cwd, origin=None):
    """A 2.25+ unified-harness session's meta at unified/<id>/meta.json (UUID-named,
    NOT session_*, so Tier 1 never sees it; its lease is keyed by the cwd-hash its
    hooks synthesize).  The lock lives in active/ like any lease (call _mk_lease
    with the SAME id).  cwd may be empty to model meta landing a beat before the
    cwd is known; origin defaults to cwd (Vibe's origin_directory fallback)."""
    d = root / "unified" / session_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"session_id": session_id,
            "environment": {"working_directory": cwd},
            "origin_directory": origin or cwd}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


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


def _mk_sidecar_lease(root, session_id, pid):
    """Newer Vibe lease shape: an EMPTY <id>.lock (the flock target) and the
    holder's pid in a <id>.lock.json sidecar."""
    active = root / "active"
    active.mkdir(parents=True, exist_ok=True)
    lock = active / f"{session_id}.lock"
    lock.write_bytes(b"")
    (active / f"{session_id}.lock.json").write_text(json.dumps(
        {"lease_version": 1, "session_id": session_id, "process_id": pid,
         "acquired_at": "2026-10-09T13:51:00.000Z"}))
    return lock


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_sidecar_lease_live_pid_fires_start(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_session(tmp_path, "session_S", "side-1", "/proj")
    _mk_sidecar_lease(tmp_path, "side-1", os.getpid())
    w._scan_leases()
    assert [(e["verb"], e["session_id"], e.get("pid")) for e in events] == [
        ("start", "side-1", os.getpid())]


def test_sidecar_lease_dead_pid_never_registers(tmp_path):
    # The phantom-segment bug: an empty .lock read as pid 0 counted as live, so a
    # crashed session's leftover lock was heartbeated forever and never evicted.
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _age(_mk_sidecar_lease(tmp_path, "dead-side", 999999), 3600)
    w._scan_leases()
    assert events == []


def test_sidecar_pid_dies_after_live_fires_end(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    lock = _mk_sidecar_lease(tmp_path, "s4", os.getpid())
    w._scan_leases()                         # live -> start
    _mk_sidecar_lease(tmp_path, "s4", 999999)
    w._scan_leases()                         # sidecar pid dead -> end
    assert [e["verb"] for e in events] == ["start", "end"]
    assert lock.exists()


def test_pidless_lease_old_is_not_live(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    active = tmp_path / "active"; active.mkdir()
    lock = active / "orphan.lock"; lock.write_bytes(b"")   # no pid anywhere
    _age(lock, 3600)
    w._scan_leases()
    assert events == []


def test_pidless_lease_fresh_gets_grace(tmp_path):
    # A just-acquired lock whose sidecar isn't written yet must not be dropped.
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    active = tmp_path / "active"; active.mkdir()
    (active / "young.lock").write_bytes(b"")
    w._scan_leases()
    assert [e["session_id"] for e in events] == ["young"]


def test_sidecar_file_is_not_a_lease(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    lock = _mk_sidecar_lease(tmp_path, "s5", os.getpid())
    w._scan_leases()
    assert [e["session_id"] for e in events] == ["s5"]   # no "s5.lock" phantom
    assert lock.with_name("s5.lock.json").exists()


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
# Tier 2: unified-harness lease (Vibe >= 2.25): keyed by the SAME
# vibe-cwd-<hash> the hooks synthesize (NOT the raw uuid), heartbeated
# while live, ref-counted across same-cwd leases.
# ------------------------------------------------------------------

def test_unified_lease_keys_by_cwd_hash_not_uuid(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-unified", "/u/proj")   # unified meta -> cwd
    _mk_lease(tmp_path, "uuid-unified", os.getpid())   # live lease (our pid)
    w._scan_leases()
    assert len(events) == 1
    assert events[0]["verb"] == "start"
    assert events[0]["session_id"] == _synth_session_key("/u/proj")
    assert events[0]["session_id"].startswith("vibe-cwd-")
    assert events[0]["session_id"] != "uuid-unified"        # NOT the raw uuid
    assert events[0]["cwd"] == "/u/proj"
    assert events[0]["pid"] == os.getpid()


def test_unified_lease_key_matches_hook_synthesized_key(tmp_path, monkeypatch):
    # The crux: the watcher lease key MUST equal the key build_event synthesizes
    # for a unified hook payload with the same cwd, or the lease and the hook
    # events split into a phantom duplicate instead of collapsing to one segment.
    cwd = "/u/collapse"
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-x", cwd)
    _mk_lease(tmp_path, "uuid-x", os.getpid())
    w._scan_leases()
    watcher_key = events[0]["session_id"]

    monkeypatch.setattr(vibe.sys, "stdin",
                        io.StringIO(json.dumps({"cwd": cwd, "hook_event_name": "pre_tool"})))
    hook_key = build_event("pre_tool").session_id

    assert watcher_key == hook_key
    assert watcher_key.startswith("vibe-cwd-")


def test_unified_live_lease_emits_heartbeat_not_duplicate_start(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-hb", "/u/hb")
    _mk_lease(tmp_path, "uuid-hb", os.getpid())
    w._scan_leases()                         # start
    w._scan_leases()                         # still live -> heartbeat, NOT a 2nd start
    assert [e["verb"] for e in events] == ["start", "heartbeat"]
    assert events[-1]["session_id"] == _synth_session_key("/u/hb")
    assert events[-1]["pid"] == os.getpid()  # heartbeat carries the pid too


def test_unified_lease_release_fires_end_under_cwd_key(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-end", "/u/end")
    lock = _mk_lease(tmp_path, "uuid-end", os.getpid())
    w._scan_leases()                         # start
    lock.unlink()                            # session closed -> lease released
    w._scan_leases()                         # end
    assert [e["verb"] for e in events] == ["start", "end"]
    assert events[-1]["session_id"] == _synth_session_key("/u/end")   # cwd key, not uuid


def test_two_unified_leases_same_cwd_collapse_end_when_last_gone(tmp_path):
    # Two unified sessions in the SAME cwd share one vibe-cwd-<hash>: ONE start, and
    # the key ends only when the LAST lease releases (recompute-each-sweep is the
    # ref count; no explicit counter needed).
    cwd = "/u/shared"
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-a", cwd)
    _mk_unified(tmp_path, "uuid-b", cwd)
    lock_a = _mk_lease(tmp_path, "uuid-a", os.getpid())
    _mk_lease(tmp_path, "uuid-b", os.getpid())
    w._scan_leases()                         # ONE start for the shared key
    assert [e["verb"] for e in events] == ["start"]
    assert events[0]["session_id"] == _synth_session_key(cwd)

    lock_a.unlink()                          # one gone, one still live
    w._scan_leases()                         # heartbeat, NO end
    assert [e["verb"] for e in events] == ["start", "heartbeat"]

    (tmp_path / "active" / "uuid-b.lock").unlink()   # last one gone
    w._scan_leases()                         # NOW end
    assert [e["verb"] for e in events] == ["start", "heartbeat", "end"]
    assert events[-1]["session_id"] == _synth_session_key(cwd)


def test_unified_lease_without_cwd_in_meta_is_deferred(tmp_path):
    # meta.json can land a beat after the lock; until it names a cwd the key would
    # be bogus, so a cwd-less unified lease is DEFERRED (not mis-keyed), then picked
    # up on the sweep after the cwd appears.
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-late", "")   # meta present but NO cwd yet
    _mk_lease(tmp_path, "uuid-late", os.getpid())
    w._scan_leases()
    assert events == []                      # deferred, not registered under a bad key

    _mk_unified(tmp_path, "uuid-late", "/late/cwd")   # cwd finally written
    w._scan_leases()
    assert [e["verb"] for e in events] == ["start"]
    assert events[0]["session_id"] == _synth_session_key("/late/cwd")


def test_unified_lease_cwd_falls_back_to_origin_directory(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-orig", "", origin="/from/origin")   # cwd empty, origin set
    _mk_lease(tmp_path, "uuid-orig", os.getpid())
    w._scan_leases()
    assert len(events) == 1
    assert events[0]["cwd"] == "/from/origin"
    assert events[0]["session_id"] == _synth_session_key("/from/origin")


def test_legacy_lease_keeps_uuid_key_and_heartbeats_when_live(tmp_path):
    # A legacy (non-unified) session has a session_* dir and NO unified/ meta: its
    # lease keeps the RAW uuid key (unchanged), and it too heartbeats while live.
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_session(tmp_path, "session_legacy", "legacy-uuid", "/legacy/cwd")
    _mk_lease(tmp_path, "legacy-uuid", os.getpid())
    w._scan_leases()                         # start
    w._scan_leases()                         # heartbeat
    assert [e["verb"] for e in events] == ["start", "heartbeat"]
    assert all(e["session_id"] == "legacy-uuid" for e in events)   # raw uuid, not cwd-hash


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


def test_unified_dir_without_meta_is_deferred_not_raw_uuid(tmp_path):
    # The lock (and unified/<id>/ dir) can land before meta.json: defer it rather
    # than registering a raw-uuid duplicate beside the hooks' vibe-cwd segment.
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    (tmp_path / "unified" / "child-1").mkdir(parents=True)
    _mk_lease(tmp_path, "child-1", os.getpid())
    w._scan_leases()
    assert events == []
    _mk_unified(tmp_path, "child-1", "/proj")
    w._scan_leases()
    assert [(e["verb"], e["session_id"]) for e in events] == [
        ("start", _synth_session_key("/proj"))]


def test_unified_meta_mid_rewrite_keeps_key_no_flap(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    d = _mk_unified(tmp_path, "uuid-rw", "/proj")
    _mk_lease(tmp_path, "uuid-rw", os.getpid())
    w._scan_leases()                         # start
    (d / "meta.json").write_text("{")        # torn write: cwd briefly unreadable
    w._scan_leases()                         # must heartbeat, NOT end + start
    assert [e["verb"] for e in events] == ["start", "heartbeat"]
    assert {e["session_id"] for e in events} == {_synth_session_key("/proj")}


def test_unified_meta_bad_utf8_does_not_abort_sweep(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    d = tmp_path / "unified" / "uuid-bin"; d.mkdir(parents=True)
    (d / "meta.json").write_bytes(b"\xff\xfe\x00garbage")
    _mk_lease(tmp_path, "uuid-bin", os.getpid())
    _mk_session(tmp_path, "session_ok", "legacy-ok", "/ok")
    _mk_lease(tmp_path, "legacy-ok", os.getpid())
    w._scan_leases()                         # no UnicodeDecodeError escapes
    assert [e["session_id"] for e in events] == ["legacy-ok"]


def test_unified_cwd_change_ends_old_key_before_starting_new(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    _mk_unified(tmp_path, "uuid-mv", "/a")
    _mk_lease(tmp_path, "uuid-mv", os.getpid())
    w._scan_leases()
    _mk_unified(tmp_path, "uuid-mv", "/b")
    w._scan_leases()
    assert [(e["verb"], e["session_id"]) for e in events] == [
        ("start", _synth_session_key("/a")),
        ("end",   _synth_session_key("/a")),     # frees the slot first
        ("start", _synth_session_key("/b")),
    ]


def test_sidecar_briefly_missing_keeps_live_lease(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    lock = _mk_sidecar_lease(tmp_path, "s6", os.getpid())
    _age(lock, 3600)                         # established lease, past the grace
    w._scan_leases()                         # start
    lock.with_name("s6.lock.json").unlink()  # sidecar momentarily gone
    w._scan_leases()                         # remembered pid -> heartbeat, no end
    assert [e["verb"] for e in events] == ["start", "heartbeat"]


def test_lease_pid_rejects_bool_and_out_of_range(tmp_path):
    active = tmp_path / "active"; active.mkdir()
    lock = active / "x.lock"; lock.write_bytes(b"")
    side = active / "x.lock.json"
    for bad in ("true", "1e400", "99999999999999999999", "-5", '"12"', "null", "[]"):
        side.write_text(f'{{"process_id": {bad}}}')
        assert VibeWatcher._lease_pid(lock) == 0, bad
    side.write_text('{"process_id": 4242}')
    assert VibeWatcher._lease_pid(lock) == 4242


def test_huge_pid_does_not_abort_sweep(tmp_path):
    events: list[dict] = []
    w = VibeWatcher(events.append, root=tmp_path)
    active = tmp_path / "active"; active.mkdir()
    (active / "big.lock").write_text('{"process_id": 99999999999999999999}')
    _age(active / "big.lock", 3600)
    _mk_session(tmp_path, "session_ok", "ok-1", "/ok")
    _mk_lease(tmp_path, "ok-1", os.getpid())
    w._scan_leases()
    assert [e["session_id"] for e in events] == ["ok-1"]
