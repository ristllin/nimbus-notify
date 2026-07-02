"""BleTransport state-machine tests — mocked bleak, NO real Bluetooth.

The harness replaces BleakClient/BleakScanner inside notify.transport.ble_tx
with in-memory fakes and shrinks the module's timing constants so the worker
thread's scan→connect→serve→backoff cycle runs in milliseconds."""
from __future__ import annotations

import time

import pytest

import notify.transport.ble_tx as ble
from notify.broker.frame import FrameSegment, encode_frame
from notify.state import State

FRAME = encode_frame([], brightness=30, seq=1)   # a real (empty) nsn packet


def wait_until(pred, timeout=2.0, msg="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.005)
    pytest.fail(f"timeout waiting for {msg}")


class FakeDevice:
    name    = "Nimbus"
    address = "11:22:33:44:55:66"


class Harness:
    """Installs fake bleak objects; records every client the worker creates."""

    def __init__(self, monkeypatch, *, mtu=185, send_ack=True, device="scan"):
        self.clients  = []
        self.mtu      = mtu
        self.send_ack = send_ack
        self.device   = FakeDevice() if device == "scan" else device
        harness = self

        class FakeClient:
            def __init__(self, target, disconnected_callback=None, timeout=None):
                self.target      = target
                self.on_disc     = disconnected_callback
                self.mtu_size    = harness.mtu
                self.writes      = []   # (uuid, bytes, response)
                self.notify_subs = []
                self.is_connected = False
                harness.clients.append(self)

            async def connect(self):
                self.is_connected = True

            async def disconnect(self):
                self.is_connected = False

            async def start_notify(self, uuid, cb):
                self.notify_subs.append(uuid)
                if harness.send_ack:
                    cb(None, bytearray([0x01, 1, 0, 1]))

            async def write_gatt_char(self, uuid, data, response=True):
                self.writes.append((uuid, bytes(data), response))

            def drop(self):
                """Simulate a link drop (thread-safe, like a real backend)."""
                self.is_connected = False
                if self.on_disc:
                    self.on_disc(self)

        class FakeScanner:
            @staticmethod
            async def find_device_by_filter(filterfunc, timeout=0.0):
                dev = harness.device
                if dev is None:
                    return None
                return dev if filterfunc(dev, _Adv()) else None

        class _Adv:
            service_uuids = [ble.SERVICE_UUID]

        monkeypatch.setattr(ble, "BleakClient", FakeClient)
        monkeypatch.setattr(ble, "BleakScanner", FakeScanner)
        monkeypatch.setattr(ble, "SCAN_TIMEOUT_S", 0.01)
        monkeypatch.setattr(ble, "ACK_TIMEOUT_S", 0.1)
        monkeypatch.setattr(ble, "BACKOFF_INITIAL_S", 0.01)
        monkeypatch.setattr(ble, "BACKOFF_CAP_S", 0.05)


@pytest.fixture
def transport_factory(monkeypatch):
    created = []

    def make(harness, address=None):
        t = ble.BleTransport(device_address=address)
        created.append(t)
        return t

    yield make
    for t in created:
        t.close()


def test_pinned_uuids_match_spec():
    assert ble.SERVICE_UUID     == "e20b0001-9463-42a9-aaf8-8aa1fd518d52"
    assert ble.FRAME_CHAR_UUID  == "e20b0002-9463-42a9-aaf8-8aa1fd518d52"
    assert ble.STATUS_CHAR_UUID == "e20b0003-9463-42a9-aaf8-8aa1fd518d52"
    assert ble.CONFIG_CHAR_UUID == "e20b0004-9463-42a9-aaf8-8aa1fd518d52"
    assert ble.DEVICE_NAME == "Nimbus"
    uuids = [ble.SERVICE_UUID, ble.FRAME_CHAR_UUID,
             ble.STATUS_CHAR_UUID, ble.CONFIG_CHAR_UUID]
    assert len(set(uuids)) == 4
    assert all(u == u.lower() for u in uuids)
    assert ble.MIN_MTU == 74


def test_connect_subscribes_and_sends(monkeypatch, transport_factory):
    h = Harness(monkeypatch)
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect")
    c = h.clients[0]
    assert c.notify_subs == [ble.STATUS_CHAR_UUID]   # CCCD enabled
    assert t.send(FRAME) is True
    wait_until(lambda: c.writes, msg="frame write")
    uuid, data, response = c.writes[0]
    assert uuid == ble.FRAME_CHAR_UUID
    assert data == FRAME                              # whole packet, one write
    assert response is False                          # Write Without Response


def test_send_while_disconnected_retains_and_resends(monkeypatch,
                                                     transport_factory):
    h = Harness(monkeypatch, device=None)             # nothing advertising yet
    t = transport_factory(h)
    assert t.send(FRAME) is False                     # not connected
    time.sleep(0.05)                                  # a few scan cycles
    assert h.clients == []
    h.device = FakeDevice()                           # device appears
    wait_until(lambda: h.clients and h.clients[0].writes, msg="resend")
    assert h.clients[0].writes[0][1] == FRAME         # full frame, unprompted


def test_latest_wins_mailbox(monkeypatch, transport_factory):
    h = Harness(monkeypatch, device=None)
    t = transport_factory(h)
    f_old = encode_frame([], brightness=10, seq=1)
    f_new = encode_frame([], brightness=20, seq=2)
    t.send(f_old)
    t.send(f_new)                                     # overwrites the mailbox
    h.device = FakeDevice()
    wait_until(lambda: h.clients and h.clients[0].writes, msg="resend")
    writes = h.clients[0].writes
    assert all(w[1] == f_new for w in writes)         # f_old never hits the air


def test_reconnect_resubscribes_and_resends(monkeypatch, transport_factory):
    h = Harness(monkeypatch)
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="first connect")
    t.send(FRAME)
    wait_until(lambda: h.clients[0].writes, msg="first write")
    h.clients[0].drop()                               # link drops
    wait_until(lambda: len(h.clients) >= 2 and h.clients[1].writes,
               msg="reconnect + resend")
    c2 = h.clients[1]
    assert ble.STATUS_CHAR_UUID in c2.notify_subs     # CCCD re-enabled
    assert c2.writes[0][1] == FRAME                   # full-frame re-send
    wait_until(lambda: t._connected.is_set(), msg="reconnected")
    assert t.send(FRAME) is True


