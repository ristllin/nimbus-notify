"""`nimbus-notify status` — the live "is the device connected, and which one?"
query. Pins the three seams it rides on: the transport status() snapshot, the
broker's status_snapshot(), the socket handler's `{"cmd":"status"}` branch, and
the CLI formatting + scriptable exit codes.
"""
import asyncio
import json
from pathlib import Path

from notify.broker.server import Broker, _handle_client
from notify.cli import install
from notify.transport.serial_tx import SerialTransport


class FakeTransport:
    """A transport that reports a fixed link state."""
    def __init__(self, connected=True, name="Nimbus-2"):
        self._connected = connected
        self._name = name
        self.sent = []
    def send(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return self._connected
    def close(self):
        pass
    def status(self) -> dict:
        return {"kind": "ble", "connected": self._connected,
                "name": self._name, "address": None, "mtu": 247,
                "established": self._connected}


def _ev(sid, verb):
    return {"harness": "claude", "session_id": sid, "cwd": "/tmp/proj", "verb": verb}


# --- transport.status() -----------------------------------------------------

def test_serial_status_shape_before_open():
    # SerialTransport never opens until first send, so status() is safe offline.
    st = SerialTransport(port="/dev/ttyFAKE").status()
    assert st["kind"] == "serial"
    assert st["connected"] is False        # nothing opened
    assert st["port"] == "/dev/ttyFAKE"


# --- broker.status_snapshot() ----------------------------------------------

def test_snapshot_carries_transport_and_sessions():
    b = Broker(FakeTransport(connected=True, name="Nimbus-2"))
    b.handle_event(_ev("s1", "running"))
    snap = b.status_snapshot()
    assert snap["transport"]["connected"] is True
    assert snap["transport"]["name"] == "Nimbus-2"
    assert [s["session_id"] for s in snap["sessions"]] == ["s1"]


def test_snapshot_survives_a_broken_transport_status():
    class Boom(FakeTransport):
        def status(self):
            raise RuntimeError("wedged")
    snap = Broker(Boom()).status_snapshot()
    assert snap["transport"] == {"kind": "unknown", "connected": False}


# --- socket handler `{"cmd": "status"}` branch ------------------------------

class _Reader:
    def __init__(self, line: bytes):
        self._line = line
    async def readline(self):
        return self._line


class _Writer:
    def __init__(self):
        self.buf = b""
    def write(self, data: bytes):
        self.buf += data
    async def drain(self):
        pass
    def close(self):
        pass


def _drive(broker, line: bytes) -> bytes:
    w = _Writer()
    asyncio.run(_handle_client(broker, _Reader(line), w))
    return w.buf


def test_handler_status_query_returns_snapshot_not_ok():
    b = Broker(FakeTransport(connected=True, name="Nimbus-2"))
    out = _drive(b, b'{"cmd": "status"}\n')
    assert out != b"ok\n"
    parsed = json.loads(out.decode())
    assert parsed["transport"]["name"] == "Nimbus-2"


def test_handler_event_path_unchanged_by_status_branch():
    # A normal led-report event (no "cmd") still ingests and gets "ok".
    b = Broker(FakeTransport())
    out = _drive(b, (json.dumps(_ev("s9", "running")) + "\n").encode())
    assert out == b"ok\n"
    assert [s["session_id"] for s in b.status_snapshot()["sessions"]] == ["s9"]


# --- CLI formatting + exit codes -------------------------------------------

def test_end_to_end_over_a_real_unix_socket(monkeypatch):
    """The whole round-trip: real asyncio unix server + handler on one side, the
    real CLI socket client (`_query_broker_status`) on the other. Proves the wire
    contract, not just the two halves in isolation."""
    import shutil
    import tempfile
    from notify.broker import server
    # macOS caps AF_UNIX paths at ~104 chars, so keep this shallow (not tmp_path).
    tmpdir = tempfile.mkdtemp(prefix="nsn", dir="/tmp")
    try:
        sock_path = Path(tmpdir) / "b.sock"
        monkeypatch.setattr(server, "SOCKET_PATH", sock_path)

        b = Broker(FakeTransport(connected=True, name="Nimbus-2"))
        b.handle_event(_ev("s1", "running"))

        async def _serve_one():
            srv = await asyncio.start_unix_server(
                lambda r, w: _handle_client(b, r, w), path=str(sock_path))
            async with srv:
                # Blocking CLI query in a thread so both ends share this loop.
                return await asyncio.to_thread(install._query_broker_status)

        snap = asyncio.run(_serve_one())
        assert snap is not None
        assert snap["transport"]["name"] == "Nimbus-2"
        assert snap["sessions"][0]["session_id"] == "s1"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_cli_status_exit_codes(monkeypatch, capsys):
    # broker down -> 1
    monkeypatch.setattr(install, "_query_broker_status", lambda: None)
    assert install.status() == 1
    assert "broker not running" in capsys.readouterr().out

    # up + connected -> 0
    monkeypatch.setattr(install, "_query_broker_status", lambda: {
        "transport": {"kind": "ble", "connected": True, "name": "Nimbus-2", "mtu": 247},
        "sessions": [{"harness": "claude", "cwd": "/tmp/proj", "state": "Running", "segment": 0}],
    })
    assert install.status() == 0
    out = capsys.readouterr().out
    assert "Nimbus-2" in out and "connected" in out and "sessions: 1" in out

    # up but link down -> 2
    monkeypatch.setattr(install, "_query_broker_status", lambda: {
        "transport": {"kind": "ble", "connected": False, "name": "Nimbus-2"},
        "sessions": [],
    })
    assert install.status() == 2
    assert "NOT connected" in capsys.readouterr().out
