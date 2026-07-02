"""
Phase 2 — USB-CDC serial transport.

Writes encoded frame bytes to the ESP32 over a serial port.
Reconnects automatically if the port disappears (e.g. USB unplug).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import serial
import serial.tools.list_ports

log = logging.getLogger(__name__)


def _find_esp32_port() -> str | None:
    """Return the first serial port that looks like an ESP32.

    Matches both generations: classic boards behind a USB-UART bridge chip
    (CP210x/CH340/FTDI) AND ESP32-S2/S3/C3 native USB — Espressif VID 0x303A,
    which enumerates as "USB JTAG/serial debug unit" (this is what the Nimbus
    S3 device is). No blind first-port fallback: it used to grab unrelated
    nodes like macOS's /dev/cu.debug-console and silently talk to nothing."""
    candidates = serial.tools.list_ports.comports()
    for p in candidates:
        if p.vid == 0x303A:  # Espressif native USB (S2/S3/C3/...)
            return p.device
    for p in candidates:
        desc = (p.description or "").lower()
        mfg  = (p.manufacturer or "").lower()
        if any(k in desc or k in mfg for k in ("cp210", "ch340", "ch341", "ftdi",
                                               "silicon", "esp32", "jtag")):
            return p.device
    return None


class SerialTransport:
    def __init__(self, port: str | None = None, baud: int = 115200) -> None:
        self._port   = port    # None → auto-detect on first send
        self._baud   = baud
        self._serial: Optional[serial.Serial] = None

    def send(self, frame: bytes) -> bool:
        """Write a frame.  Returns True on success, False on error (reconnect scheduled)."""
        if not self._ensure_open():
            return False
        try:
            assert self._serial is not None
            self._serial.write(frame)
            return True
        except serial.SerialException as exc:
            log.warning("serial write error: %s — will reconnect", exc)
            self._close()
            return False

    def close(self) -> None:
        self._close()

    # ------------------------------------------------------------------

    def _ensure_open(self) -> bool:
        if self._serial and self._serial.is_open:
            return True
        port = self._port or _find_esp32_port()
        if not port:
            log.debug("no serial port found")
            return False
        try:
            # Quiet-open: pySerial's default open asserts DTR+RTS, which on the
            # ESP32-S3's native USB-serial-JTAG (Nimbus) strobes the reset lines —
            # sometimes a spurious reboot, sometimes a wedged USB peripheral that
            # needs a bus reset to recover. Clear both BEFORE open so attaching
            # the broker is a pure listen. (See Nimbus AGENTS.md, "USB serial".)
            s = serial.Serial()
            s.port = port
            s.baudrate = self._baud
            s.timeout = 0.1
            s.dtr = False
            s.rts = False
            s.open()
            self._serial = s
            self._port   = port
            log.info("serial connected: %s @ %d", port, self._baud)
            return True
        except serial.SerialException as exc:
            log.debug("serial open failed (%s): %s", port, exc)
            self._serial = None
            return False

    def _close(self) -> None:
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
