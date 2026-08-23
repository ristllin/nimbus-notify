"""CUM-14: broker state-model hardening for unattended wake-up loops.

Pins three behaviors:
  1. Permission stays visually distinct from a human-input wait, even for
     ``notify:*`` subtypes we don't explicitly map (permission -> AwaitingApproval,
     everything else -> WaitingInput).
  2. A per-session ``heartbeat`` refreshes liveness only: it never changes state,
     never creates a session, and never relights a retired/CTA one.
  3. A Stop-less wake-up window resolves to Done and ages out on the BENIGN TTL,
     and a wake-up (timer) wait is never mislabeled as a WaitingInput CTA.
"""
import os

from notify.broker.server import Broker, _resolve_wakeup
from notify.broker.session import CTA_STATES, verb_to_state
from notify.state import State


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return True

    def close(self):
        pass


def _mk():
    t = FakeTransport()
    return Broker(t), t


def _ev(sid, verb, pid=0, ntype=None):
    e = {"harness": "claude", "session_id": sid, "cwd": "/tmp/x", "verb": verb}
    if pid:
        e["pid"] = pid
    if ntype:
        e["verb"] = "notify"
        e["notification_type"] = ntype
    return e


def _state(b, sid):
    return b._allocator._sessions[sid].state


# --------------------------------------------------------------------------
# 1. Permission distinct from waiting
# --------------------------------------------------------------------------

def test_mapped_permission_prompt_is_approval():
    assert verb_to_state("notify:permission_prompt") == State.AwaitingApproval


def test_unmapped_permission_subtype_is_approval_not_input():
    # A future/renamed permission subtype we don't explicitly map must still be
    # amber "approve", not purple "waiting on you".
    assert verb_to_state("notify:tool_permission_request") == State.AwaitingApproval
    assert verb_to_state("notify:approval_needed") == State.AwaitingApproval
    assert verb_to_state("notify:consent_required") == State.AwaitingApproval


def test_unmapped_non_permission_subtype_is_input():
    assert verb_to_state("notify:some_question") == State.WaitingInput
    assert verb_to_state("notify:idle_prompt") == State.WaitingInput


def test_permission_and_input_are_different_states():
    assert State.AwaitingApproval != State.WaitingInput  # distinct bytes on the wire


def test_permission_notification_via_broker_renders_approval():
    b, _ = _mk()
    b.handle_event(_ev("s1", "notify", ntype="tool_permission_request"))
    assert _state(b, "s1") == State.AwaitingApproval


# --------------------------------------------------------------------------
# 2. Per-session heartbeat
# --------------------------------------------------------------------------

def test_heartbeat_refreshes_liveness_without_changing_state():
    b, _ = _mk()
    b.handle_event(_ev("s1", "notify", ntype="permission_prompt"))
    assert _state(b, "s1") == State.AwaitingApproval
    before = b._allocator._sessions["s1"].last_event
    # Heartbeat must not knock it off the amber state.
    b.handle_event(_ev("s1", "heartbeat"))
    assert _state(b, "s1") == State.AwaitingApproval
    assert b._allocator._sessions["s1"].last_event >= before


def test_heartbeat_never_creates_a_session():
    b, _ = _mk()
    b.handle_event(_ev("ghost", "heartbeat", pid=os.getpid()))
    assert b._allocator.active_segments() == []  # no phantom segment


def test_heartbeat_pushes_no_frame():
    b, t = _mk()
    b.handle_event(_ev("s1", "running", pid=os.getpid()))
    n = len(t.sent)
    b.handle_event(_ev("s1", "heartbeat"))
    assert len(t.sent) == n  # nothing visual changed


def test_heartbeat_keeps_session_off_the_reaper(monkeypatch):
    import notify.broker.session as session_mod
    b, _ = _mk()
    b.handle_event(_ev("s1", "running"))
    rec = b._allocator._sessions["s1"]
    # Jump past the benign TTL, but a heartbeat lands first.
    base = rec.last_event
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: base + 119.0)
    b.handle_event(_ev("s1", "heartbeat"))
    # Now sweep at 119s + a hair: without the heartbeat touch it would be stale.
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: base + 119.0 + 10.0)
    b._sweep_once()
    assert "s1" in b._allocator._sessions  # heartbeat kept it alive


# --------------------------------------------------------------------------
# 3. Stop-less wake-up window resolves to Done / expired
# --------------------------------------------------------------------------

def test_wakeup_verb_maps_to_done():
    assert verb_to_state("wakeup") == State.Done
    assert State.Done not in CTA_STATES  # benign -> ages out on the short TTL


