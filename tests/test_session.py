"""Tests for session state machine and verb→State mapping."""
import time

from notify.broker.session import SESSION_TTL_S, SessionRecord, verb_to_state
from notify.state import State


def test_verb_to_state_known():
    assert verb_to_state("start")              == State.Idle
    assert verb_to_state("running")            == State.Running
    assert verb_to_state("done")               == State.Done
    assert verb_to_state("end")                == State.Offline
    assert verb_to_state("approval")           == State.AwaitingApproval
    assert verb_to_state("notify:idle_prompt") == State.WaitingInput
    assert verb_to_state("notify:permission_prompt") == State.AwaitingApproval


def test_verb_to_state_unknown_defaults_to_running():
    assert verb_to_state("bogus_verb") == State.Running


def test_session_record_not_stale_immediately():
    rec = SessionRecord(session_id="a", harness="claude", cwd="/foo", state=State.Running)
    assert not rec.is_stale()


def test_session_record_stale_after_ttl(monkeypatch):
    rec = SessionRecord(session_id="a", harness="claude", cwd="/foo", state=State.Running)
    # Simulate time passing beyond TTL.
    monkeypatch.setattr(
        "notify.broker.session.time",
        type("T", (), {"monotonic": staticmethod(lambda: rec.last_event + SESSION_TTL_S + 1)})(),
    )
    assert rec.is_stale()


def test_touch_resets_staleness(monkeypatch):
    rec = SessionRecord(session_id="a", harness="claude", cwd="/foo", state=State.Running)
    future = rec.last_event + SESSION_TTL_S + 1

    import notify.broker.session as mod
    original = mod.time.monotonic

    call_count = [0]
    def fake_monotonic():
        call_count[0] += 1
        if call_count[0] <= 1:
            return future
        return future  # touch() will read future as the new baseline

    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)
    rec.touch()
    # After touch, last_event = future; is_stale checks future - future = 0 < TTL
    assert not rec.is_stale(ttl=SESSION_TTL_S)
