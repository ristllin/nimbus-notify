"""Vibe hook normalization + broker verb mapping (CUM-414 Tier 1).

Class tests, not instance tests:
  - every (old, new) alias pair normalizes identically from BOTH the payload's
    hook_event_name and the CLI argv verb;
  - every canonical + alias verb resolves in the broker map (an unmapped verb
    must fail the suite, so a future rename can't silently mis-map);
  - a unified-harness payload with an empty session_id still registers;
  - a late watcher `start` never downgrades a live session;
  - a user-denied tool (pre_tool with no post_tool) clears its amber on post_agent;
  - the captured Vibe 2.19.0 + 2.25.4 payloads drive the ring as golden fixtures.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import notify.harness.vibe as vibe
from notify.broker.server import Broker
from notify.broker.session import _VERB_TO_STATE, verb_to_state
from notify.harness.vibe import build_event, normalize_hook_name
from notify.state import State

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _NullTransport:
    def send(self, frame: bytes) -> bool:
        return True

    def close(self) -> None:
        pass


def _build_from_stdin(monkeypatch, verb: str, payload: dict):
    monkeypatch.setattr(vibe.sys, "stdin", io.StringIO(json.dumps(payload)))
    return build_event(verb)


# ---------------------------------------------------------------------------
# A. Normalization: old and new names collapse to the same canonical verb,
#    from BOTH the payload hook_event_name and the argv verb.
# ---------------------------------------------------------------------------

# (old_name, new_name, expected_canonical_verb_ignoring_tool_status)
_ALIAS_PAIRS = [
    ("before_tool",     "pre_tool",   "pre_tool"),
    ("after_tool",      "post_tool",  "post_tool"),
    ("post_agent_turn", "post_agent", "post_agent"),
]


@pytest.mark.parametrize("old, new, canon", _ALIAS_PAIRS)
def test_alias_pair_normalizes_identically(old, new, canon):
    assert normalize_hook_name(old) == canon
    assert normalize_hook_name(new) == canon  # new name is already canonical


@pytest.mark.parametrize("old, new, canon", _ALIAS_PAIRS)
def test_payload_name_drives_verb_old_and_new(monkeypatch, old, new, canon):
    base = {"session_id": "s", "cwd": "/p"}
    for name in (old, new):
        ev = _build_from_stdin(monkeypatch, "IGNORED_ARGV", {**base, "hook_event_name": name})
        expected = f"{canon}:success" if canon == "post_tool" else canon
        assert ev.verb == expected, f"payload {name!r} -> {ev.verb!r}"


@pytest.mark.parametrize("old, new, canon", _ALIAS_PAIRS)
def test_argv_verb_normalizes_when_no_payload_name(monkeypatch, old, new, canon):
    # No hook_event_name in the payload -> the argv verb is the fallback, normalized.
    for name in (old, new):
        ev = _build_from_stdin(monkeypatch, name, {"session_id": "s", "cwd": "/p"})
        expected = f"{canon}:success" if canon == "post_tool" else canon
        assert ev.verb == expected, f"argv {name!r} -> {ev.verb!r}"


def test_tool_status_appended_after_normalization(monkeypatch):
    for name in ("after_tool", "post_tool"):
        for status in ("success", "failure", "cancelled"):
            ev = _build_from_stdin(monkeypatch, "x",
                                   {"session_id": "s", "cwd": "/p",
                                    "hook_event_name": name, "tool_status": status})
            assert ev.verb == f"post_tool:{status}"


def test_unknown_future_payload_name_falls_back_to_argv(monkeypatch):
    # A future rename we don't know about must NOT crash and must fall back to the
    # argv verb (what the installer wrote), never silently mis-map to Running.
    ev = _build_from_stdin(monkeypatch, "post_agent",
                           {"session_id": "s", "cwd": "/p",
                            "hook_event_name": "brand_new_event_2027"})
    assert ev.verb == "post_agent"


# ---------------------------------------------------------------------------
# B. Broker verb map: every canonical + alias verb resolves; the specific
#    states CUM-414 requires; an unmapped verb must fail the suite.
# ---------------------------------------------------------------------------

def test_required_verb_states():
    assert verb_to_state("pre_tool") is State.Running
    assert verb_to_state("post_tool:success") is State.Running
    assert verb_to_state("post_tool:failure") is State.Error
    assert verb_to_state("post_tool:cancelled") is State.Running
    assert verb_to_state("post_agent") is State.Done
    # pre-2.21 aliases still resolve to the same states
    assert verb_to_state("before_tool") is State.Running
    assert verb_to_state("after_tool:success") is State.Running
    assert verb_to_state("after_tool:failure") is State.Error
    assert verb_to_state("post_agent_turn") is State.Done


@pytest.mark.parametrize("verb", [
    "pre_tool", "post_tool:success", "post_tool:failure", "post_tool:cancelled",
    "post_agent", "before_tool", "after_tool:success", "after_tool:failure",
    "post_agent_turn",
])
def test_every_vibe_verb_is_explicitly_mapped(verb):
    # An unmapped verb would fall through to the Running default, which is exactly
    # the bug (post_agent silently -> Running -> never Done). Assert each is EXPLICIT.
    assert verb in _VERB_TO_STATE, f"{verb!r} is not explicitly mapped"


def test_post_agent_reaches_done_not_running_default():
    # The headline regression: an unknown verb defaults to Running, so before this
    # fix post_agent (new name) rendered Running and a Vibe session never showed Done.
    assert _VERB_TO_STATE.get("post_agent") is State.Done


# ---------------------------------------------------------------------------
# C. Unified-harness payload (no session_id) still registers.
# ---------------------------------------------------------------------------

def test_empty_session_id_synthesizes_stable_cwd_key(monkeypatch):
    ev1 = _build_from_stdin(monkeypatch, "pre_tool",
                            {"cwd": "/work/proj", "hook_event_name": "pre_tool",
                             "tool_name": "file_system.bash"})
    ev2 = _build_from_stdin(monkeypatch, "post_agent",
                            {"cwd": "/work/proj", "hook_event_name": "post_agent"})
    assert ev1.session_id and ev2.session_id
    assert ev1.session_id == ev2.session_id           # stable per cwd
    assert ev1.session_id != "" and ev1.session_id.startswith("vibe-cwd-")


def test_empty_session_id_event_registers_on_broker(monkeypatch):
    b = Broker(_NullTransport())
    ev = _build_from_stdin(monkeypatch, "pre_tool",
                           {"cwd": "/work/proj", "hook_event_name": "pre_tool"})
    b.handle_event({"harness": ev.harness, "session_id": ev.session_id,
                    "cwd": ev.cwd, "verb": ev.verb})
    segs = b._allocator.active_segments()
    assert len(segs) == 1
    assert segs[0].state is State.Running
    assert segs[0].cwd == "/work/proj"


# ---------------------------------------------------------------------------
# D. `start` never downgrades a live session (late watcher/lease start).
# ---------------------------------------------------------------------------

def test_start_does_not_downgrade_running():
    b = Broker(_NullTransport())
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "/p", "verb": "pre_tool"})
    assert b._allocator._sessions["s1"].state is State.Running
    # a late watcher start (session dir/lease appeared after the hook) must NOT reset
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "/p", "verb": "start"})
    assert b._allocator._sessions["s1"].state is State.Running


def test_start_does_not_downgrade_done():
    b = Broker(_NullTransport())
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "/p", "verb": "post_agent"})
    assert b._allocator._sessions["s1"].state is State.Done
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "/p", "verb": "start"})
    assert b._allocator._sessions["s1"].state is State.Done


def test_start_enriches_empty_cwd_without_state_change():
    b = Broker(_NullTransport())
    # lease start fires before meta names the cwd -> registers Idle with empty cwd
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "", "verb": "start"})
    assert b._allocator._sessions["s1"].cwd == ""
    # a later start (cwd now known from meta) enriches it, still Idle
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "/proj", "verb": "start"})
    rec = b._allocator._sessions["s1"]
    assert rec.cwd == "/proj" and rec.state is State.Idle


def test_start_registers_new_session_as_idle():
    b = Broker(_NullTransport())
    b.handle_event({"harness": "vibe", "session_id": "s1", "cwd": "/p", "verb": "start"})
    assert b._allocator._sessions["s1"].state is State.Idle


# ---------------------------------------------------------------------------
# E. Denied-tool HITL: pre_tool then post_agent (no post_tool) clears the timer,
#    so no false amber is inferred.
# ---------------------------------------------------------------------------

class _SpyWatcher:
    def __init__(self):
        self.before: list[str] = []
        self.after:  list[str] = []

    def record_before_tool(self, sid): self.before.append(sid)
    def record_after_tool(self, sid):  self.after.append(sid)


def test_post_agent_clears_pending_hitl_timer():
    b = Broker(_NullTransport())
    spy = _SpyWatcher()
    b.vibe_watcher = spy
    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "pre_tool"})
    # user DENIES the tool: no post_tool ever arrives, the turn ends with post_agent
    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "post_agent"})
    assert spy.before == ["s1"]
    assert spy.after == ["s1"]            # post_agent cleared the pending timer


def test_post_tool_clears_pending_hitl_timer():
    b = Broker(_NullTransport())
    spy = _SpyWatcher()
    b.vibe_watcher = spy
    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "pre_tool"})
    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "post_tool:success"})
    assert spy.after == ["s1"]


def test_non_vibe_never_touches_hitl_tracker():
    b = Broker(_NullTransport())
    spy = _SpyWatcher()
    b.vibe_watcher = spy
    # a claude PreToolUse maps to `running`, and must NOT feed the vibe tracker
    b.handle_event({"harness": "claude", "session_id": "c1", "verb": "running"})
    assert spy.before == [] and spy.after == []


def test_hitl_denied_tool_end_to_end_no_amber():
    """Full denied-tool path through the real watcher: pre_tool arms the timer,
    post_agent clears it, so _check_hitl_timeouts never fires hitl_inferred."""
    events: list[dict] = []
    w = vibe.VibeWatcher(events.append, root=Path("/nonexistent"))
    b = Broker(_NullTransport())
    b.vibe_watcher = w
    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "pre_tool"})
    b.handle_event({"harness": "vibe", "session_id": "s1", "verb": "post_agent"})
    old = vibe.HITL_TIMEOUT_S
    vibe.HITL_TIMEOUT_S = -1.0   # would fire immediately IF anything were still pending
    try:
        w._check_hitl_timeouts()
    finally:
        vibe.HITL_TIMEOUT_S = old
    assert not any(e["verb"] == "hitl_inferred" for e in events)


# ---------------------------------------------------------------------------
# F. Golden fixtures: the REAL captured payloads drive the ring correctly.
# ---------------------------------------------------------------------------

def _feed_fixture(monkeypatch, rel: str, argv_verb: str):
    payload = json.loads((FIXTURES / rel).read_text())
    return _build_from_stdin(monkeypatch, argv_verb, payload)


def test_golden_2254_payloads(monkeypatch):
    pre = _feed_fixture(monkeypatch, "vibe-2.25.4/stdin_pre_tool_34833.json", "pre_tool")
    assert pre.verb == "pre_tool"
    assert pre.session_id == "940448b3-7744-cf33-bfa3-05410ffc227b"
    assert pre.cwd.endswith("/proj2254")

    post = _feed_fixture(monkeypatch, "vibe-2.25.4/stdin_post_tool_34839.json", "post_tool")
    assert post.verb == "post_tool:success"
    assert verb_to_state(post.verb) is State.Running

    agent = _feed_fixture(monkeypatch, "vibe-2.25.4/stdin_post_agent_34850.json", "post_agent")
    assert agent.verb == "post_agent"
    assert verb_to_state(agent.verb) is State.Done


def test_golden_2190_payloads(monkeypatch):
    # Pre-rename payloads: hook_event_name is the OLD name; must normalize to new.
    pre = _feed_fixture(monkeypatch, "vibe-2.19.0/stdin_before_tool_19072.json", "before_tool")
    assert pre.verb == "pre_tool"
    assert pre.session_id == "ae828734-2384-986a-3b83-2d9a36603c57"

    post = _feed_fixture(monkeypatch, "vibe-2.19.0/stdin_after_tool_19346.json", "after_tool")
    assert post.verb == "post_tool:success"

    agent = _feed_fixture(monkeypatch, "vibe-2.19.0/stdin_post_agent_turn_20790.json",
                          "post_agent_turn")
    assert agent.verb == "post_agent"
    assert verb_to_state(agent.verb) is State.Done