def test_stopless_wakeup_resolves_to_done():
    # running ... then the session arms a wake-up and NO Stop ever arrives.
    b, _ = _mk()
    b.handle_event(_ev("s1", "running", pid=os.getpid()))
    assert _state(b, "s1") == State.Running
    b.handle_event(_ev("s1", "wakeup", pid=os.getpid()))
    assert _state(b, "s1") == State.Done                       # window resolved
    assert b._allocator._sessions["s1"].awaiting_wakeup is True


def test_wakeup_window_ages_out_on_benign_ttl(monkeypatch):
    import notify.broker.session as session_mod
    b, _ = _mk()
    b.handle_event(_ev("s1", "wakeup"))
    rec = b._allocator._sessions["s1"]
    # Past the benign TTL (120s) but well within the CTA TTL (300s): a benign Done
    # must be reaped here; a false CTA would survive.
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: rec.last_event + 130.0)
    b._sweep_once()
    assert b._allocator.active_segments() == []  # expired, not pinned


def test_idle_prompt_during_wakeup_wait_is_not_a_cta():
    # The 60s idle notification while awaiting a wake-up must NOT become a purple
    # WaitingInput CTA; it's a timer wait, not a human wait.
    b, _ = _mk()
    b.handle_event(_ev("s1", "wakeup"))
    b.handle_event(_ev("s1", "notify", ntype="idle_prompt"))
    assert _state(b, "s1") == State.Done
    assert _state(b, "s1") not in CTA_STATES


def test_activity_clears_the_wakeup_flag():
    # When the wake-up fires and the session works again, it relights and the flag
    # clears so a later genuine idle prompt is treated normally.
    b, _ = _mk()
    b.handle_event(_ev("s1", "wakeup"))
    assert b._allocator._sessions["s1"].awaiting_wakeup is True
    b.handle_event(_ev("s1", "running"))
    assert _state(b, "s1") == State.Running
    assert b._allocator._sessions["s1"].awaiting_wakeup is False
    # A real idle prompt now is a genuine human wait again.
    b.handle_event(_ev("s1", "notify", ntype="idle_prompt"))
    assert _state(b, "s1") == State.WaitingInput


def test_error_during_wakeup_wait_is_not_downgraded_by_idle():
    # A wake-up armed, then the turn ERRORS, then a 60s idle prompt: the red Error
    # must survive (a later idle must NOT silently downgrade a real needs-you to Done).
    b, _ = _mk()
    b.handle_event(_ev("s1", "wakeup"))
    b.handle_event(_ev("s1", "error"))
    assert _state(b, "s1") == State.Error
    assert b._allocator._sessions["s1"].awaiting_wakeup is False   # CTA cleared the flag
    b.handle_event(_ev("s1", "notify", ntype="idle_prompt"))
    assert _state(b, "s1") == State.WaitingInput   # genuine wait, NOT a hidden Done
    assert _state(b, "s1") in CTA_STATES


def test_question_during_wakeup_wait_is_not_downgraded():
    # wake-up armed, then the agent asks a real question (elicitation): shown as
    # WaitingInput and the flag clears, so a later idle can't hide it.
    b, _ = _mk()
    b.handle_event(_ev("s1", "wakeup"))
    b.handle_event(_ev("s1", "notify", ntype="elicitation_dialog"))
    assert _state(b, "s1") == State.WaitingInput
    assert b._allocator._sessions["s1"].awaiting_wakeup is False


def test_normal_session_idle_prompt_still_waits_on_you():
    # No wake-up armed -> idle prompt is a genuine WaitingInput (no regression).
    b, _ = _mk()
    b.handle_event(_ev("s1", "running"))
    b.handle_event(_ev("s1", "notify", ntype="idle_prompt"))
    assert _state(b, "s1") == State.WaitingInput


# --------------------------------------------------------------------------
# _resolve_wakeup unit table
# --------------------------------------------------------------------------

def test_resolve_wakeup_table():
    assert _resolve_wakeup("wakeup", State.Running, False) == (State.Done, True)
    assert _resolve_wakeup("notify:idle_prompt", State.WaitingInput, True) == (State.Done, True)
    assert _resolve_wakeup("notify:idle_prompt", State.WaitingInput, False) == (State.WaitingInput, False)
    assert _resolve_wakeup("running", State.Running, True) == (State.Running, False)
    assert _resolve_wakeup("done", State.Done, True) == (State.Done, False)
    # A real CTA while armed clears the flag and shows the true state (Finding 1).
    assert _resolve_wakeup("error", State.Error, True) == (State.Error, False)
    assert _resolve_wakeup("notify:permission_prompt", State.AwaitingApproval, True) \
        == (State.AwaitingApproval, False)
