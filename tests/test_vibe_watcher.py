"""VibeWatcher session detection + broker HITL wiring (no hardware, no threads).

Vibe exposes no session start/stop hook, so the broker runs a VibeWatcher on
~/.vibe/logs/session/.  These tests drive the watcher's scan/HITL logic
synchronously (never start the daemon thread) and confirm the broker feeds it
the before_tool/after_tool timing.
"""
from __future__ import annotations

import json

import notify.harness.vibe as vibe
from notify.broker.server import Broker
from notify.harness.vibe import VibeWatcher


class _NullTransport:
    def send(self, frame: bytes) -> bool:
        return True

    def close(self) -> None:
        pass


def _mk_session(root, name, session_id, cwd):
    d = root / name
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"session_id": session_id, "working_directory": cwd}))
    return d


# ------------------------------------------------------------------
# Watcher scan: new dir -> start, removed dir -> end
# ------------------------------------------------------------------

def test_new_session_dir_fires_start(tmp_path, monkeypatch):
    monkeypatch.setattr(vibe, "VIBE_SESSIONS", tmp_path)
    events: list[dict] = []
    w = VibeWatcher(events.append)

    _mk_session(tmp_path, "sess-A", "vibe-123", "/tmp/proj")
    w._scan_sessions()

    assert len(events) == 1
    assert events[0] == {"harness": "vibe", "session_id": "vibe-123",
                         "cwd": "/tmp/proj", "verb": "start"}


def test_removed_session_dir_fires_end(tmp_path, monkeypatch):
    monkeypatch.setattr(vibe, "VIBE_SESSIONS", tmp_path)
    events: list[dict] = []
    w = VibeWatcher(events.append)

    d = _mk_session(tmp_path, "sess-B", "vibe-9", "/tmp/p")
    w._scan_sessions()          # start
    (d / "meta.json").unlink(); d.rmdir()
    w._scan_sessions()          # end

    assert [e["verb"] for e in events] == ["start", "end"]


def test_scan_is_idempotent_between_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(vibe, "VIBE_SESSIONS", tmp_path)
    events: list[dict] = []
    w = VibeWatcher(events.append)
    _mk_session(tmp_path, "sess-C", "vibe-c", "/w")
    w._scan_sessions()
    w._scan_sessions()          # no change -> no new events
    assert len(events) == 1


# ------------------------------------------------------------------
# HITL inference: before_tool with no after_tool past the timeout
# ------------------------------------------------------------------

def test_hitl_timeout_fires_inferred(tmp_path, monkeypatch):
    monkeypatch.setattr(vibe, "VIBE_SESSIONS", tmp_path)
    # collapse the timeout so the check trips deterministically
    monkeypatch.setattr(vibe, "HITL_TIMEOUT_S", -1.0)
    events: list[dict] = []
    w = VibeWatcher(events.append)

    w.record_before_tool("vibe-x")
    w._check_hitl_timeouts()

    assert len(events) == 1
    assert events[0]["verb"] == "hitl_inferred"
    assert events[0]["session_id"] == "vibe-x"


def test_after_tool_cancels_hitl(tmp_path, monkeypatch):
    monkeypatch.setattr(vibe, "VIBE_SESSIONS", tmp_path)
    monkeypatch.setattr(vibe, "HITL_TIMEOUT_S", -1.0)
    events: list[dict] = []
    w = VibeWatcher(events.append)

    w.record_before_tool("vibe-y")
    w.record_after_tool("vibe-y")     # tool completed -> no HITL
    w._check_hitl_timeouts()

    assert events == []


# ------------------------------------------------------------------
# Broker routing: vibe tool verbs feed the watcher's HITL tracker
# ------------------------------------------------------------------

class _SpyWatcher:
    def __init__(self):
        self.before: list[str] = []
        self.after:  list[str] = []

    def record_before_tool(self, sid): self.before.append(sid)
    def record_after_tool(self, sid):  self.after.append(sid)


def test_broker_routes_before_and_after_tool_to_watcher():
    b = Broker(_NullTransport())
    spy = _SpyWatcher()
    b.vibe_watcher = spy

    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "before_tool"})
    b.handle_event({"harness": "vibe", "session_id": "s1",
                    "verb": "after_tool:success"})

    assert spy.before == ["s1"]
    assert spy.after == ["s1"]


def test_broker_ignores_hitl_routing_for_non_vibe():
    b = Broker(_NullTransport())
    spy = _SpyWatcher()
    b.vibe_watcher = spy
    # a claude before_tool-shaped verb must NOT touch the vibe tracker
    b.handle_event({"harness": "claude", "session_id": "c1", "verb": "running"})
    assert spy.before == [] and spy.after == []
