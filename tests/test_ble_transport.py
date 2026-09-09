"""BleTransport state-machine tests — mocked bleak, NO real Bluetooth.

The harness replaces BleakClient/BleakScanner inside notify.transport.ble_tx
with in-memory fakes and shrinks the module's timing constants so the worker
thread's scan→connect→serve→backoff cycle runs in milliseconds."""
from __future__ import annotations

import asyncio
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

    def __init__(self, monkeypatch, *, mtu=185, send_ack=True, device="scan",
                 flap=False, probe_hang=False, adv_local_name=None):
        self.clients    = []
        self.mtu        = mtu
        self.send_ack   = send_ack
        self.flap       = flap        # each session drops the instant it comes up
        self.probe_hang = probe_hang  # CONFIG read never answers (half-open link)
        self.device     = FakeDevice() if device == "scan" else device
        self.adv_local_name = adv_local_name  # advertised name (may differ from GAP)
        harness = self

        class FakeClient:
            def __init__(self, target, disconnected_callback=None, timeout=None):
                self.target      = target
                self.on_disc     = disconnected_callback
                self.mtu_size    = harness.mtu
                self.writes      = []   # (uuid, bytes, response)
                self.reads       = []   # uuids read (liveness probe)
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
                if harness.flap:
                    # Drop the instant we go 'established' — synthesize the macOS
                    # CoreBluetooth wedge (connects, then dies within seconds).
                    asyncio.get_running_loop().call_soon(self.drop)

            async def write_gatt_char(self, uuid, data, response=True):
                self.writes.append((uuid, bytes(data), response))

            async def read_gatt_char(self, uuid):
                self.reads.append(uuid)
                if harness.probe_hang:
                    # Peer silently gone: never answers, so the probe's wait_for
                    # times out — the macOS half-open link the fix must detect.
                    await asyncio.sleep(5.0)
                return bytearray([1, 45, 30, 1])   # CONFIG snapshot

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
            local_name    = harness.adv_local_name

        monkeypatch.setattr(ble, "BleakClient", FakeClient)
        monkeypatch.setattr(ble, "BleakScanner", FakeScanner)
        monkeypatch.setattr(ble, "SCAN_TIMEOUT_S", 0.01)
        monkeypatch.setattr(ble, "ACK_TIMEOUT_S", 0.1)
        monkeypatch.setattr(ble, "BACKOFF_INITIAL_S", 0.01)
        monkeypatch.setattr(ble, "BACKOFF_CAP_S", 0.05)


@pytest.fixture
def transport_factory(monkeypatch):
    created = []

    def make(harness, address=None, **kw):
        t = ble.BleTransport(device_address=address, **kw)
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
    assert response is True                           # with-response: triggers
    #                                                   pairing on the encrypted
    #                                                   FRAME char (insufficient-
    #                                                   encryption ATT error)


def test_name_filter_matches_advertised_local_name(monkeypatch, transport_factory):
    # macOS reports a stale cached GAP name (device.name) that can differ from the
    # LIVE advertised local_name — a board customized to "Lumi" still caches its
    # base "Nimbus" GAP name. The name filter must match the advertised local_name,
    # else the broker never selects the (renamed) device and stays disconnected.
    dev = FakeDevice()
    dev.name = "Nimbus"                         # cached GAP name (lags the adv)
    h = Harness(monkeypatch, device=dev, adv_local_name="Lumi")
    t = transport_factory(h, device_name="Lumi")
    wait_until(lambda: t._connected.is_set(), msg="connect by advertised name")


def test_is_encryption_error_classifies_pairing_failures():
    assert ble._is_encryption_error(Exception("Insufficient Encryption"))
    assert ble._is_encryption_error(Exception("connection is not authenticated"))
    assert ble._is_encryption_error(Exception("peripheral is not paired"))
    assert ble._is_encryption_error(Exception("ATT error: insufficient authentication"))
    # Narrow on purpose: these must NOT be treated as pairing errors (swallowing
    # them would hide real frame drops on an already-bonded link).
    assert not ble._is_encryption_error(Exception("Writing is not permitted"))  # config bug
    assert not ble._is_encryption_error(Exception("insufficient resources"))    # transient
    assert not ble._is_encryption_error(Exception("peer disconnected"))
    assert not ble._is_encryption_error(Exception("operation timed out"))


