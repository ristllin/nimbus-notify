"""
Phase 5 — BLE GATT transport (host = central/writer, ESP32 = peripheral/server).

Shared contract with the Nimbus firmware (`src/net/ble_notifier.cpp`) — the
UUIDs below are pinned on BOTH sides; never regenerate them.

GATT layout (one custom primary service):
  FRAME_CHAR   — Write Without Response — host→ESP: one COMPLETE nsn wire
                 packet per write, byte-identical to the serial stream
                 ([SOF 0xAA][LEN][payload≤68][CRC8-MAXIM], ≤71 bytes).
                 No transport-level chunking or re-framing.
  STATUS_CHAR  — Notify — ESP→host, tag-prefixed binary:
                 [0x01, protoVer, fwMaj, fwMin]  conn ack ("link ready")
                 [0x02, seq]                     seq echo after a frame applies
                 [0x03, ...]                     reserved (button/encoder)
  CONFIG_CHAR  — Read — [ver, ledCount, brightness, flags] diagnostic snapshot
                 (Write reserved for v2; not consumed by the broker in v1).

MTU: both sides target ATT_MTU 247; hard requirement is ≥74 (71-byte packet +
3-byte ATT header).  macOS/CoreBluetooth negotiates automatically (typ. 185+);
Linux/BlueZ reports mtu_size == 23 until negotiated — we re-check after
connect and hard-fail the session rather than silently truncate.

Threading: bleak is asyncio; the broker calls send() synchronously from its
own event loop.  A daemon worker thread runs a private asyncio loop hosting
the BleakClient and the scan→connect→serve→backoff state machine.  send()
never blocks: it stores the frame in a latest-wins mailbox (frames are
idempotent full state) and wakes the worker; it returns True iff the device
is currently connected — the same best-effort "delivered" semantics as
SerialTransport.  On every (re)connect the worker re-enables the STATUS CCCD
(forgetting this is the classic "device acks nothing after reconnect" bug)
and re-sends the current frame in full.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

log = logging.getLogger(__name__)

# Pinned identity — shared with Nimbus src/net/ble_notifier.cpp.  Generated
# once (Nordic-style family index in the low 16 bits of the first field);
# lowercase canonical form (bleak normalizes to lowercase).  NEVER regenerate.
SERVICE_UUID     = "e20b0001-9463-42a9-aaf8-8aa1fd518d52"
FRAME_CHAR_UUID  = "e20b0002-9463-42a9-aaf8-8aa1fd518d52"
STATUS_CHAR_UUID = "e20b0003-9463-42a9-aaf8-8aa1fd518d52"
CONFIG_CHAR_UUID = "e20b0004-9463-42a9-aaf8-8aa1fd518d52"
DEVICE_NAME      = "Nimbus"

# One whole nsn packet per ATT Write Without Response — no chunking.
MAX_PACKET     = 71                # nsn::kMaxPacket: SOF+LEN+payload(68)+CRC
ATT_HEADER     = 3                 # opcode + handle
MIN_MTU        = MAX_PACKET + ATT_HEADER   # 74 — below this we hard-fail

# STATUS notification tags (binary, not panel strings).
STATUS_CONN_ACK = 0x01
STATUS_SEQ_ECHO = 0x02

# Timings — module-level so tests can shrink them.
SCAN_TIMEOUT_S    = 5.0
CONNECT_TIMEOUT_S = 10.0
ACK_TIMEOUT_S     = 2.0
BACKOFF_INITIAL_S = 0.5
BACKOFF_CAP_S     = 10.0
CLOSE_TIMEOUT_S   = 5.0
PAIRING_RETRY_S   = 2.0   # re-drive the current frame while waiting for pairing


def _is_encryption_error(exc: Exception) -> bool:
    """True if a GATT write failed because the link isn't paired/encrypted.

    Nimbus gates its FRAME characteristic behind encryption + MITM auth, so an
    unbonded write is rejected with an insufficient-encryption/authentication
    ATT error. bleak surfaces this differently per backend (CoreBluetooth on
    macOS, BlueZ on Linux), so match on the message text rather than a type.
    Deliberately NARROW: we do NOT match a bare 'insufficient' (that also covers
    ATT 'insufficient resources', a transient error on an already-bonded link)
    or a bare 'not permitted' (WRITE_NOT_PERMITTED is a config bug, not pairing)
    — swallowing those would hide real frame-drop failures on the proven path."""
    s = str(exc).lower()
    return ("encrypt" in s or "authent" in s or "not paired" in s
            or "insufficient enc" in s or "insufficient authen" in s)


class BleTransport:
    """BLE GATT central transport (same public shape as SerialTransport)."""

    def __init__(self, device_address: str | None = None,
                 device_name: str | None = None) -> None:
        # macOS: CoreBluetooth UUID, NOT a MAC.  None → scan by service UUID.
        self._address = device_address
        # Exact advertised-name filter. When set, ONLY a peripheral whose name
        # matches (and which exposes the service) is connected — so several
        # boards running this firmware on one desk (e.g. a bench board named
        # "Nimbus-BT" alongside a production "Nimbus") stay unambiguous. macOS
        # hides the MAC, so name is the only stable discriminator there.
        self._name = device_name
        self._current: bytes | None = None   # latest-wins mailbox
        self._lock        = threading.Lock()
        self._connected   = threading.Event()
        self._stop        = threading.Event()
        self._established = False            # connected at least once this cycle
        self._pairing_warned = False        # rate-limit the "pair first" hint
        self._retry_handle = None           # pending call_later that re-drives a frame while pairing
        self._mtu         = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None        # lives on the worker loop
        self._stopping: asyncio.Event | None = None    # lives on the worker loop
        self._ack: asyncio.Event | None = None         # per-session conn ack
        self._thread = threading.Thread(target=self._worker,
                                        name="nimbus-notify-ble", daemon=True)
        self._thread.start()

    def send(self, frame: bytes) -> bool:
        """Queue a frame (latest wins).  Returns True iff currently connected.

        Never blocks.  On False the broker just logs debug — the frame is
        retained and re-sent in full on the next (re)connect."""
        with self._lock:
            self._current = bytes(frame)
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass   # loop already closed
        return self._connected.is_set()

    def close(self) -> None:
        """Stop the worker, disconnect, join (bounded)."""
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._signal_stop)
            except RuntimeError:
                pass
        self._thread.join(timeout=CLOSE_TIMEOUT_S)
        if self._thread.is_alive():
            log.warning("BLE worker did not stop within %.1fs", CLOSE_TIMEOUT_S)

    # ------------------------------------------------------------------
    # Worker thread — private asyncio loop
    # ------------------------------------------------------------------

    def _signal_stop(self) -> None:
        """Runs ON the worker loop: wake everything so _run can exit."""
        if self._wake is not None:
            self._wake.set()
        if self._stopping is not None:
            self._stopping.set()

    def _worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._wake     = asyncio.Event()
        self._stopping = asyncio.Event()
        self._loop     = loop
        try:
            loop.run_until_complete(self._run())
        except Exception:
            log.exception("BLE worker died")
        finally:
            self._loop = None
            loop.close()

    async def _run(self) -> None:
        """Scan → connect → serve → backoff, forever (until close())."""
        backoff = BACKOFF_INITIAL_S
        while not self._stop.is_set():
            self._established = False
            try:
                target = self._address or await self._find_device()
                if target is None:
                    log.debug("no Nimbus BLE device found")
                else:
                    await self._session(target)
            except (BleakError, OSError, asyncio.TimeoutError) as exc:
                log.warning("BLE session error: %s — will reconnect", exc)
            if self._stop.is_set():
                break
            if self._established:
                backoff = BACKOFF_INITIAL_S   # fresh drop: retry quickly
            await self._sleep(backoff)
            backoff = min(backoff * 2.0, BACKOFF_CAP_S)

    async def _find_device(self):
        """Scan for the target peripheral. With an explicit name filter, require
        an EXACT name match (still gated on the service UUID); otherwise match by
        service UUID or the default local name."""
        want = self._name
        def _match(device, adv) -> bool:
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            has_svc = SERVICE_UUID in uuids
            if want is not None:
                return has_svc and (device.name or "") == want
            return has_svc or (device.name or "") == DEVICE_NAME
        return await BleakScanner.find_device_by_filter(
            _match, timeout=SCAN_TIMEOUT_S)

    async def _session(self, target) -> None:
        """One connect→serve cycle; returns on disconnect or stop."""
        loop = asyncio.get_running_loop()
        disconnected = asyncio.Event()

        def _on_disconnect(_client) -> None:
            loop.call_soon_threadsafe(disconnected.set)

        client = BleakClient(target, disconnected_callback=_on_disconnect,
                             timeout=CONNECT_TIMEOUT_S)
        await client.connect()
        try:
            mtu = await self._negotiated_mtu(client)
            if mtu < MIN_MTU:
                # A full nsn packet (71 B) + ATT header (3 B) doesn't fit.
                # Hard-fail the session — never silently truncate.
                log.error("ATT MTU %d < %d — cannot carry a full nsn packet; "
                          "dropping session", mtu, MIN_MTU)
                return
            self._mtu = mtu
            ack = asyncio.Event()
            self._ack = ack
            # Re-enable the STATUS CCCD on EVERY (re)connect.
            await client.start_notify(STATUS_CHAR_UUID, self._on_status)
            try:
                await asyncio.wait_for(ack.wait(), ACK_TIMEOUT_S)
                log.info("BLE link ready (conn ack), mtu=%d", mtu)
            except asyncio.TimeoutError:
                log.warning("no conn ack within %.1fs — proceeding", ACK_TIMEOUT_S)
            self._connected.set()
            self._established = True
            # Full-state re-send after every (re)connect (idempotent).
            if self._wake is not None:
                self._wake.clear()
            await self._write_current(client)
            await self._serve(client, disconnected)
        finally:
            self._connected.clear()
            # Fresh connection cycle re-surfaces the pairing hint once, and drops
            # any pending pairing-retry timer from the old session.
            self._pairing_warned = False
            if self._retry_handle is not None:
                self._retry_handle.cancel()
                self._retry_handle = None
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _serve(self, client, disconnected: asyncio.Event) -> None:
        """Steady state: wait for wake (new frame) or disconnect."""
        assert self._wake is not None
        while not self._stop.is_set():
            wake_t = asyncio.ensure_future(self._wake.wait())
            disc_t = asyncio.ensure_future(disconnected.wait())
            _, pending = await asyncio.wait({wake_t, disc_t},
                                            return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if disconnected.is_set():
                log.info("BLE disconnected")
                return
            self._wake.clear()
            if self._stop.is_set():
                return
            await self._write_current(client)

    async def _write_current(self, client) -> None:
        """Write the mailbox frame: one whole nsn packet per WWR, no chunking."""
        with self._lock:
            frame = self._current
        if frame is None:
            return
        limit = self._mtu - ATT_HEADER
        if len(frame) > limit:
            # Must not silently truncate — drop whole frame; the next
            # full-state frame repairs.
            log.error("frame %d B exceeds ATT payload %d B — dropped",
                      len(frame), limit)
            return
        try:
            # response=True (NOT write-without-response): the FRAME characteristic
            # now requires an encrypted + MITM-authenticated link, so an unbonded
            # write returns an ATT insufficient-encryption error — which is exactly
            # what makes macOS raise its native pairing sheet. A no-response write
            # would be dropped silently with no error and never trigger pairing.
            # Once the Mac is bonded, macOS encrypts transparently and this just
            # succeeds — no code change needed here.
            await client.write_gatt_char(FRAME_CHAR_UUID, frame, response=True)
            log.debug("frame sent (%d B)", len(frame))
            self._pairing_warned = False
            if self._retry_handle is not None:      # link is up; drop the retry timer
                self._retry_handle.cancel()
                self._retry_handle = None
        except Exception as exc:
            if _is_encryption_error(exc):
                if not self._pairing_warned:
                    self._pairing_warned = True
                    log.warning(
                        "Nimbus isn't bonded yet — the OS bonds automatically "
                        "(Just Works, no code to type), this should clear in a "
                        "second. If it persists, you're likely running the broker "
                        "detached (nohup/&): run it in the FOREGROUND once so macOS "
                        "can complete the first bond, then it works anywhere.")
                # macOS upgrades the SAME connection to encrypted in place (no
                # reconnect), and frames are event-driven — so without this timer
                # nothing would re-drive the pending frame once the user pairs, and
                # the ring would stay stale. Re-arm a single wake to re-attempt the
                # write until it succeeds (or the session drops, which cancels it).
                loop = asyncio.get_running_loop()
                if self._retry_handle is not None:
                    self._retry_handle.cancel()
                self._retry_handle = loop.call_later(PAIRING_RETRY_S, self._wake.set)
                return
            raise

    async def _negotiated_mtu(self, client) -> int:
        """Post-connect ATT MTU, best effort.

        macOS/CoreBluetooth negotiates automatically (always ≥ MIN_MTU in
        practice).  Linux/BlueZ reports 23 until negotiated — poke the
        backend's explicit acquire if this bleak version exposes one."""
        mtu = int(getattr(client, "mtu_size", 0) or 0)
        if mtu >= MIN_MTU:
            return mtu
        acquire = getattr(getattr(client, "_backend", None), "_acquire_mtu", None)
        if acquire is not None:
            try:
                await acquire()
                mtu = int(getattr(client, "mtu_size", 0) or 0)
            except Exception as exc:   # backend-specific, never fatal here
                log.debug("MTU acquire failed: %s", exc)
        return mtu

    def _on_status(self, _char, data: bytearray) -> None:
        """STATUS notify callback (worker loop).  v1 logs; only ack is acted on."""
        if not data:
            return
        tag = data[0]
        if tag == STATUS_CONN_ACK:
            log.debug("conn ack: %s", bytes(data).hex())
            if self._ack is not None:
                self._ack.set()
        elif tag == STATUS_SEQ_ECHO:
            log.debug("seq echo: %d", data[1] if len(data) > 1 else -1)
        else:
            log.debug("status notify 0x%02x: %s", tag, bytes(data).hex())

    async def _sleep(self, seconds: float) -> None:
        """Backoff sleep, interruptible only by close()."""
        assert self._stopping is not None
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