def test_mtu_too_small_hard_fails_session(monkeypatch, transport_factory):
    h = Harness(monkeypatch, mtu=23)                  # BlueZ pre-negotiation
    t = transport_factory(h)
    wait_until(lambda: len(h.clients) >= 2, msg="retry after MTU fail")
    assert t.send(FRAME) is False                     # never came up
    assert all(c.writes == [] for c in h.clients)     # nothing truncated/sent
    assert all(not c.is_connected for c in h.clients)  # sessions torn down


def test_oversize_frame_dropped_not_truncated(monkeypatch, transport_factory):
    h = Harness(monkeypatch, mtu=ble.MIN_MTU)         # exactly 74 → 71 B payload
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect")
    c = h.clients[0]
    t.send(bytes(ble.MAX_PACKET + 1))                 # 72 B — over the wire cap
    time.sleep(0.1)
    assert c.writes == []                             # dropped, NOT truncated
    full = encode_frame([FrameSegment.from_state(State.Running)] * 16, 30, 1)
    assert len(full) == ble.MAX_PACKET                # biggest legal packet
    t.send(full)
    wait_until(lambda: c.writes, msg="max-size write")
    assert c.writes[-1][1] == full


def test_no_ack_proceeds_after_timeout(monkeypatch, transport_factory):
    h = Harness(monkeypatch, send_ack=False)          # device never acks
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect without ack")
    assert t.send(FRAME) is True


def test_explicit_address_bypasses_scan(monkeypatch, transport_factory):
    h = Harness(monkeypatch, device=None)             # scanner finds nothing
    t = transport_factory(h, address="cb-uuid-1234")  # macOS CoreBluetooth UUID
    wait_until(lambda: h.clients, msg="direct connect")
    assert h.clients[0].target == "cb-uuid-1234"
    wait_until(lambda: t._connected.is_set(), msg="connect")


def test_close_joins_worker(monkeypatch):
    h = Harness(monkeypatch)
    t = ble.BleTransport()
    wait_until(lambda: t._connected.is_set(), msg="connect")
    t.close()
    assert not t._thread.is_alive()
    assert t.send(FRAME) is False                     # after close: best-effort no