def test_unpaired_write_is_swallowed_not_fatal(monkeypatch, transport_factory):
    # An unbonded device rejects the encrypted FRAME write; the worker must warn
    # (once) and keep running so the user has time to pair — never crash/tear down.
    h = Harness(monkeypatch)
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect")
    c = h.clients[0]

    async def _reject(uuid, data, response=True):
        raise Exception("Insufficient Encryption")
    c.write_gatt_char = _reject

    assert t.send(FRAME) is True
    time.sleep(0.1)
    assert t._connected.is_set()          # survived the rejected write
    assert t._pairing_warned is True      # user was told to pair (once)
    assert t._retry_handle is not None    # a retry was armed to re-drive the frame


def test_pairing_retry_redrives_frame_after_encrypt(monkeypatch, transport_factory):
    # macOS upgrades the link to encrypted in place (no reconnect), so the swallowed
    # first frame must be re-driven by the retry timer once the peer pairs.
    monkeypatch.setattr(ble, "PAIRING_RETRY_S", 0.02)
    h = Harness(monkeypatch)
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect")
    c = h.clients[0]

    real_write = c.write_gatt_char
    calls = {"n": 0}

    async def _flaky(uuid, data, response=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Insufficient Encryption")   # unbonded: first attempt
        await real_write(uuid, data, response)           # paired: retry succeeds
    c.write_gatt_char = _flaky

    assert t.send(FRAME) is True
    wait_until(lambda: any(w[0] == ble.FRAME_CHAR_UUID for w in c.writes),
               msg="frame re-driven after pairing")
    assert calls["n"] >= 2                 # first failed, retry landed it


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


def test_flap_recycles_process_when_supervised(monkeypatch, transport_factory):
    # A process-level CoreBluetooth wedge makes every session come up then drop
    # within seconds. After FLAP_MAX_CYCLES such flaps in the window, self-heal
    # must fire the restart hook (in production: os._exit → supervisor respawn).
    monkeypatch.setattr(ble, "FLAP_MAX_CYCLES", 3)
    h = Harness(monkeypatch, flap=True)
    restarts = {"n": 0}
    t = transport_factory(h, self_heal=True,
                          restart_hook=lambda: restarts.__setitem__("n", restarts["n"] + 1))
    wait_until(lambda: restarts["n"] == 1, msg="self-heal restart fired")
    assert len(h.clients) >= 3          # it really did flap several sessions
    wait_until(lambda: not t._thread.is_alive(), msg="worker loop stopped after restart")


def test_flap_warns_but_keeps_running_when_unsupervised(monkeypatch, transport_factory):
    # Not supervised (e.g. a foreground bootstrap run): exiting would just kill
    # the only transport, so self-heal must NOT fire — warn, reset, keep trying.
    monkeypatch.setattr(ble, "FLAP_MAX_CYCLES", 3)
    h = Harness(monkeypatch, flap=True)
    restarts = {"n": 0}
    t = transport_factory(h, self_heal=False,
                          restart_hook=lambda: restarts.__setitem__("n", restarts["n"] + 1))
    wait_until(lambda: len(h.clients) >= 6, msg="kept reconnecting past the threshold")
    assert restarts["n"] == 0           # never recycled
    assert t._thread.is_alive()         # still trying


def test_occasional_drop_does_not_trip_watchdog(monkeypatch, transport_factory):
    # A single clean drop + reconnect is normal operation, not a flap — the
    # watchdog must not recycle the process for it.
    monkeypatch.setattr(ble, "FLAP_MAX_CYCLES", 3)
    h = Harness(monkeypatch)
    restarts = {"n": 0}
    t = transport_factory(h, self_heal=True,
                          restart_hook=lambda: restarts.__setitem__("n", restarts["n"] + 1))
    wait_until(lambda: t._connected.is_set(), msg="first connect")
    t.send(FRAME)
    wait_until(lambda: h.clients[0].writes, msg="first write")
    h.clients[0].drop()                              # one drop
    wait_until(lambda: len(h.clients) >= 2 and h.clients[1].writes, msg="reconnect")
    wait_until(lambda: t._connected.is_set(), msg="reconnected and stable")
    time.sleep(0.1)
    assert restarts["n"] == 0                        # one drop never trips it
    assert len(t._flaps) <= 1


def test_under_supervisor_detects_launchd_and_systemd(monkeypatch):
    for var in ("XPC_SERVICE_NAME", "INVOCATION_ID", "NOTIFY_SOCKET"):
        monkeypatch.delenv(var, raising=False)
    assert ble._under_supervisor() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")      # plain interactive shell
    assert ble._under_supervisor() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.nimbus-notify.broker")  # launchd
    assert ble._under_supervisor() is True
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setenv("INVOCATION_ID", "abc123")    # systemd unit
    assert ble._under_supervisor() is True


def test_close_joins_worker(monkeypatch):
    h = Harness(monkeypatch)
    t = ble.BleTransport()
    wait_until(lambda: t._connected.is_set(), msg="connect")
    t.close()
    assert not t._thread.is_alive()
    assert t.send(FRAME) is False                     # after close: best-effort no


def test_liveness_probe_reads_config_and_keeps_session(monkeypatch, transport_factory):
    # With no conn ack, _last_rx stays 0 so the idle heartbeat actively probes; a
    # successful CONFIG read proves the peer is alive and the session continues —
    # no spurious reconnect.
    monkeypatch.setattr(ble, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(ble, "HEARTBEAT_TIMEOUT_S", 0.2)
    h = Harness(monkeypatch, send_ack=False)
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect")
    c = h.clients[0]
    wait_until(lambda: c.reads, msg="liveness probe read")
    assert c.reads[0] == ble.CONFIG_CHAR_UUID         # probed the right char
    time.sleep(0.1)
    assert t._connected.is_set()                      # still up
    assert len(h.clients) == 1                        # never reconnected
    assert t._probe_dead is False


def test_half_open_link_detected_and_reconnects(monkeypatch, transport_factory):
    # macOS holds the link "connected" but the peer is gone — the disconnect
    # callback never fires. The heartbeat's CONFIG read times out, so the worker
    # tears the session down and reconnects instead of waiting forever (the
    # multi-day silent wedge).
    monkeypatch.setattr(ble, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(ble, "HEARTBEAT_TIMEOUT_S", 0.05)
    h = Harness(monkeypatch, send_ack=False, probe_hang=True)
    t = transport_factory(h)
    wait_until(lambda: len(h.clients) >= 2, msg="half-open detected → reconnect")
    assert h.clients[0].reads                         # it did probe the dead link
    assert not h.clients[0].is_connected              # and tore the session down


def test_liveness_probe_encryption_error_is_alive(monkeypatch, transport_factory):
    # An unbonded-but-live link rejects the READ_ENC CONFIG read; that still proves
    # the peer is there, so the session must NOT be torn down.
    monkeypatch.setattr(ble, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(ble, "HEARTBEAT_TIMEOUT_S", 0.2)
    h = Harness(monkeypatch, send_ack=False)
    t = transport_factory(h)
    wait_until(lambda: t._connected.is_set(), msg="connect")

    async def _reject(uuid):
        raise Exception("Insufficient Encryption")
    h.clients[0].read_gatt_char = _reject

    time.sleep(0.1)
    assert t._connected.is_set()                      # survived the rejected read
    assert len(h.clients) == 1                        # not treated as dead
    assert t._probe_dead is False


def test_half_open_wedge_recycles_process_when_supervised(monkeypatch, transport_factory):
    # A permanently half-open stack (every reconnect immediately dead) is a wedge:
    # repeated probe-killed sessions must feed the SAME self-heal watchdog and
    # recycle the process — even with FLAP_SESSION_MIN_S=0 so no session counts as
    # a duration-flap and ONLY the probe-dead path can trip it.
    monkeypatch.setattr(ble, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(ble, "HEARTBEAT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ble, "FLAP_SESSION_MIN_S", 0.0)
    monkeypatch.setattr(ble, "FLAP_MAX_CYCLES", 3)
    h = Harness(monkeypatch, send_ack=False, probe_hang=True)
    restarts = {"n": 0}
    t = transport_factory(h, self_heal=True,
                          restart_hook=lambda: restarts.__setitem__("n", restarts["n"] + 1))
    wait_until(lambda: restarts["n"] == 1, msg="probe-dead wedge recycles process")
    assert len(h.clients) >= 3          # it really did retry several dead sessions
