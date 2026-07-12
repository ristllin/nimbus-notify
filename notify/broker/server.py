"""
Phase 2 — Broker daemon.

Listens on a Unix domain socket for events from led-report (Phase 3/4) and
the Vibe file watcher (Phase 4).  Maintains per-session state, assigns ring
segments, and pushes full-state frames to the transport on every change.

Socket path: ~/.local/share/nsnotify/broker.sock
Run:         nimbus-notify-broker  (entry point wired in pyproject.toml)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from pathlib import Path

from notify.broker.frame import FrameSegment, HARNESS_CODE, encode_frame
from notify.broker.segments import SegmentAllocator
from notify.broker.session import CTA_TTL_S, SESSION_TTL_S, SessionRecord, verb_to_state
from notify.harness.vibe import VibeWatcher
from notify.state import State
from notify.transport import Transport
from notify.transport.serial_tx import SerialTransport, _find_esp32_port

log = logging.getLogger(__name__)

SOCKET_PATH = Path.home() / ".local" / "share" / "nsnotify" / "broker.sock"
BRIGHTNESS  = 30
TTL_CHECK_S = 60.0   # upper bound on how often to sweep for stale sessions
MIN_TTL_S   = 5.0    # floor: never reap a session that's only seconds idle


class Broker:
    def __init__(self, transport: Transport, ttl: float = SESSION_TTL_S) -> None:
        self._transport = transport
        self._allocator = SegmentAllocator()
        self._seq       = 0
        # Idle-eviction TTLs (seconds) + how often to sweep. Two windows: benign
        # states (Idle/Running/Done) age out at self._ttl; call-to-action states
        # (a job blocked on the human) hold self._cta_ttl so the ring's "needs
        # you" signal can't vanish while pending — never shorter than the benign
        # window. The sweep is adaptive: a short TTL is useless if we only check
        # every 60 s, so the interval tracks the (shorter) benign TTL — ¼ of it,
        # but never slower than TTL_CHECK_S nor faster than MIN_TTL_S. Worst-case
        # benign linger ≈ ttl + _ttl_check.
        self._ttl       = max(MIN_TTL_S, ttl)
        self._cta_ttl   = max(self._ttl, CTA_TTL_S)
        self._ttl_check = max(MIN_TTL_S, min(TTL_CHECK_S, self._ttl / 4.0))
        # handle_event is called from TWO threads — the asyncio socket handler
        # (led-report events) and the VibeWatcher daemon thread (session
        # start/end + HITL inference).  Serialize so the allocator/seq/frame
        # push can't interleave.
        self._lock = threading.Lock()
        # Wired by _run() once the watcher exists; the broker feeds it the
        # before_tool/after_tool timing for its stalled-HITL heuristic (Vibe
        # exposes no approval hook — see notify/harness/vibe.py).
        self.vibe_watcher: VibeWatcher | None = None

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def handle_event(self, msg: dict) -> None:
        """Process one inbound event dict from led-report (or the watcher)."""
        session_id = msg.get("session_id", "")
        harness    = msg.get("harness", "unknown")
        cwd        = msg.get("cwd", "")
        verb       = msg.get("verb", "running")

        # Fold notification_type into the verb for Claude Notification events.
        if verb == "notify" and "notification_type" in msg:
            verb = f"notify:{msg['notification_type']}"

        # Feed the Vibe HITL tracker: a before_tool with no following after_tool
        # within the timeout means the tool is blocked on approval.
        if harness == "vibe" and self.vibe_watcher is not None:
            if verb == "before_tool":
                self.vibe_watcher.record_before_tool(session_id)
            elif verb.startswith("after_tool"):
                self.vibe_watcher.record_after_tool(session_id)

        state = verb_to_state(verb)

        with self._lock:
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
        # nsn v2: carry the harness + a short title (cwd basename) per segment so the
        # device e-ink NAMES a session ("codex nimbus: running") instead of "job N".
        # Byte-compatible: a v1 device ignores the trailing TLVs. (A protoVer gate to
        # suppress v2 for old firmware is a future refinement; small v2 frames fit the
        # v1 buffer anyway.)
        segments = []
        for r in records:
            seg = FrameSegment.from_state(r.state)
            seg.harness = HARNESS_CODE.get(r.harness, 0)
            seg.title = os.path.basename(r.cwd.rstrip("/")) if r.cwd else ""
            segments.append(seg)
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
            await asyncio.sleep(self._ttl_check)
            with self._lock:
                evicted = self._allocator.evict_stale(self._ttl, self._cta_ttl)
                if evicted:
                    log.info("evicted stale sessions (ttl=%.0fs/cta=%.0fs): %s",
                             self._ttl, self._cta_ttl, evicted)
                    self._push_frame()


# ------------------------------------------------------------------
# Transport selection
# ------------------------------------------------------------------

def _make_transport(kind: str,
                    port: str | None = None,
                    ble_address: str | None = None,
                    ble_name: str | None = None) -> Transport:
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
        target = ble_address or (f"name={ble_name}" if ble_name
                                 else "scan by service UUID")
        log.info("transport: ble (%s)", target)
        return BleTransport(device_address=ble_address, device_name=ble_name)
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
               ble_address: str | None = None,
               ble_name: str | None = None,
               ttl: float = SESSION_TTL_S) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    transport = _make_transport(transport_kind, port=port,
                                ble_address=ble_address, ble_name=ble_name)
    broker    = Broker(transport, ttl=ttl)
    log.info("idle-session TTL: %.0fs benign / %.0fs call-to-action "
             "(sweep every %.0fs)",
             broker._ttl, broker._cta_ttl, broker._ttl_check)

    # Vibe has no session start/stop hook: a background watcher on
    # ~/.vibe/logs/session/ supplies start/end + HITL inference, firing events
    # straight into broker.handle_event.  No-op (start() returns early) when the
    # vibe session dir is absent, so Claude/serial-only setups are unaffected.
    vibe_watcher = VibeWatcher(broker.handle_event)
    broker.vibe_watcher = vibe_watcher
    vibe_watcher.start()

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
    vibe_watcher.stop()
    transport.close()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    log.info("broker stopped")


# ---- service install (auto-start on reboot) --------------------------------
# macOS launchd LaunchAgent + Linux systemd --user unit, so the broker survives a
# reboot/login (the AI-session hooks fire-and-forget into its socket and silently
# no-op if it's down). ⚠ On macOS do the FIRST BLE bond in the FOREGROUND once
# (`nimbus-notify-broker --transport ble`) before relying on the detached service —
# a fully-detached process can't complete the macOS "Just Works" bond. Serial needs
# no bond. The service uses `--transport auto` (serial if a board is plugged at
# boot, else BLE).

_LAUNCHD_LABEL = "com.nimbus-notify.broker"
_SYSTEMD_UNIT  = "nimbus-notify-broker"
_LOG_PATH      = "/tmp/nimbus-notify-broker.log"


def _broker_argv() -> list[str]:
    """The command that starts the broker, absolute where possible. Prefer the
    installed console script; fall back to running the module with this interpreter."""
    import shutil
    import sys
    exe = shutil.which("nimbus-notify-broker")
    if exe:
        return [exe, "--transport", "auto"]
    return [sys.executable, "-m", "notify.broker.server", "--transport", "auto"]


def _install_service() -> int:
    import subprocess
    import sys
    argv = _broker_argv()
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
        args_xml = "\n".join(f"    <string>{a}</string>" for a in argv)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f'  <key>Label</key><string>{_LAUNCHD_LABEL}</string>\n'
            '  <key>ProgramArguments</key>\n  <array>\n'
            f'{args_xml}\n  </array>\n'
            '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n'
            f'  <key>StandardOutPath</key><string>{_LOG_PATH}</string>\n'
            f'  <key>StandardErrorPath</key><string>{_LOG_PATH}</string>\n'
            '</dict>\n</plist>\n')
        uid = os.getuid()
        # modern (bootstrap) first; fall back to legacy load on older macOS
        if subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)]).returncode != 0:
            subprocess.run(["launchctl", "load", str(plist)])
        print(f"installed launchd service {_LAUNCHD_LABEL}\n  {plist}\n  logs: {_LOG_PATH}")
        print("⚠ If using BLE: run `nimbus-notify-broker --transport ble` in the foreground "
              "ONCE first to complete the macOS bond, then it auto-starts on every login.")
        return 0
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / f"{_SYSTEMD_UNIT}.service"
        exec_start = " ".join(argv)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\n"
            "Description=nimbus-notify broker (AI coding session status -> device)\n"
            "After=network.target\n\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=on-failure\nRestartSec=3\n\n"
            "[Install]\nWantedBy=default.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        subprocess.run(["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT])
        print(f"installed systemd --user service {_SYSTEMD_UNIT}\n  {unit}")
        print("Tip: `loginctl enable-linger $USER` to keep it running after logout.")
        return 0
    print(f"--install-service is not supported on {sys.platform}; run the broker manually.")
    return 1


def _uninstall_service() -> int:
    import subprocess
    import sys
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
        uid = os.getuid()
        if subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)]).returncode != 0:
            subprocess.run(["launchctl", "unload", str(plist)])
        plist.unlink(missing_ok=True)
        print(f"removed launchd service {_LAUNCHD_LABEL}")
        return 0
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / f"{_SYSTEMD_UNIT}.service"
        subprocess.run(["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT])
        unit.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        print(f"removed systemd --user service {_SYSTEMD_UNIT}")
        return 0
    return 1


def _build_parser():
    import argparse
    p = argparse.ArgumentParser(description="Nimbus Notify broker daemon")
    p.add_argument("--install-service", action="store_true",
                   help="install the broker as an auto-starting service (macOS launchd / "
                        "Linux systemd --user) so it survives reboot, then exit")
    p.add_argument("--uninstall-service", action="store_true",
                   help="remove the auto-start service installed by --install-service, then exit")
    p.add_argument("--port",
                   help="Serial port (serial transport only; default: auto-detect)")
    p.add_argument("--transport", choices=("serial", "ble", "auto"),
                   default="serial",
                   help="frame transport: serial (default), ble, or auto "
                        "(serial if an ESP32 port is present at startup, else ble)")
    p.add_argument("--ble-address",
                   help="BLE device address (CoreBluetooth UUID on macOS, "
                        "MAC on Linux; default: scan by service UUID)")
    p.add_argument("--ble-name",
                   help="Connect ONLY to a peripheral advertising this exact "
                        "name (still gated on the nsn service UUID). Use when "
                        "several boards run this firmware on one desk, e.g. a "
                        "bench board named 'Nimbus-BT' vs a production 'Nimbus'. "
                        "On macOS this is the reliable discriminator (the MAC is "
                        "hidden).")
    p.add_argument("--ttl", type=float, default=SESSION_TTL_S,
                   metavar="SECONDS",
                   help=f"Idle-session eviction TTL in seconds for benign states "
                        f"(default: {SESSION_TTL_S:.0f}). A session whose harness "
                        "sends no events for this long is dropped from the ring — "
                        "this is what clears sessions that were KILLED (closed "
                        "terminal / kill / force-quit) without emitting a clean "
                        "'end'. Lower = killed/abandoned sessions clear faster; a "
                        "genuinely idle-but-alive session also drops but reappears "
                        "on its next activity. Call-to-action states (awaiting "
                        f"approval / input / error) always hold {CTA_TTL_S:.0f}s "
                        "instead, so a job blocked on you can't vanish. Clean exits "
                        "clear instantly via the SessionEnd hook regardless. "
                        f"Floored at {MIN_TTL_S:.0f}s.")
    return p


def main() -> None:
    import sys
    args = _build_parser().parse_args()
    if args.install_service:
        sys.exit(_install_service())
    if args.uninstall_service:
        sys.exit(_uninstall_service())
    asyncio.run(_run(port=args.port,
                     transport_kind=args.transport,
                     ble_address=args.ble_address,
                     ble_name=args.ble_name,
                     ttl=args.ttl))


if __name__ == "__main__":
    main()
