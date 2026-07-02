"""
Phase 2 — Broker daemon.

Listens on a Unix domain socket for events from led-report (Phase 3/4) and
the Vibe file watcher (Phase 4).  Maintains per-session state, assigns ring
segments, and pushes full-state frames to the transport on every change.

Socket path: ~/.local/share/nsnotify/broker.sock
Run:         nsnotify-broker  (entry point wired in pyproject.toml)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from notify.broker.frame import FrameSegment, encode_frame
from notify.broker.segments import SegmentAllocator
from notify.broker.session import SESSION_TTL_S, SessionRecord, verb_to_state
from notify.state import State
from notify.transport import Transport
from notify.transport.serial_tx import SerialTransport, _find_esp32_port

log = logging.getLogger(__name__)

SOCKET_PATH = Path.home() / ".local" / "share" / "nsnotify" / "broker.sock"
BRIGHTNESS  = 30
TTL_CHECK_S = 60.0   # how often to sweep for stale sessions


class Broker:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._allocator = SegmentAllocator()
        self._seq       = 0

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def handle_event(self, msg: dict) -> None:
        """Process one inbound event dict from led-report."""
        session_id = msg.get("session_id", "")
        harness    = msg.get("harness", "unknown")
        cwd        = msg.get("cwd", "")
        verb       = msg.get("verb", "running")

        # Fold notification_type into the verb for Claude Notification events.
        if verb == "notify" and "notification_type" in msg:
            verb = f"notify:{msg['notification_type']}"

        state = verb_to_state(verb)

        if state == State.Offline:
            self._allocator.free(session_id)
        else:
            rec = SessionRecord(session_id=session_id, harness=harness,
                                cwd=cwd, state=state)
            if session_id not in self._allocator._index:
                self._allocator.register(rec)
            else:
                self._allocator.update(rec)

        self._push_frame()

    # ------------------------------------------------------------------
    # Frame push
    # ------------------------------------------------------------------

    def _push_frame(self) -> None:
        records  = self._allocator.active_segments()
        segments = [FrameSegment.from_state(r.state) for r in records]
        frame    = encode_frame(segments, BRIGHTNESS, self._seq)
        self._seq = (self._seq + 1) & 0xFF
        ok = self._transport.send(frame)
        if not ok:
            log.debug("frame not delivered (transport unavailable)")
        self._write_status(records)

    def _write_status(self, records: list) -> None:
        import time as _time
        status = {
            "ts": _time.time(),
            "sessions": [
                {
                    "session_id": r.session_id,
                    "harness":    r.harness,
                    "cwd":        r.cwd,
                    "state":      r.state.name,
                    "segment":    r.segment,
                }
                for r in records
            ],
        }
        path = SOCKET_PATH.parent / "status.json"
        tmp  = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(path)

    # ------------------------------------------------------------------
    # TTL housekeeping
    # ------------------------------------------------------------------

    async def _ttl_loop(self) -> None:
        while True:
            await asyncio.sleep(TTL_CHECK_S)
            evicted = self._allocator.evict_stale()
            if evicted:
                log.info("evicted stale sessions: %s", evicted)
                self._push_frame()


# ------------------------------------------------------------------
# Transport selection
# ------------------------------------------------------------------

def _make_transport(kind: str,
                    port: str | None = None,
                    ble_address: str | None = None) -> Transport:
    """Resolve --transport {serial,ble,auto} into a concrete transport.

    auto: serial iff an ESP32-looking port is present RIGHT NOW, else BLE.
    Resolved once at startup — no live failover in v1."""
    if kind == "auto":
        found = _find_esp32_port()
        kind = "serial" if found else "ble"
        log.info("transport auto-selected: %s%s",
                 kind, f" ({found})" if found else "")
    if kind == "serial":
        log.info("transport: serial (%s)", port or "auto-detect")
        return SerialTransport(port=port)
    if kind == "ble":
        # Lazy import so serial-only setups never touch bleak.
        from notify.transport.ble_tx import BleTransport
        log.info("transport: ble (%s)", ble_address or "scan by service UUID")
        return BleTransport(device_address=ble_address)
    raise ValueError(f"unknown transport kind: {kind!r}")


# ------------------------------------------------------------------
# Asyncio server
# ------------------------------------------------------------------

async def _handle_client(broker: Broker,
                          reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=2.0)
        msg  = json.loads(data.decode())
        broker.handle_event(msg)
        writer.write(b"ok\n")
        await writer.drain()
    except (json.JSONDecodeError, asyncio.TimeoutError, OSError) as exc:
        log.debug("client error: %s", exc)
    finally:
        writer.close()


async def _run(port: str | None = None,
               transport_kind: str = "serial",
               ble_address: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    transport = _make_transport(transport_kind, port=port,
                                ble_address=ble_address)
    broker    = Broker(transport)

    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(broker, r, w),
        path=str(SOCKET_PATH),
    )
    os.chmod(str(SOCKET_PATH), 0o600)

    log.info("broker listening on %s", SOCKET_PATH)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGINT,  stop.set_result, None)
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

    ttl_task = asyncio.create_task(broker._ttl_loop())

    async with server:
        await stop

    ttl_task.cancel()
    transport.close()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    log.info("broker stopped")


def _build_parser():
    import argparse
    p = argparse.ArgumentParser(description="Nuage Solide Notify broker daemon")
    p.add_argument("--port",
                   help="Serial port (serial transport only; default: auto-detect)")
    p.add_argument("--transport", choices=("serial", "ble", "auto"),
                   default="serial",
                   help="frame transport: serial (default), ble, or auto "
                        "(serial if an ESP32 port is present at startup, else ble)")
    p.add_argument("--ble-address",
                   help="BLE device address (CoreBluetooth UUID on macOS, "
                        "MAC on Linux; default: scan by service UUID)")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(_run(port=args.port,
                     transport_kind=args.transport,
                     ble_address=args.ble_address))


if __name__ == "__main__":
    main()
