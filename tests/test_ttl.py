"""Tests for the configurable idle-session eviction TTL.

The TTL is the reaper for sessions that vanished WITHOUT a clean ``end`` event
(a hard kill terminates the harness before its SessionEnd hook can run). The
default was lowered 900 s → 120 s so killed sessions clear promptly, and the
broker gained a ``--ttl`` flag so it can be tuned.
"""
import notify.broker.session as session_mod
from notify.broker.segments import SegmentAllocator
from notify.broker.server import MIN_TTL_S, TTL_CHECK_S, Broker
from notify.broker.session import CTA_TTL_S, SESSION_TTL_S, SessionRecord
from notify.state import State


def _rec(sid: str, state: State = State.Running) -> SessionRecord:
    return SessionRecord(session_id=sid, harness="claude", cwd="/x", state=state)


class _NullTransport:
    """Minimal Transport stand-in: the Broker constructor only stores it."""

    def send(self, frame) -> bool:  # noqa: D401
        return True

    def close(self) -> None:
        pass


def test_default_ttl_is_two_minutes():
    # A killed session (no SessionEnd hook) must clear in ~2 min, not 15.
    assert SESSION_TTL_S == 120.0


def test_evict_stale_honours_explicit_ttl(monkeypatch):
    alloc = SegmentAllocator(max_segs=4)
    r = _rec("s0")
    alloc.register(r)
    # 130 s since its last event.
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: r.last_event + 130.0)

    # A 900 s window keeps it; a 120 s window reaps it.
    assert alloc.evict_stale(ttl=900.0) == []
    assert len(alloc) == 1
    assert alloc.evict_stale(ttl=120.0) == ["s0"]
    assert len(alloc) == 0


def test_evict_stale_defaults_to_session_ttl(monkeypatch):
    alloc = SegmentAllocator(max_segs=4)
    r = _rec("s0")
    alloc.register(r)
    monkeypatch.setattr(session_mod.time, "monotonic",
                        lambda: r.last_event + SESSION_TTL_S + 1)
    assert alloc.evict_stale() == ["s0"]


def test_evict_stale_keeps_fresh_session(monkeypatch):
    alloc = SegmentAllocator(max_segs=4)
    r = _rec("s0")
    alloc.register(r)
    # Only 10 s idle — well under any sane TTL.
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: r.last_event + 10.0)
    assert alloc.evict_stale(ttl=120.0) == []
    assert len(alloc) == 1


def test_broker_defaults_to_session_ttl():
    b = Broker(_NullTransport())
    assert b._ttl == SESSION_TTL_S


def test_broker_threads_custom_ttl():
    b = Broker(_NullTransport(), ttl=40.0)
    assert b._ttl == 40.0


# --- call-to-action exemption: a job blocked on the human must not be reaped ---

def test_cta_states_survive_benign_ttl(monkeypatch):
    # A session parked in a "needs you" state fires one event then goes quiet
    # while it waits on the human — it must NOT be reaped at the short benign TTL.
    for cta in (State.AwaitingApproval, State.WaitingInput, State.Error):
        alloc = SegmentAllocator(max_segs=4)
        r = _rec("s0", cta)
        alloc.register(r)
        # Idle well past the benign window but within the CTA window.
        monkeypatch.setattr(session_mod.time, "monotonic", lambda: r.last_event + 300.0)
        assert alloc.evict_stale(ttl=120.0, cta_ttl=900.0) == [], f"{cta} reaped early"
        assert len(alloc) == 1


def test_cta_states_reaped_after_cta_ttl(monkeypatch):
    # Past the CTA window (e.g. it was killed while awaiting approval), it clears.
    alloc = SegmentAllocator(max_segs=4)
    r = _rec("s0", State.AwaitingApproval)
    alloc.register(r)
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: r.last_event + 901.0)
    assert alloc.evict_stale(ttl=120.0, cta_ttl=900.0) == ["s0"]


def test_benign_states_still_reaped_while_cta_survives(monkeypatch):
    # Mixed ring: a benign Idle session and a pending-approval session, both idle
    # 200 s. Only the benign one is reaped; the CTA one stays.
    alloc = SegmentAllocator(max_segs=4)
    idle = _rec("idle", State.Idle)
    cta = _rec("cta", State.AwaitingApproval)
    alloc.register(idle)
    alloc.register(cta)
    monkeypatch.setattr(session_mod.time, "monotonic",
                        lambda: idle.last_event + 200.0)
    evicted = alloc.evict_stale(ttl=120.0, cta_ttl=900.0)
    assert evicted == ["idle"]
    assert len(alloc) == 1


def test_broker_cta_ttl_is_the_long_window():
    b = Broker(_NullTransport())
    assert b._cta_ttl == CTA_TTL_S
    assert b._cta_ttl > b._ttl


def test_broker_cta_ttl_never_shorter_than_benign():
    # A --ttl longer than the CTA default must not make CTAs reap sooner.
    b = Broker(_NullTransport(), ttl=1200.0)
    assert b._ttl == 1200.0
    assert b._cta_ttl == 1200.0  # max(benign, CTA_TTL_S)


def test_broker_sweep_is_adaptive_and_bounded():
    # Short TTL → proportionally frequent sweep, but never below the floor.
    fast = Broker(_NullTransport(), ttl=40.0)
    assert MIN_TTL_S <= fast._ttl_check <= TTL_CHECK_S
    assert fast._ttl_check <= fast._ttl  # never sweep slower than the ttl itself

    # Long TTL → sweep capped at TTL_CHECK_S (no busy-spin).
    slow = Broker(_NullTransport(), ttl=900.0)
    assert slow._ttl_check == TTL_CHECK_S


def test_broker_floors_tiny_ttl():
    # A footgun value (0 / negative) must not evict live sessions instantly.
    b = Broker(_NullTransport(), ttl=0.0)
    assert b._ttl == MIN_TTL_S
    assert b._ttl_check == MIN_TTL_S
