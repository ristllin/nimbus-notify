"""Red-ring regression guards (owner root-cause round 2026-07-13).

Pin the four broker-side fixes: dead-pid eviction, snapshot heartbeat,
going-empty trailing frame, the shortened Error CTA TTL, and the singleton
probe. See the Nimbus plan file 'owner batch 2026-07-12b' P1 for the causal map.
"""
import os
import socket
import tempfile
import threading
from pathlib import Path

from notify.broker.server import Broker, _socket_alive
from notify.broker.session import CTA_TTL_S


class FakeTransport:
    def __init__(self):
        self.sent = []
    def send(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return True
    def close(self):
        pass


def _mk() -> tuple[Broker, FakeTransport]:
    t = FakeTransport()
    return Broker(t), t


def _ev(sid, verb, pid=0):
    e = {"harness": "claude", "session_id": sid, "cwd": "/tmp/x", "verb": verb}
    if pid:
        e["pid"] = pid
    return e


def test_cta_ttl_matches_device_hold():
    # 900s red holds read as 'stuck forever'; must match the device's 5-min hold.
    assert CTA_TTL_S == 300.0


def test_dead_pid_session_evicted_on_sweep():
    # A REAL process that is alive at registration (pid_alive_seen latches) and
    # then dies -> evicted on the next sweep. (A never-seen-alive pid must NOT
    # evict — see test_foreign_namespace_pid_never_evicted.)
    import subprocess, time
    b, t = _mk()
    child = subprocess.Popen(["sleep", "30"])
    try:
        b.handle_event(_ev("s1", "error", pid=child.pid))
        assert b._allocator._sessions["s1"].pid_alive_seen is True
        assert len(b._allocator.active_segments()) == 1
    finally:
        child.kill(); child.wait()                        # now genuinely dead
    frames_before = len(t.sent)
    b._sweep_once()
    assert b._allocator.active_segments() == []           # evicted immediately
    assert len(t.sent) > frames_before                    # and a frame announced it


def test_foreign_namespace_pid_never_evicted():
    # A containerized harness reports a pid that does not exist host-side. It was
    # NEVER seen alive in our namespace -> pid eviction must not touch it (the
    # TTLs still apply). Regression guard for the false-evict-every-sweep bug.
    b, t = _mk()
    b.handle_event(_ev("s1", "running", pid=(1 << 22) + 7))   # never alive here
    assert b._allocator._sessions["s1"].pid_alive_seen is False
    b._sweep_once()
    assert len(b._allocator.active_segments()) == 1           # survives


def test_live_pid_session_survives_sweep():
    b, t = _mk()
    b.handle_event(_ev("s1", "error", pid=os.getpid()))   # our own live pid
    b._sweep_once()
    assert len(b._allocator.active_segments()) == 1       # still held (CTA window)


def test_heartbeat_repushes_snapshot_while_active():
    b, t = _mk()
    b.handle_event(_ev("s1", "running", pid=os.getpid()))
    n = len(t.sent)
    b._sweep_once()
    assert len(t.sent) == n + 1     # heartbeat re-sent the snapshot (self-healing)
    b._sweep_once()
    assert len(t.sent) == n + 2     # every sweep while active


def test_going_empty_sends_one_trailing_frame_then_silence():
    b, t = _mk()
    b.handle_event(_ev("s1", "running", pid=os.getpid()))
    b._sweep_once()                          # active heartbeat
    b.handle_event(_ev("s1", "end"))         # clean end -> push (table empty now)
    n = len(t.sent)
    b._sweep_once()                          # trailing all-clear frame
    assert len(t.sent) == n + 1
    b._sweep_once()                          # table empty + latch cleared -> silence
    assert len(t.sent) == n + 1


def test_socket_alive_probe():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "probe.sock"
        assert _socket_alive(path) is False            # nothing there
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(1)
        try:
            assert _socket_alive(path) is True         # live listener -> refuse start
        finally:
            srv.close()
        assert _socket_alive(path) is False            # dead socket file -> reclaimable
